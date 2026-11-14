#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
НОВОСТНОЙ РАДАР СУРГУТА И ЮГРЫ — прототип для хакатона СИА-ПРЕСС.

Пайплайн: Сбор -> Отбор -> Уведомление (+ история находок в SQLite и веб-дашборд).

Запуск:
    pip install -r requirements.txt
    python news_radar.py --once            # один проход (для теста)
    python news_radar.py                   # цикл: проверка каждые N минут
    python news_radar.py --dashboard       # + веб-дашборд на http://127.0.0.1:8900
    python news_radar.py --test-telegram   # проверить, что бот подключён
    python news_radar.py --demo            # подбросить тестовую «горячую» новость (для показа)
    python news_radar.py --reset-db        # стереть историю и начать с чистого листа

Команды бота в Telegram (работают, пока радар запущен): /help /top /history /latest /sources /status /demo

Настройки — в config.yaml (создаётся автоматически при первом запуске).
Секреты (токен бота) — в файле .env, см. .env.example.
"""

import argparse
import hashlib
import html as html_mod
import importlib.util
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # .env с секретами (необязательно)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_PATH = BASE_DIR / "radar_history.db"

# Сургут живёт по времени UTC+5. Все даты из источников приводим к нему.
LOCAL_TZ = timezone(timedelta(hours=5))
# Заголовки как у обычного браузера: некоторые сайты не пускают скрипты с «чужим» User-Agent
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
# Парсер HTML: lxml быстрее, но если не установлен, работает встроенный
HTML_PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"

# Состояние источников для дашборда: {имя: {"time", "count", "error"}}
SOURCE_STATUS = {}
LAST_CYCLE = {"time": None}
DEMO_SOURCE = "Демо-источник"  # демо-новости в истории не хранятся

DEFAULT_CONFIG = r"""
# ===== Настройки новостного радара =====

# Как часто проверять источники, минут
interval_minutes: 15

# Минимальный балл, начиная с которого редактору приходит уведомление
notify_min_score: 35

# Отбор по теме: если в тексте нет достаточно «новостных» слов (сумма баллов темы
# меньше min_topic_score), балл не поднимается выше no_topic_cap. Это отсекает
# афиши, поздравления и прочие записи, где просто упомянут Сургут.
min_topic_score: 6
no_topic_cap: 30

# Сколько дней хранить и учитывать историю при удалении дублей
history_days: 7

# Уникальных находок в одном уведомлении (анти-спам пачками)
max_alerts_per_cycle: 10

# Сколько часов ждать, если Telegram временно недоступен: неотправленные находки
# уйдут при следующей проверке (а не потеряются)
pending_hours: 12

# Как считать «тот же новостной повод» (0-100): чем выше, тем строже
dup_threshold: 75

# Первый запуск только запоминает текущие новости и не шлёт уведомления
# (чтобы редактору не прилетели старые). Для демо поставьте false.
baseline_on_first_run: true

# Telegram-бот: токен и chat_id ЛУЧШЕ хранить в файле .env (см. .env.example),
# тогда сюда ничего вписывать не нужно. Без токена находки печатаются в консоль.
telegram:
  bot_token: ""
  chat_id: ""

# Вес географии (находим подстроку в тексте; большее число = важнее)
geo_weights:
  сургут: 40
  "ханты-мансийск": 25
  югра: 25
  "хмао": 25
  нижневартовск: 15
  нефтеюганск: 10
  когалым: 10
  "невъюганск": 10
  лангепас: 10
  пывара: 10
  "мейрог": 0

# Темы и ключевые слова с весами. Слово ищется как подстрока без учёта регистра.
categories:
  "ЧС и происшествия":
    weight: 1.4
    words:
      - {word: "пожар", w: 12}
      - {word: "дтп", w: 10}
      - {word: "авария", w: 10}
      - {word: "эвакуац", w: 12}
      - {word: "эвакуир", w: 12}
      - {word: "погиб", w: 14}
      - {word: "пострадал", w: 12}
      - {word: "пропал", w: 10}
      - {word: "угроза", w: 8}
      - {word: "отключен", w: 7}
      - {word: "энергосб", w: 7}
      - {word: "разлив нефт", w: 12}
      - {word: "взрыв", w: 14}
      - {word: "обрушен", w: 12}
      - {word: "подтоплен", w: 10}
      - {word: "паводок", w: 10}
      - {word: "метел", w: 6}
      - {word: "аномальн", w: 8}
      - {word: "ограничен", w: 5}
  "Правопорядок":
    weight: 1.3
    words:
      - {word: "задержа", w: 10}
      - {word: "полици", w: 6}
      - {word: "следственн", w: 8}
      - {word: "возбужд", w: 9}
      - {word: "уголовн", w: 9}
      - {word: "приговор", w: 9}
      - {word: "суд", w: 6}
      - {word: "наркот", w: 9}
      - {word: "мошенн", w: 9}
      - {word: "убийств", w: 14}
      - {word: "ограблен", w: 10}
      - {word: "подозреваем", w: 6}
      - {word: "краж", w: 7}
  "Власть и решения":
    weight: 1.2
    words:
      - {word: "мэр", w: 8}
      - {word: "глава города", w: 8}
      - {word: "губернатор", w: 8}
      - {word: "дума", w: 6}
      - {word: "бюджет", w: 8}
      - {word: "выборы", w: 7}
      - {word: "распоряжение", w: 6}
      - {word: "постановлен", w: 6}
  "Деньги и экономика":
    weight: 1.1
    words:
      - {word: "тариф", w: 8}
      - {word: "налог", w: 7}
      - {word: "инвест", w: 7}
      - {word: "стройк", w: 6}
      - {word: "реконструкц", w: 6}
      - {word: "тендер", w: 5}
      - {word: "закупк", w: 5}
  "Транспорт и город":
    weight: 1.1
    words:
      - {word: "дорог", w: 7}
      - {word: "ремонт", w: 5}
      - {word: "маршрут", w: 7}
      - {word: "автобус", w: 6}
      - {word: "перекры", w: 8}
      - {word: "мост", w: 7}
      - {word: "объезд", w: 7}
  "ЖКХ и инфраструктура":
    weight: 1.1
    words:
      - {word: "жкх", w: 7}
      - {word: "отоплен", w: 7}
      - {word: "водопровод", w: 7}
      - {word: "текстком", w: 5}
      - {word: "электроэнерг", w: 6}
  "Общество и события":
    weight: 0.8
    words:
      - {word: "школ", w: 5}
      - {word: "детск", w: 5}
      - {word: "больниц", w: 6}
      - {word: "вакцин", w: 6}
      - {word: "эпидем", w: 8}
      - {word: "субсиди", w: 6}

# Штрафы за «шум»: дайджесты, афиши, поздравления обычно не повод для срочного материала.
# Отрицательное число вычитается из балла (слово ищется с начала слова).
noise_words:
  - {word: "за неделю", w: -20}
  - {word: "дайджест", w: -20}
  - {word: "итоги недели", w: -20}
  - {word: "куда сходить", w: -20}
  - {word: "афиша", w: -15}
  - {word: "поздравл", w: -15}
  - {word: "график работы", w: -10}
  - {word: "режим работы", w: -10}

# Источники. type: rss | html | telegram
# priority 1..5: добавляет priority*3 к баллу.
# geo_hint: базовая география источника (ключ из geo_weights), если в тексте нет названия города.
sources:
  - name: СИА-ПРЕСС (RSS)
    type: rss
    url: https://siapress.ru/rss
    geo_hint: югра
    priority: 5
    enabled: true

  - name: Администрация Сургута (RSS)
    type: rss
    url: https://admsurgut.ru/bitrix/rss.php?ID=45&LANG=s1&TYPE=news&LIMIT=20
    geo_hint: сургут
    priority: 5
    enabled: true

  - name: Правительство Югры (Telegram)
    type: telegram
    url: https://t.me/s/ugra_official
    geo_hint: югра
    priority: 4
    enabled: true

  - name: Мэрия Сургута (Telegram)
    type: telegram
    url: https://t.me/s/admsurgut
    geo_hint: сургут
    priority: 5
    enabled: true

  # Добавляйте свои источники по образцу выше.
  # Для сайтов подберите CSS-селектор списка новостей через "Просмотр кода".
"""

# ----------------------------------------------------------------------------- #
#  Конфиг
# ----------------------------------------------------------------------------- #

def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG.strip() + "\n", encoding="utf-8")
        print(f"[init] Создан файл настроек: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ----------------------------------------------------------------------------- #
#  Мелкие помощники для текста
# ----------------------------------------------------------------------------- #

def _clean(text):
    """Убираем HTML-сущности (&nbsp; и т.п.), неразрывные пробелы и лишние пробелы."""
    text = html_mod.unescape(text or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def shorten(text, limit):
    """Обрезаем по границе слова, а не посреди слова, и ставим многоточие."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[limit // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-–—") + "…"


def normalize_title(t):
    t = unicodedata.normalize("NFKC", (t or "")).lower()
    return re.sub(r"\s+", " ", t).strip()


def make_hash(url, title):
    return hashlib.sha256(f"{(url or '').split('#')[0]}|{normalize_title(title)}".encode()).hexdigest()


# ----------------------------------------------------------------------------- #
#  База истории находок
# ----------------------------------------------------------------------------- #

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash TEXT UNIQUE,
            url TEXT,
            source TEXT,
            title TEXT,
            summary TEXT,
            category TEXT,
            score REAL,
            published TEXT,
            found_at TEXT,
            confirmations INTEGER DEFAULT 1,
            also_in TEXT DEFAULT '',
            why TEXT DEFAULT '',
            notified INTEGER DEFAULT 1
        )
    """)
    # Миграция: если база создана старой версией, добавляем недостающие колонки
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for name, ddl in [
        ("confirmations", "INTEGER DEFAULT 1"),
        ("also_in", "TEXT DEFAULT ''"),
        ("why", "TEXT DEFAULT ''"),
        ("notified", "INTEGER DEFAULT 1"),
    ]:
        if name not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
    conn.execute("DELETE FROM items WHERE source = ?", (DEMO_SOURCE,))  # демо не должно жить в истории
    conn.commit()
    return conn


def _stems(t):
    """Основы слов: первые 4 буквы, чтобы «Сургуте» и «Сургута» считались одним словом."""
    return {w[:4] for w in re.findall(r"\w+", normalize_title(t)) if len(w) >= 4}


def similarity(a, b):
    """Похожесть двух заголовков, 0..100. Берём лучшее из двух способов:
    1) посимвольное сходство строк (ловит почти одинаковые заголовки);
    2) пересечение основ слов (ловит ту же новость, пересказанную другим порядком слов)."""
    char_sim = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio() * 100
    sa, sb = _stems(a), _stems(b)
    common = len(sa & sb)
    stem_sim = 0
    if common >= 3 and min(len(sa), len(sb)) >= 4:
        stem_sim = 100 * common / min(len(sa), len(sb))
    return max(char_sim, stem_sim)


def hash_exists(conn, url_hash):
    return conn.execute("SELECT 1 FROM items WHERE url_hash = ?", (url_hash,)).fetchone() is not None


def find_similar_in_db(conn, title, history_days, threshold):
    """Ищем в истории новость с очень похожим заголовком (та же новость из другого источника)."""
    since = (datetime.now() - timedelta(days=history_days)).isoformat(timespec="seconds")
    for row_id, old, src, also in conn.execute(
        "SELECT id, title, source, also_in FROM items WHERE found_at >= ?", (since,)
    ):
        if old and similarity(title, old) >= threshold:
            return row_id, src, also or ""
    return None


def add_confirmation(conn, row_id, main_source, also_in, new_source):
    """Эту новость уже видели; если источник новый, +1 к «подтверждено источниками»."""
    known = {main_source, *[x for x in also_in.split(", ") if x]}
    if new_source in known:
        return
    known.discard(main_source)
    known.add(new_source)
    conn.execute(
        "UPDATE items SET confirmations = confirmations + 1, also_in = ? WHERE id = ?",
        (", ".join(sorted(known)), row_id),
    )
    conn.commit()


def save_item(conn, item):
    conn.execute(
        """INSERT OR IGNORE INTO items
           (url_hash, url, source, title, summary, category, score, published, found_at,
            confirmations, also_in, why, notified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item["url_hash"], item["url"], item["source"], item["title"],
            item["summary"], item["category"], item["score"],
            item["published"], datetime.now().isoformat(timespec="seconds"),
            item.get("confirmations", 1), ", ".join(item.get("also_in", [])),
            item.get("why", ""), item.get("notified", 1),
        ),
    )
    conn.commit()


def purge_old(conn, history_days):
    since = (datetime.now() - timedelta(days=history_days * 3)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM items WHERE found_at < ?", (since,))
    conn.commit()


# ----------------------------------------------------------------------------- #
#  1. СБОР: источники
# ----------------------------------------------------------------------------- #

def make_session():
    """Сессия с повторными попытками: сайт моргнул, пробуем ещё 2 раза."""
    s = requests.Session()
    s.headers["User-Agent"] = "SIA-Press News Radar/1.0 (hackathon prototype)"
    retry = Retry(total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch_rss(source, session):
    if not feedparser:
        print("[warn] не установлен feedparser — RSS-источники пропущены")
        return []
    resp = session.get(source["url"], timeout=20)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries[:25]:
        title = _clean(getattr(e, "title", ""))
        link = getattr(e, "link", "")
        summary = _clean(re.sub(r"<[^>]+>", " ", getattr(e, "summary", "") or getattr(e, "description", "")))[:400]
        pub = getattr(e, "published", "") or ""
        if title and link:
            items.append({"title": title, "url": link, "summary": summary, "published": pub})
    return items


def fetch_html(source, session):
    if not BeautifulSoup:
        print("[warn] не установлен beautifulsoup4 — HTML-источники пропущены")
        return []
    resp = session.get(source["url"], timeout=20, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, HTML_PARSER)
    items, seen = [], set()
    selector = source.get("item_selector") or "a"
    for a in soup.select(selector)[:40]:
        title = _clean(a.get_text())
        href = a.get("href", "")
        if not title or len(title) < 15 or not href:
            continue
        link = urljoin(source["url"], href)
        if link in seen:  # одна и та же ссылка часто встречается на странице дважды
            continue
        seen.add(link)
        # Небольшая выжимка: соседний текст с датой/анонсом, если есть
        parent_text = _clean(a.find_parent().get_text(" ", strip=True)) if a.parent else title
        items.append({"title": shorten(title, 200), "url": link, "summary": parent_text[:400], "published": ""})
    return items


def fetch_telegram(source, session):
    """Публичный веб-просмотр канала: t.me/s/<channel>."""
    if not BeautifulSoup:
        print("[warn] не установлен beautifulsoup4 — Telegram-источники пропущены")
        return []
    resp = session.get(source["url"], timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    for msg in soup.select(".tgme_widget_message")[:25]:
        text_node = msg.select_one(".tgme_widget_message_text")
        if not text_node:
            continue
        raw_text = text_node.get_text("\n", strip=True)
        text = _clean(raw_text)
        first_line = _clean(raw_text.split("\n")[0])
        # Заголовок: первая строка поста, если она осмысленная, иначе начало текста
        title = shorten(first_line if len(first_line) >= 20 else text, 160)
        time_tag = msg.select_one("time[datetime]")
        link_node = msg.select_one("a.tgme_widget_message_date")
        link = urljoin("https://t.me/", link_node["href"]) if link_node and link_node.get("href") else source["url"]
        published = time_tag["datetime"] if time_tag else ""
        if title:
            items.append({"title": title, "url": link, "summary": text[:600], "published": published})
    return items


FETCHERS = {"rss": fetch_rss, "html": fetch_html, "telegram": fetch_telegram}


def _fetch_one(src):
    """Забираем один источник. Возвращает (источник, записи, текст ошибки или None)."""
    fetch = FETCHERS.get(src["type"])
    if not fetch:
        return src, [], f"неизвестный тип источника: {src['type']}"
    try:
        return src, fetch(src, make_session()), None
    except Exception as e:
        return src, [], str(e)[:200] or type(e).__name__


def fetch_all(sources):
    """Все источники опрашиваются одновременно: один медленный сайт не тормозит остальные."""
    active = [s for s in sources if s.get("enabled", True)]
    if not active:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(active))) as pool:
        return list(pool.map(_fetch_one, active))


# ----------------------------------------------------------------------------- #
#  2. ОТБОР: гео + темы + оценка важности (с объяснением)
# ----------------------------------------------------------------------------- #

def _hit(word, low):
    """Слово найдено в начале слова: «мэр» ловит «мэрия», но не «самэр…»."""
    return re.search(r"(?<!\w)" + re.escape(word), low) is not None


def match_geo(text, geo_weights):
    """Возвращает (вес, название) самой важной найденной географии."""
    low = text.lower()
    best, name = 0, ""
    for geo, w in geo_weights.items():
        if geo and _hit(str(geo).lower(), low) and w > best:
            best, name = w, str(geo)
    return best, name


def match_topics(text, categories):
    low = text.lower()
    best_cat, best_score = "Общий поток", 0
    for cat, spec in categories.items():
        raw = sum(min(w["w"], 15) for w in spec.get("words", []) if _hit(w["word"], low))
        weighted = raw * spec.get("weight", 1.0)
        if weighted > best_score:
            best_cat, best_score = cat, weighted
    return best_cat, min(best_score, 60)


def noise_penalty(text, cfg):
    """Штраф за дайджесты, афиши и поздравления. Возвращает (штраф <= 0, причина)."""
    low = text.lower()
    penalty, found = 0, []
    for nw in cfg.get("noise_words", []) or []:
        if _hit(nw["word"], low):
            penalty += nw["w"]
            found.append(nw["word"])
    if text.count("•") >= 2:  # пост-подборка из нескольких новостей
        penalty -= 20
        found.append("подборка")
    penalty = max(penalty, -40)
    return penalty, ", ".join(found)


def parse_dt(ts):
    """Понимает и ISO (Telegram), и RFC-822 (RSS). Возвращает время с часовым поясом или None."""
    if not ts:
        return None
    ts = ts.strip()
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(ts)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def score_item(item, cfg):
    """Возвращает (балл, тема, объяснение «почему такой балл»)."""
    text = f"{item['title']} {item['summary']}"
    geo_weights = cfg.get("geo_weights", {})
    geo_text, geo_name = match_geo(text, geo_weights)
    # Если источник сам по себе «про Сургут» (мэрия), даём ему базовую географию
    hint = str(item.get("geo_hint") or "").lower()
    hint_w = 0.6 * geo_weights.get(hint, 0) if hint else 0
    if hint_w > geo_text:
        geo, geo_label = hint_w, f"{hint.capitalize()} (по источнику)"
    else:
        geo, geo_label = geo_text, geo_name.capitalize()

    cat, topic_score = match_topics(text, cfg.get("categories", {}))
    priority_boost = 3 * int(item.get("source_priority", 3))

    fresh_bonus = 0
    dt = parse_dt(item.get("published", ""))
    if dt:
        hours = max((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 0)
        fresh_bonus = 10 if hours <= 2 else (5 if hours <= 6 else 0)

    penalty, noise_why = noise_penalty(text, cfg)

    score = geo + topic_score + priority_boost + fresh_bonus + penalty
    why = []
    if geo:
        why.append(f"{geo_label} +{geo:.0f}")
    if topic_score:
        why.append(f"{cat} +{topic_score:.0f}")
    why.append(f"источник +{priority_boost}")
    if fresh_bonus:
        why.append(f"свежо +{fresh_bonus}")
    if penalty:
        why.append(f"шум ({noise_why}) {penalty}")
    if geo == 0 and score > 20:
        score = 20  # без привязки к региону выше «фонового» не поднимаем
        why.append("нет привязки к региону, потолок 20")
    cap = cfg.get("no_topic_cap", 30)
    if topic_score < cfg.get("min_topic_score", 6) and score > cap:
        score = cap  # Сургут упомянут, но событие неясно: в уведомления не пускаем
        why.append(f"нет значимой темы, потолок {cap}")
    return round(min(max(score, 0), 100), 1), cat, "; ".join(why)  # шкала 0..100


# ----------------------------------------------------------------------------- #
#  3. УВЕДОМЛЕНИЕ
# ----------------------------------------------------------------------------- #

def _plural(n, one, few, many):
    n = int(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else (few if 2 <= n <= 4 else many)


def _human_time(ts):
    """Любую дату -> '14.11 16:20' по времени Сургута; непонятный формат отдаём как есть."""
    if not ts:
        return datetime.now(LOCAL_TZ).strftime("%d.%m %H:%M")
    dt = parse_dt(ts)
    return dt.astimezone(LOCAL_TZ).strftime("%d.%m %H:%M") if dt else ts


def format_alert(item):
    icon = "🔥" if item["score"] >= 60 else ("❗" if item["score"] >= 45 else "ℹ️")
    pub = _human_time(item.get("published", ""))
    summary = (item.get("summary") or "")[:300]
    title = item["title"]
    # если «суть» просто повторяет заголовок, второй раз её не показываем
    summary_line = html_mod.escape(summary) + "\n" if summary and not summary.startswith(title[:60]) else ""
    conf = item.get("confirmations", 1)
    conf_line = ""
    if conf > 1:
        conf_line = f"✅ Подтверждено источниками: {conf} ({html_mod.escape(', '.join(item.get('also_in', [])))})\n"
    why = item.get("why", "")
    why_line = f"<i>Почему: {html_mod.escape(why)}</i>\n" if why else ""
    return (
        f"{icon} <b>{item['score']:.0f} {_plural(item['score'], 'балл', 'балла', 'баллов')}</b> · {html_mod.escape(item['category'])}\n"
        f"<b>{html_mod.escape(title[:180])}</b>\n"
        f"{summary_line}\n"
        f"Источник: {html_mod.escape(item['source'])}\n"
        f"{conf_line}"
        f"{why_line}"
        f"🔗 {html_mod.escape(item['url'])}\n"
        f"🕐 {pub}"
    )


def telegram_creds(cfg):
    # Секреты лучше хранить в .env, а не в config.yaml (см. .env.example)
    tg = cfg.get("telegram") or {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id", "")
    return str(token or "").strip(), str(chat_id or "").strip()


# Кнопки под полем ввода в Telegram: нажатие отправляет боту текст кнопки
KEYBOARD = {
    "keyboard": [
        [{"text": "🏆 Топ"}, {"text": "🕘 История"}],
        [{"text": "🛰 Источники"}, {"text": "📊 Статус"}],
        [{"text": "🧪 Проверка"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}
BUTTONS = {
    "🏆 Топ": "/top",
    "🕘 История": "/history",
    "🛰 Источники": "/sources",
    "📊 Статус": "/status",
    "🧪 Проверка": "/demo",
}


def telegram_chat_ids(cfg):
    """В TELEGRAM_CHAT_ID можно указать нескольких получателей через запятую."""
    _, raw = telegram_creds(cfg)
    return [x for x in re.split(r"[,\s;]+", raw) if x]


def _send_one(token, chat_id, text, preview):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": not preview, "reply_markup": KEYBOARD},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[warn] Telegram недоступен: {e}")
        return "stop"
    if r.status_code == 200:
        return "ok"
    print(f"[warn] Telegram вернул {r.status_code} для чата {chat_id}: {r.text[:160]}")
    return "stop" if r.status_code == 429 or r.status_code >= 500 else "skip"


def send_telegram(cfg, text, preview=True, to=None):
    """None — бот не настроен; 'ok' — доставлено хотя бы одному получателю;
    'stop' — временный сбой (повторим позже); 'skip' — Telegram отклонил насовсем.
    to — отправить только в этот чат (так бот отвечает на команды)."""
    token, _ = telegram_creds(cfg)
    ids = [str(to)] if to else telegram_chat_ids(cfg)
    if not (token and ids):
        return None
    results = [_send_one(token, cid, text, preview) for cid in ids]
    if "ok" in results:
        return "ok"
    return "stop" if "stop" in results else "skip"


def notify(cfg, items):
    """Отправляет находки. Возвращает множество url_hash тех, что считаются доставленными."""
    delivered, stopped = set(), False
    for it in items:
        msg = format_alert(it)
        print("\n" + re.sub(r"<[^>]+>", "", html_mod.unescape(msg)))  # дубль в консоль всегда
        if stopped:
            continue
        res = send_telegram(cfg, msg)
        if res in (None, "ok", "skip"):
            delivered.add(it["url_hash"])
            if res == "ok":
                time.sleep(0.4)  # у Telegram лимит на частоту сообщений
        else:
            stopped = True
            print("[warn] Отправка прервана, остаток уйдёт при следующей проверке.")
    return delivered


def load_pending(conn, cfg):
    """Находки, которые пора показать редактору, но ещё не доставлены (новые + неотправленные раньше)."""
    since = (datetime.now() - timedelta(hours=cfg.get("pending_hours", 12))).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT url_hash, url, source, title, summary, category, score, published,
                  confirmations, also_in, why
           FROM items WHERE notified = 0 AND score >= ? AND found_at >= ?
           ORDER BY score DESC LIMIT ?""",
        (cfg.get("notify_min_score", 35), since, cfg.get("max_alerts_per_cycle", 10)),
    ).fetchall()
    keys = ["url_hash", "url", "source", "title", "summary", "category", "score", "published",
            "confirmations", "also_in", "why"]
    items = []
    for row in rows:
        d = dict(zip(keys, row))
        d["also_in"] = [x for x in (d["also_in"] or "").split(", ") if x]
        items.append(d)
    return items


def deliver_pending(conn, cfg):
    items = load_pending(conn, cfg)
    if not items:
        return 0
    delivered = notify(cfg, items)
    conn.executemany("UPDATE items SET notified = 1 WHERE url_hash = ?", [(h,) for h in delivered])
    conn.commit()
    return len(delivered)


# ----------------------------------------------------------------------------- #
#  Веб-дашборд (история находок + состояние источников)
# ----------------------------------------------------------------------------- #

DASH_PORT = 8900

DASH_CSS = """
body{font-family:system-ui,Segoe UI,sans-serif;margin:0;background:#f5f5f5;color:#222}
header{background:#d52b1e;color:#fff;padding:14px 24px}
header h1{margin:0;font-size:20px}header p{margin:4px 0 0;opacity:.9;font-size:13px}
main{padding:18px 24px}
.card{background:#fff;border-radius:10px;padding:14px 18px;margin-bottom:16px;box-shadow:0 1px 3px #0001}
table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #eee;padding:7px 10px;font-size:14px;text-align:left;vertical-align:top}
th{background:#fafafa;font-weight:600}
.b{display:inline-block;min-width:34px;text-align:center;border-radius:6px;padding:2px 6px;color:#fff;font-weight:700}
.hi{background:#d52b1e}.mid{background:#e8890c}.lo{background:#8a8a8a}
.why{color:#777;font-size:12px}
form{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
label{font-size:12px;color:#555;display:flex;flex-direction:column;gap:3px}
input,select,button{padding:6px 8px;font-size:14px}
a{color:#0b5cad;text-decoration:none}a:hover{text-decoration:underline}
.ok{color:#1a8f3a}.bad{color:#c0392b}
"""


class DashHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/":
            self.send_response(204)
            self.end_headers()
            return
        qs = parse_qs(url.query)
        try:
            min_score = int((qs.get("min", ["0"])[0] or "0"))
        except ValueError:
            min_score = 0
        cat = qs.get("cat", [""])[0]
        q = qs.get("q", [""])[0].strip().lower()

        conn = db_connect()
        rows = conn.execute(
            """SELECT found_at, score, category, source, title, url, confirmations, why, summary
               FROM items WHERE score >= ? ORDER BY found_at DESC, score DESC LIMIT 600""",
            (min_score,),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()

        categories = sorted({r[2] for r in rows if r[2]})
        if cat:
            rows = [r for r in rows if r[2] == cat]
        if q:
            rows = [r for r in rows if q in (r[4] or "").lower() or q in (r[8] or "").lower()]
        rows = rows[:200]

        esc = html_mod.escape  # заголовки из интернета нельзя вставлять в HTML как есть

        def badge(score):
            cls = "hi" if score >= 60 else ("mid" if score >= 45 else "lo")
            return f"<span class='b {cls}'>{score:.0f}</span>"

        rows_html = "".join(
            f"<tr><td>{esc(_found_time(f))}</td><td>{badge(s)}</td><td>{esc(c or '')}</td><td>{esc(src)}</td>"
            f"<td>{n}</td><td><a href='{esc(u, quote=True)}' target='_blank'>{esc(t[:140])}</a>"
            f"<div class='why'>{esc(w or '')}</div></td></tr>"
            for f, s, c, src, t, u, n, w, _summary in rows
        )
        status_html = "".join(
            f"<tr><td>{'<span class=ok>✅</span>' if not st['error'] else '<span class=bad>⚠️</span>'}</td>"
            f"<td>{esc(name)}</td><td>{st['count']}</td><td>{esc(st['time'])}</td>"
            f"<td class='bad'>{esc(st['error'] or '')}</td></tr>"
            for name, st in SOURCE_STATUS.items()
        ) or "<tr><td colspan=5>Ещё не было ни одной проверки</td></tr>"
        cat_opts = "<option value=''>все темы</option>" + "".join(
            f"<option value='{esc(c, quote=True)}'{' selected' if c == cat else ''}>{esc(c)}</option>" for c in categories
        )
        last = LAST_CYCLE["time"] or "—"
        page = f"""<!doctype html><html lang='ru'><meta charset='utf-8'><meta http-equiv='refresh' content='60'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Радар Сургута и Югры</title><style>{DASH_CSS}</style>
<header><h1>📡 Новостной радар: Сургут — приоритет №1, далее вся Югра</h1>
<p>Последняя проверка: {esc(last)} · всего находок в базе: {total} · страница обновляется сама раз в минуту</p></header>
<main>
<div class='card'><b>Источники</b>
<table><tr><th></th><th>Название</th><th>Записей</th><th>Проверен</th><th>Ошибка</th></tr>{status_html}</table></div>
<div class='card'><form method='get'>
<label>Минимальный балл<input type='number' name='min' value='{min_score}' min='0' max='100'></label>
<label>Тема<select name='cat'>{cat_opts}</select></label>
<label>Поиск в тексте<input name='q' value='{esc(q, quote=True)}' placeholder='например, пожар'></label>
<button>Показать</button></form></div>
<div class='card'><table><tr><th>Найдено</th><th>Балл</th><th>Тема</th><th>Источник</th><th title='сколько источников написали об этом'>Подтв.</th><th>Заголовок и почему такой балл</th></tr>{rows_html}</table></div>
</main>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def log_message(self, *a):
        pass


def start_dashboard():
    Thread(target=lambda: ThreadingHTTPServer(("127.0.0.1", DASH_PORT), DashHandler).serve_forever(), daemon=True).start()
    print(f"[dashboard] Открыт на http://127.0.0.1:{DASH_PORT}")


# ----------------------------------------------------------------------------- #
#  Цикл: сбор -> отбор -> уведомление
# ----------------------------------------------------------------------------- #

# ----------------------------------------------------------------------------- #
#  Команды бота в Telegram: /top /history /sources /status /help
# ----------------------------------------------------------------------------- #

def _found_time(ts):
    """found_at хранится по времени компьютера; показываем по времени Сургута."""
    try:
        return datetime.fromisoformat(ts).astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return ts or ""


def _icon(score):
    return "🔥" if score >= 60 else ("❗" if score >= 45 else "ℹ️")


def _list_items(rows):
    esc = html_mod.escape
    blocks = []
    for found_at, score, category, source, title, url, conf in rows:
        mark = f" · ✅×{conf}" if conf and conf > 1 else ""
        blocks.append(
            f"{_icon(score)} <b>{score:.0f}</b> · {esc(category or '—')}{mark}\n"
            f"<a href=\"{esc(url, quote=True)}\">{esc(shorten(title, 110))}</a>\n"
            f"<i>{esc(source)} · {_found_time(found_at)}</i>"
        )
    return "\n\n".join(blocks)


BOT_COMMANDS = [
    ("top", "Топ новостей за сутки по баллам (/top 10)"),
    ("history", "Вся история по убыванию баллов (/history 15)"),
    ("latest", "Самые свежие находки по времени"),
    ("sources", "Источники, приоритеты и их состояние"),
    ("demo", "Тестовое уведомление (проверка связи)"),
    ("status", "Состояние радара и настройки"),
    ("help", "Список команд"),
]

BOT_HELP = (
    "📡 <b>Новостной радар Сургута и Югры</b>\n"
    "Сам присылаю важные новости. А ещё умею:\n\n"
    + "\n".join(f"/{c} — {d}" for c, d in BOT_COMMANDS)
)


def bot_reply(cfg, command):
    """Готовит ответ на команду бота. Данные берём из той же базы, что и дашборд."""
    esc = html_mod.escape
    parts = command.split()
    cmd = parts[0].split("@")[0].lower()
    arg = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None  # например /top 10
    if cmd in ("/start", "/help"):
        return BOT_HELP
    if cmd == "/demo":
        return format_alert(make_demo_item(cfg))
    conn = db_connect()
    try:
        cols = "found_at, score, category, source, title, url, confirmations"
        if cmd == "/top":
            n = min(arg or 5, 10)
            since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
            rows = conn.execute(
                f"SELECT {cols} FROM items WHERE found_at >= ? ORDER BY score DESC, found_at DESC LIMIT ?",
                (since, n),
            ).fetchall()
            if not rows:
                return "За последние сутки находок пока нет."
            return f"🏆 <b>Топ-{len(rows)} за сутки (по баллам)</b>\n\n" + _list_items(rows)
        if cmd == "/history":
            n = min(arg or 10, 20)
            rows = conn.execute(
                f"SELECT {cols} FROM items ORDER BY score DESC, found_at DESC LIMIT ?", (n,)
            ).fetchall()
            if not rows:
                return "История пока пуста."
            return f"🕘 <b>История: {len(rows)} лучших находок по баллам</b>\n\n" + _list_items(rows)
        if cmd == "/latest":
            n = min(arg or 10, 20)
            rows = conn.execute(
                f"SELECT {cols} FROM items ORDER BY found_at DESC, score DESC LIMIT ?", (n,)
            ).fetchall()
            if not rows:
                return "История пока пуста."
            rows.sort(key=lambda r: -r[1])  # внутри свежей десятки — по убыванию баллов
            return f"🆕 <b>Последние {len(rows)} находок</b>\n\n" + _list_items(rows)
        if cmd == "/sources":
            lines = []
            for src in cfg.get("sources", []):
                st = SOURCE_STATUS.get(src["name"])
                if not src.get("enabled", True):
                    icon, info = "⏸", "выключен"
                elif not st:
                    icon, info = "⏳", "ещё не проверялся"
                elif st["error"]:
                    icon, info = "⚠️", "ошибка"
                else:
                    icon, info = "✅", f"записей: {st['count']}, проверен {st['time']}"
                lines.append(
                    f"{icon} <b>{esc(src['name'])}</b>\n"
                    f"    приоритет {src.get('priority', 3)}/5 · {esc(info)}"
                )
            return "🛰 <b>Источники</b>\n\n" + "\n".join(lines)
        if cmd == "/status":
            total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            min_score = cfg.get("notify_min_score", 35)
            important = conn.execute("SELECT COUNT(*) FROM items WHERE score >= ?", (min_score,)).fetchone()[0]
            enabled = sum(1 for s in cfg.get("sources", []) if s.get("enabled", True))
            return (
                "📊 <b>Состояние радара</b>\n"
                f"Последняя проверка: {esc(LAST_CYCLE['time'] or '—')}\n"
                f"Проверка каждые {cfg.get('interval_minutes', 15)} мин, источников включено: {enabled}\n"
                f"Находок в истории: {total}, из них важных (от {min_score} баллов): {important}"
            )
    finally:
        conn.close()
    return "Не знаю такую команду. Список команд: /help"


def bot_commands_loop(cfg):
    """Слушает команды редактора в Telegram. Отвечает только в чат из .env."""
    token, _ = telegram_creds(cfg)
    allowed = set(telegram_chat_ids(cfg))
    if not (token and allowed):
        return
    api = f"https://api.telegram.org/bot{token}"
    offset, warned = None, False
    try:
        # пропускаем старые сообщения, чтобы не отвечать на то, что написали до запуска
        old = requests.get(f"{api}/getUpdates", params={"offset": -1}, timeout=15).json().get("result", [])
        if old:
            offset = old[-1]["update_id"] + 1
        requests.post(
            f"{api}/setMyCommands",
            json={"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[warn] Команды бота: не удалось подключиться: {e}")
    print("[bot] Слушаю команды в Telegram (/help)")
    send_telegram(cfg, "📡 <b>Радар запущен.</b> Кнопки внизу покажут топ, историю и состояние. "
                       "«🧪 Проверка» пришлёт тестовое уведомление.", preview=False)
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": '["message"]'}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{api}/getUpdates", params=params, timeout=45)
            if r.status_code != 200:
                if not warned:
                    print(f"[warn] Команды бота: Telegram вернул {r.status_code}")
                    warned = True
                time.sleep(10)
                continue
            warned = False
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                asker = str((msg.get("chat") or {}).get("id"))
                text = BUTTONS.get(text, text)  # нажатая кнопка = команда
                if asker not in allowed or not text.startswith("/"):
                    continue  # чужие чаты и обычные сообщения игнорируем
                if text.split()[0].split("@")[0].lower() == "/demo":
                    send_demo(cfg, to=asker)  # проверка: тестовое уведомление тому, кто нажал
                    continue
                send_telegram(cfg, bot_reply(cfg, text), preview=False, to=asker)
        except requests.RequestException:
            time.sleep(10)
        except Exception as e:
            print(f"[warn] Команды бота: {e}")
            time.sleep(10)


def run_cycle(cfg):
    conn = db_connect()
    days = cfg.get("history_days", 7)
    threshold = cfg.get("dup_threshold", 75)
    min_score = cfg.get("notify_min_score", 35)
    first_run = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    new_items, errors = [], 0

    for src, raw, err in fetch_all(cfg.get("sources", [])):
        stamp = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
        SOURCE_STATUS[src["name"]] = {"time": stamp, "count": len(raw), "error": err}
        if err:
            errors += 1
            print(f"[warn] {src['name']}: {err}")
            continue
        print(f"[src] {src['name']}: получено записей {len(raw)}")
        for raw_item in raw:
            url_hash = make_hash(raw_item["url"], raw_item["title"])
            if hash_exists(conn, url_hash):
                continue
            # Та же новость, но из другого источника, в этом же цикле?
            twin = next((n for n in new_items if similarity(n["title"], raw_item["title"]) >= threshold), None)
            if twin:
                if src["name"] != twin["source"] and src["name"] not in twin["also_in"]:
                    twin["also_in"].append(src["name"])
                    twin["confirmations"] += 1
                continue
            # ...или в истории за последние дни?
            old = find_similar_in_db(conn, raw_item["title"], days, threshold)
            if old:
                add_confirmation(conn, old[0], old[1], old[2], src["name"])
                continue
            score, cat, why = score_item(
                {**raw_item, "source_priority": src.get("priority", 3), "geo_hint": src.get("geo_hint", "")}, cfg
            )
            new_items.append({
                "url_hash": url_hash,
                "url": raw_item["url"],
                "source": src["name"],
                "title": raw_item["title"],
                "summary": raw_item["summary"],
                "category": cat,
                "score": score,
                "why": why,
                "published": raw_item["published"],
                "confirmations": 1,
                "also_in": [],
            })

    # Чем больше независимых источников написали об этом, тем важнее и достовернее
    for it in new_items:
        if it["confirmations"] > 1:
            bonus = 6 * (it["confirmations"] - 1)
            it["score"] = min(100, it["score"] + bonus)
            it["why"] += f"; подтверждено {it['confirmations']} источниками +{bonus}"

    # Первый запуск: всё, что лежит на сайтах сейчас, не «свежее», а просто накопленное.
    # Запоминаем это тихо, чтобы редактору не прилетело 10 старых новостей.
    baseline = first_run and cfg.get("baseline_on_first_run", True) and bool(new_items)
    if baseline:
        print("[init] Первый запуск: запоминаем текущие новости без уведомлений.")
    for it in new_items:
        # notified=0 значит «надо показать редактору» (если не baseline и балл достаточный)
        it["notified"] = 0 if (it["score"] >= min_score and not baseline) else 1

    purge_old(conn, days)
    for it in new_items:
        save_item(conn, it)

    sent = deliver_pending(conn, cfg)
    conn.close()

    LAST_CYCLE["time"] = datetime.now(LOCAL_TZ).strftime("%d.%m %H:%M:%S")
    print(f"\n[cycle] {datetime.now():%H:%M} | новых записей: {len(new_items)} | "
          f"уведомлений: {sent} | ошибок источников: {errors}")
    return len(new_items), sent, errors


# ----------------------------------------------------------------------------- #
#  Вспомогательные команды
# ----------------------------------------------------------------------------- #

DEMO_SCENARIOS = [
    ("В Сургуте горит жилой дом на улице Энергетиков, эвакуированы жильцы",
     "Тестовая новость для демонстрации радара: пожар, эвакуация, есть пострадавшие."),
    ("В Сургуте на Югорском тракте массовое ДТП, движение перекрыто",
     "Тестовая новость для демонстрации радара: авария, пострадавшие, организован объезд."),
    ("В Сургуте из-за аварии на теплотрассе отключено отопление в нескольких домах",
     "Тестовая новость для демонстрации радара: коммунальная авария, жильцы остались без тепла."),
]


def make_demo_item(cfg):
    """Тестовая «горячая» новость. Оценивается настоящим алгоритмом, но в базу не попадает."""
    stamp = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
    title, summary = random.choice(DEMO_SCENARIOS)
    raw = {
        "title": f"ДЕМО: {title} ({stamp})",
        "url": "https://siapress.ru/",
        "summary": summary,
        "published": datetime.now(timezone.utc).isoformat(),
    }
    score, cat, why = score_item({**raw, "source_priority": 5, "geo_hint": "сургут"}, cfg)
    return {
        "url_hash": "demo", "url": raw["url"], "source": DEMO_SOURCE, "title": raw["title"],
        "summary": raw["summary"], "category": cat, "score": score, "why": why,
        "published": raw["published"], "confirmations": 1, "also_in": [],
    }


def send_demo(cfg, to=None):
    """Проверка связи: тестовое уведомление (в историю не попадает). to — только этому чату."""
    msg = format_alert(make_demo_item(cfg))
    print("\n" + re.sub(r"<[^>]+>", "", html_mod.unescape(msg)))
    return send_telegram(cfg, msg, to=to)


def run_demo(cfg):
    """Отправляет тестовую новость всем получателям. В историю и дашборд она не попадает."""
    notify(cfg, [make_demo_item(cfg)])
    print("[demo] Готово. В историю демо-новость не сохраняется.")


def run_test_telegram(cfg):
    token, chat_id = telegram_creds(cfg)
    if not (token and chat_id):
        print("[test] Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID (файл .env).")
        return
    res = send_telegram(cfg, "✅ <b>Новостной радар подключён.</b>\nСюда будут приходить важные новости Сургута и Югры.")
    print("[test] Готово: проверьте Telegram." if res == "ok" else "[test] Не получилось, причина в строке [warn] выше.")


def main():
    parser = argparse.ArgumentParser(description="Новостной радар Сургута и Югры")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--dashboard", action="store_true", help="поднять веб-дашборд")
    parser.add_argument("--demo", action="store_true", help="подбросить тестовую горячую новость")
    parser.add_argument("--test-telegram", action="store_true", help="отправить тестовое сообщение в Telegram")
    parser.add_argument("--reset-db", action="store_true", help="стереть историю находок")
    args = parser.parse_args()

    if sys.stdout is None or sys.stderr is None:  # запуск без окна консоли (pythonw): пишем в radar.log
        log = open(BASE_DIR / "radar.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    if args.reset_db:
        DB_PATH.unlink(missing_ok=True)
        print("[reset] История стёрта.")
        return

    cfg = load_config()
    if args.test_telegram:
        run_test_telegram(cfg)
        return
    if args.demo:
        run_demo(cfg)
        return
    if args.dashboard:
        start_dashboard()
    if not args.once or args.dashboard:  # команды бота работают, пока радар не закрыт
        Thread(target=bot_commands_loop, args=(cfg,), daemon=True).start()

    try:
        if args.once:
            run_cycle(cfg)
            if args.dashboard:  # чтобы дашборд не закрылся сразу
                print("Дашборд работает. Ctrl+C — выход.")
                while True:
                    time.sleep(1)
            return
        while True:
            try:
                run_cycle(cfg)
            except Exception as e:
                print(f"[error] сбой цикла: {e}")
            time.sleep(int(cfg.get("interval_minutes", 15)) * 60)
    except KeyboardInterrupt:
        print("\n[stop] Остановлено.")
        sys.exit(0)


if __name__ == "__main__":
    main()