##!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
НОВОСТНОЙ РАДАР СУРГУТА И ЮГРЫ — прототип для хакатона СИА-ПРЕСС.

Пайплайн: Сбор -> Отбор -> Уведомление (+ история находок в SQLite и веб-дашборд).

ЗАПУСК. Запустите файл (кнопка Run в PyCharm). Появится меню, введите цифру:

    1 — ЗАПУСТИТЬ РАДАР в фоне: проверка источников каждые N минут + бот в Telegram
        + дашборд (http://127.0.0.1:8900). Достаточно нажать один раз: дальше радар
        работает сам, а меню остаётся доступным.
    2 — ДЕМО: прислать в Telegram тестовую «горячую» новость (в историю не попадает).
    3 — ТЕСТ TELEGRAM: отправить «радар подключён» (проверка токена и chat_id из .env).
    4 — ОДНА ПРОВЕРКА СЕЙЧАС: сразу опросить источники и показать результат.
    5 — СБРОС ИСТОРИИ: стереть базу находок (спросит «да/нет»).
    6 — ПРОВЕРКА ИСТОЧНИКОВ: какие сайты и каналы реально отдают новости (без уведомлений).
    0 — ВЫХОД: единственный способ закрыть программу (радар при этом остановится).

После каждого действия меню появляется снова: программа сама не закрывается.
Цифру можно передать и сразу: python news_radar.py 2 (выполнит и покажет меню).
Старые команды тоже работают: --once, --dashboard, --demo, --test-telegram, --reset-db,
а также новая --check-sources.
Перед первым запуском: pip install -r requirements.txt

ДОСТОВЕРНОСТЬ. У каждого источника в config.yaml есть доверие (trust 1..5) и тип (kind):
официальные источники (власть) получают бонус к баллу, «народные» каналы — небольшой
штраф и пометку «не подтверждено, перепроверьте». Если новость из народного канала потом
подтверждает надёжный источник, радар присылает отдельное сообщение «подтверждено».

Кнопки и команды бота в Telegram (работают, пока радар запущен): /top /history /latest /sources /status /demo /help

Настройки — в config.yaml (лежит рядом с этим файлом).
Секреты (токен бота) — в файле .env, см. env.example.
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
from threading import Lock, Thread
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


def now_iso(**delta):
    """Текущее время в UTC (ISO, с +00:00), минус необязательный сдвиг: now_iso(hours=24).
    В базе все даты хранятся в UTC, поэтому радар работает одинаково на любом компьютере и сервере,
    а показывается время Сургута (см. _found_time)."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")
# Заголовки как у обычного браузера: некоторые сайты не пускают скрипты с «чужим» User-Agent
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
# Парсер HTML: lxml быстрее, но если не установлен, работает встроенный
HTML_PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"

# Состояние источников для дашборда: {имя: {"time", "count", "error", "trust", "kind"}}
SOURCE_STATUS = {}
LAST_CYCLE = {"time": None}
DEMO_SOURCE = "Демо-источник"  # демо-новости в истории не хранятся

# Типы источников (для вывода): official — власть, media — СМИ,
# community — «народный» канал, author — личный канал/блог, portal — городской портал
KIND_RU = {
    "official": "власть",
    "media": "СМИ",
    "community": "народный канал",
    "author": "личный канал",
    "portal": "городской портал",
}


# ----------------------------------------------------------------------------- #
#  Конфиг
# ----------------------------------------------------------------------------- #

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"[error] Не найден файл настроек: {CONFIG_PATH}\n"
                 "Положите config.yaml в одну папку с news_radar.py.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    set_source_kinds(cfg)
    return cfg


# ----------------------------------------------------------------------------- #
#  Охват новости: первоисточник, СМИ, «своё» издание
# ----------------------------------------------------------------------------- #

SOURCE_KINDS = {}   # имя источника -> kind (official | author | media | portal | community)
OWN_MEDIA = ()      # части названий «своих» источников (издание-заказчик)
MEDIA_KINDS = ("media", "portal")
PRIMARY_KINDS = ("official", "author")


def set_source_kinds(cfg):
    global OWN_MEDIA
    SOURCE_KINDS.clear()
    SOURCE_KINDS.update({s["name"]: s.get("kind", "") for s in cfg.get("sources", []) or []})
    OWN_MEDIA = tuple(str(x).lower() for x in (cfg.get("own_media") or []))


def split_also(text):
    """Разбирает строку also_in. Названия источников сами содержат запятые («SiTV, Сургутинформтв»),
    поэтому режем не по «, », а ищем известные названия из config.yaml."""
    text = text or ""
    found = sorted((n for n in SOURCE_KINDS if n in text), key=text.index)
    rest = text
    for n in found:
        rest = rest.replace(n, "")
    return found + [x.strip() for x in rest.split(", ") if x.strip() and x.strip() not in ("",)]


def is_own(name):
    return any(o in (name or "").lower() for o in OWN_MEDIA)


def coverage_status(source, also_in):
    """Что редактору делать с находкой: берёт (code, текст).
    own   — уже вышло в своём издании; media — уже написали СМИ;
    first — написал только первоисточник (власть), СМИ ещё нет: можно брать в работу первым;
    rumor — только «народные» каналы, нужна проверка."""
    names = [source, *(also_in or [])]
    own = [n for n in names if is_own(n)]
    if own:
        return "own", "✅ Уже опубликовано у нас"
    media = [n for n in names if SOURCE_KINDS.get(n) in MEDIA_KINDS]
    if media:
        return "media", f"📰 Уже в СМИ: {', '.join(media)}"
    if any(SOURCE_KINDS.get(n) in PRIMARY_KINDS for n in names):
        return "first", "🎯 Только первоисточник: СМИ ещё не писали, можно брать в работу"
    return "rumor", "⚠️ Только «народные» каналы: проверьте до публикации"


# ----------------------------------------------------------------------------- #
#  Мелкие помощники для текста
# ----------------------------------------------------------------------------- #

def _clean(text):
    """Убираем HTML-сущности (&nbsp; и т.п.), неразрывные пробелы и лишние пробелы."""
    text = html_mod.unescape(text or "").replace("\xa0", " ")
    text = re.sub("[\u200b\u200c\u200d\ufeff]", "", text)  # невидимые символы (siapress.ru ставит их в заголовки)
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
#  Достоверность источников
# ----------------------------------------------------------------------------- #

def src_trust(src):
    """Доверие к источнику 1..5 (по умолчанию 3). 5 — власть и проверенные СМИ, 1-2 — слухи и «народные» каналы."""
    try:
        return min(5, max(1, int(src.get("trust", 3))))
    except (TypeError, ValueError):
        return 3


def trust_dot(trust):
    trust = trust or 3
    return "🟢" if trust >= 4 else ("🟡" if trust == 3 else "🔴")


def verification_text(trust, confirmations):
    """Короткая пометка для редактора: можно ли доверять новости без перепроверки."""
    trust = trust or 3
    confirmations = confirmations or 1
    if trust >= 4:
        return "🟢 Надёжный источник"
    if confirmations >= 2:
        return "🟡 Подтверждено несколькими источниками"
    if trust == 3:
        return "🟡 Источник среднего доверия: желательно проверить"
    return "🔴 Не подтверждено (народный канал): перепроверьте до публикации"


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
            notified INTEGER DEFAULT 1,
            trust INTEGER DEFAULT 3
        )
    """)
    # Миграция: если база создана старой версией, добавляем недостающие колонки
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for name, ddl in [
        ("confirmations", "INTEGER DEFAULT 1"),
        ("also_in", "TEXT DEFAULT ''"),
        ("why", "TEXT DEFAULT ''"),
        ("notified", "INTEGER DEFAULT 1"),
        ("trust", "INTEGER DEFAULT 3"),
        ("media_at", "TEXT DEFAULT ''"),   # когда новость впервые заметили в СМИ (для «опережения»)
    ]:
        if name not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
    # Какие источники радар уже «знает»: у нового источника первый заход проходит тихо,
    # чтобы редактору не прилетели старые посты нового канала.
    conn.execute("CREATE TABLE IF NOT EXISTS seen_sources (name TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS feedback (url_hash TEXT, chat_id TEXT, vote INTEGER, ts TEXT, PRIMARY KEY (url_hash, chat_id))")
    conn.execute(
        "INSERT OR IGNORE INTO seen_sources (name) SELECT DISTINCT source FROM items WHERE source != ?",
        (DEMO_SOURCE,),
    )
    conn.execute("DELETE FROM items WHERE source = ?", (DEMO_SOURCE,))  # демо не должно жить в истории
    # Миграция времени: старые записи хранились без часового пояса (время компьютера в Сургуте, UTC+5).
    # Переводим их в UTC один раз, чтобы сравнения «за последние сутки» не путались.
    for rid, ts in conn.execute("SELECT id, found_at FROM items WHERE found_at NOT LIKE '%+%'").fetchall():
        try:
            dt = datetime.fromisoformat(ts).replace(tzinfo=LOCAL_TZ)
        except (TypeError, ValueError):
            continue
        conn.execute("UPDATE items SET found_at = ? WHERE id = ?",
                     (dt.astimezone(timezone.utc).isoformat(timespec="seconds"), rid))
    conn.commit()
    return conn


# Слова, которые есть почти в каждом заголовке: по ним новости не сравниваем
_STOP_STEMS = {"сург", "югры", "югра", "хмао", "горо", "райо", "окру", "обла", "улиц", "этот", "тоже",
               "будут", "рабо", "поср", "стал", "стали", "прои"}


def _stems(t):
    """Основы слов: первые 4 буквы, чтобы «Сургуте» и «Сургута» считались одним словом.
    Слова-«паразиты» региона и шаблонов (Сургут, город, район…) не учитываются."""
    return {w[:4] for w in re.findall(r"\w+", normalize_title(t))
            if len(w) >= 4 and w[:4] not in _STOP_STEMS}


def _key_tokens(t):
    """Числа и имена собственные (не с начала предложения): их различие значит «другая новость»."""
    nums = set(re.findall(r"\d+", t or ""))
    words = re.findall(r"\w+", t or "")
    names = {w.lower()[:4] for w in words[1:]
             if len(w) >= 3 and w[0].isupper() and w.lower()[:4] not in _STOP_STEMS and not w.isupper()}
    return nums, names


def similarity(a, b, strict=True):
    """Похожесть двух заголовков, 0..100. Берём лучшее из двух способов:
    1) посимвольное сходство строк (ловит почти одинаковые заголовки);
    2) пересечение основ слов (ловит ту же новость, пересказанную другим порядком слов).
    strict=True добавляет защиту от ложных дублей: разные числа или названия (улица Энгельса / улица Мира)
    и слишком малое пересечение содержательных слов («горячую воду» / «свет») — это разные новости."""
    char_sim = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio() * 100
    sa, sb = _stems(a), _stems(b)
    common = len(sa & sb)
    stem_sim = 0
    if common >= 3 and min(len(sa), len(sb)) >= 4:
        stem_sim = 100 * common / min(len(sa), len(sb))
    sim = max(char_sim, stem_sim)
    if strict:
        (na, pa), (nb, pb) = _key_tokens(a), _key_tokens(b)
        if (na and nb and not (na & nb)) or (pa and pb and not (pa & pb)):
            return min(sim, 40)
        if min(len(sa), len(sb)) >= 2 and common / min(len(sa), len(sb)) < 0.6:
            return min(sim, 60)
    return sim


def hash_exists(conn, url_hash):
    return conn.execute("SELECT 1 FROM items WHERE url_hash = ?", (url_hash,)).fetchone() is not None


def find_similar_in_db(conn, title, history_days, threshold):
    """Ищем в истории новость с очень похожим заголовком (та же новость из другого источника).
    Возвращает словарь со строкой из базы или None."""
    since = now_iso(days=history_days)
    for row in conn.execute(
        "SELECT id, title, url, source, also_in, trust, notified, score, found_at FROM items WHERE found_at >= ?",
        (since,),
    ):
        if row[1] and similarity(title, row[1]) >= threshold:
            return {
                "id": row[0], "title": row[1], "url": row[2], "source": row[3],
                "also_in": row[4] or "", "trust": row[5] or 3, "notified": row[6], "score": row[7] or 0,
                "found_at": row[8],
            }
    return None


def add_confirmation(conn, old, new_source, new_trust, min_score, set_media=False):
    """Эту новость уже видели; если источник новый, +1 к «подтверждено источниками».
    Возвращает True, если новость до этого шла только от ненадёжных источников и уже была
    показана редактору, а теперь её подтвердил надёжный (тогда радар шлёт «подтверждено»)."""
    known = {old["source"], *split_also(old["also_in"])}
    if new_source in known:
        return False
    known.discard(old["source"])
    known.add(new_source)
    conn.execute(
        "UPDATE items SET confirmations = confirmations + 1, also_in = ?, trust = ? WHERE id = ?",
        (", ".join(sorted(known)), max(old["trust"], new_trust), old["id"]),
    )
    if set_media:  # СМИ подхватили то, что радар уже видел у первоисточника
        conn.execute("UPDATE items SET media_at = ? WHERE id = ? AND COALESCE(media_at, '') = ''",
                     (now_iso(), old["id"]))
    conn.commit()
    return old["trust"] < 4 <= new_trust and old["notified"] == 1 and old["score"] >= min_score


def save_item(conn, item):
    conn.execute(
        """INSERT OR IGNORE INTO items
           (url_hash, url, source, title, summary, category, score, published, found_at,
            confirmations, also_in, why, notified, trust)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item["url_hash"], item["url"], item["source"], item["title"],
            item["summary"], item["category"], item["score"],
            item["published"], now_iso(),
            item.get("confirmations", 1), ", ".join(item.get("also_in", [])),
            item.get("why", ""), item.get("notified", 1), item.get("trust", 3),
        ),
    )
    conn.commit()


def purge_old(conn, history_days):
    since = now_iso(days=history_days * 3)
    conn.execute("DELETE FROM items WHERE found_at < ?", (since,))
    conn.commit()


# ----------------------------------------------------------------------------- #
#  1. СБОР: источники
# ----------------------------------------------------------------------------- #

def make_session():
    """Сессия с повторными попытками: сайт моргнул, пробуем ещё 2 раза."""
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
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


def split_card(a, min_len):
    """Ссылка-карточка вида «заголовок + анонс» в одном <a>: возвращает (заголовок, анонс).
    Без этого тексты склеиваются («...обсуждениеАдминистрация Сургута...»). Обычная ссылка
    с одним заголовком возвращается как раньше, анонс пустой."""
    texts = [_clean(x) for x in a.stripped_strings if _clean(x)]
    need = max(min_len, 25)
    for i, t in enumerate(texts):
        if len(t) >= need:
            if i + 1 < len(texts) and len(texts[i + 1]) >= 25 and all(len(x) < need for x in texts[:i]):
                return t, " ".join(texts[i + 1:])
            break
    return _clean(a.get_text()), ""


def fetch_html(source, session):
    """Сайт без RSS: берём ссылки по CSS-селектору item_selector.
    Необязательные поля источника: exclude_href — список кусков адреса, которые надо пропускать
    (например, /tag/), min_title_len — минимальная длина заголовка (по умолчанию 15)."""
    if not BeautifulSoup:
        print("[warn] не установлен beautifulsoup4 — HTML-источники пропущены")
        return []
    resp = session.get(source["url"], timeout=20, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, HTML_PARSER)
    items, seen = [], set()
    selector = source.get("item_selector") or "a"
    excluded = source.get("exclude_href") or []
    min_len = int(source.get("min_title_len", 15))
    list_page = source["url"].rstrip("/")
    for a in soup.select(selector)[:60]:
        title, lead = split_card(a, min_len)
        href = a.get("href", "")
        if not title or len(title) < min_len or not href:
            continue
        link = urljoin(source["url"], href)
        if link.rstrip("/") == list_page or any(x in link for x in excluded):
            continue  # ссылка на сам раздел или служебная страница (теги и т.п.)
        if link in seen:  # одна и та же ссылка часто встречается на странице дважды
            continue
        seen.add(link)
        # Небольшая выжимка: соседний текст с датой/анонсом, если есть
        parent_text = lead or (_clean(a.find_parent().get_text(" ", strip=True)) if a.parent else title)
        items.append({"title": shorten(title, 200), "url": link, "summary": parent_text[:400], "published": ""})
        if len(items) >= 40:
            break
    return items


def normalize_tg_url(raw):
    """Принимает что угодно: @name, name, t.me/name, https://t.me/name, https://t.me/s/name
    и возвращает адрес веб-просмотра канала https://t.me/s/name."""
    u = str(raw or "").strip()
    if not u or "ВПИШИТЕ" in u.upper():
        raise ValueError("не указан ник Telegram-канала (поле url в config.yaml)")
    m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{3,})", u) \
        or re.match(r"^@?([A-Za-z0-9_]{3,})$", u)
    if not m:
        raise ValueError(f"не удалось понять адрес канала: {u[:60]} (для закрытых каналов с + веб-просмотра нет)")
    return f"https://t.me/s/{m.group(1)}"


def fetch_telegram(source, session):
    """Публичный веб-просмотр канала: t.me/s/<channel>."""
    if not BeautifulSoup:
        print("[warn] не установлен beautifulsoup4 — Telegram-источники пропущены")
        return []
    url = normalize_tg_url(source["url"])
    resp = session.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.select(".tgme_widget_message")
    if not messages:
        raise RuntimeError("постов нет: ник неверный, канал закрыт или у него отключён веб-просмотр")
    items = []
    for msg in messages[-25:][::-1]:  # на странице посты идут от старых к новым: берём свежие
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
        link = urljoin("https://t.me/", link_node["href"]) if link_node and link_node.get("href") else url
        published = time_tag["datetime"] if time_tag else ""
        if title:
            items.append({"title": title, "url": link, "summary": text[:600], "published": published})
    return items


FETCHERS = {"rss": fetch_rss, "html": fetch_html, "telegram": fetch_telegram}


def _fetch_one(src):
    """Забираем один источник. Возвращает (источник, записи, текст ошибки или None)."""
    fetch = FETCHERS.get(src.get("type"))
    if not fetch:
        return src, [], f"неизвестный тип источника: {src.get('type')}"
    try:
        return src, fetch(src, make_session()), None
    except Exception as e:
        return src, [], str(e)[:200] or type(e).__name__


def fetch_many(sources):
    """Все источники опрашиваются одновременно: один медленный сайт не тормозит остальные."""
    if not sources:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
        return list(pool.map(_fetch_one, sources))


def fetch_all(sources):
    return fetch_many([s for s in sources if s.get("enabled", True)])


def check_sources(cfg):
    """Режим 6: проверка, какие источники живы. Ничего не сохраняет и не отправляет."""
    sources = cfg.get("sources", [])
    print(f"\n=== Проверка источников: всего {len(sources)} ===")
    ok = bad = 0
    for src, raw, err in fetch_many([s for s in sources if s.get("enabled", True)]):
        trust = src_trust(src)
        head = (f"{trust_dot(trust)} {src['name']} [{KIND_RU.get(src.get('kind'), '—')}, "
                f"доверие {trust}/5, приоритет {src.get('priority', 3)}/5]")
        if err:
            bad += 1
            print(f"⚠️  {head}\n     ошибка: {err}")
        else:
            ok += 1
            sample = shorten(raw[0]["title"], 90) if raw else "—"
            print(f"✅ {head}\n     записей: {len(raw)}; пример: {sample}")
    for src in sources:
        if not src.get("enabled", True):
            print(f"⏸  {src['name']}: выключен в config.yaml")
    print(f"\nИтог: работает {ok}, с ошибкой {bad}.")


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


# «в Саратовской области», «в Красноярском крае», «в Республике ...»: прилагательное на -ск/-цк + область/край/республика
_FOREIGN_RE = re.compile(
    r"(?<!\w)([а-яё\-]*(?:ск|цк)(?:ая|ой|ую|ий|ом|ого|ое|им))\s+"
    r"(?:област(?:ь|и|ью)|кра(?:й|я|е|ю)|республик(?:а|и|е|у|ой))(?!\w)")
LOCAL_REGION_STEMS = ("ханты", "мансийск", "югор", "сургут", "тюменск")


def find_foreign(low, cfg):
    """Ищет упоминание чужого региона в тексте (нижний регистр). Возвращает найденное или ''."""
    for place in cfg.get("foreign_places") or []:
        if _hit(str(place).lower(), low):
            return str(place)
    for m in _FOREIGN_RE.finditer(low):
        if not any(s in m.group(1) for s in LOCAL_REGION_STEMS):
            return m.group(0)
    return ""


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


def score_item(item, cfg, now=None):
    """Возвращает (балл, тема, объяснение «почему такой балл»)."""
    text = f"{item['title']} {item['summary']}"
    geo_weights = cfg.get("geo_weights", {})
    geo_text, geo_name = match_geo(text, geo_weights)
    # Нет ни одного местного слова, но назван чужой регион: подсказку географии от источника не даём
    foreign = find_foreign(text.lower(), cfg) if geo_text == 0 else ""
    foreign_pen = float(cfg.get("foreign_penalty", -15)) if foreign else 0
    # Если источник сам по себе «про Сургут» (мэрия), даём ему базовую географию
    hint = str(item.get("geo_hint") or "").lower()
    hint_w = 0.6 * geo_weights.get(hint, 0) if (hint and not foreign) else 0
    if hint_w > geo_text:
        geo, geo_label = hint_w, f"{hint.capitalize()} (по источнику)"
    else:
        geo, geo_label = geo_text, geo_name.capitalize()

    cat, topic_score = match_topics(text, cfg.get("categories", {}))
    priority_boost = 3 * int(item.get("source_priority", 3))

    # Достоверность: власть и проверенные СМИ чуть выше, «народные» каналы чуть ниже
    trust = int(item.get("source_trust", 3))
    trust_adj = (trust - 3) * float(cfg.get("trust_weight", 3))

    # Бонусы «от источника» (приоритет, доверие, география по источнику) работают только вместе с темой:
    # сильная тема (>= topic_full) даёт их целиком, слабая или её отсутствие урезает пропорционально.
    # Так официальная страница с постом «о публичных слушаниях» не проходит порог только за то, что она официальная.
    topic_full = float(cfg.get("topic_full", 20))
    k = min(1.0, topic_score / topic_full) if topic_full > 0 else 1.0
    if hint_w > geo_text:
        geo, geo_label = hint_w * k, f"{hint.capitalize()} (по источнику)"
    priority_boost *= k
    if trust_adj > 0:
        trust_adj *= k

    fresh_bonus = 0
    dt = parse_dt(item.get("published", ""))
    if dt:
        hours = max(((now or datetime.now(timezone.utc)) - dt).total_seconds() / 3600, 0)
        fresh_bonus = 10 if hours <= 2 else (5 if hours <= 6 else 0)

    penalty, noise_why = noise_penalty(text, cfg)

    score = geo + topic_score + priority_boost + trust_adj + fresh_bonus + penalty + foreign_pen
    why = []
    if geo:
        why.append(f"{geo_label} +{geo:.0f}")
    if topic_score:
        why.append(f"{cat} +{topic_score:.0f}")
    why.append(f"источник +{priority_boost:.0f}")
    if trust_adj:
        why.append(f"доверие {trust}/5 {trust_adj:+.0f}")
    if fresh_bonus:
        why.append(f"свежо +{fresh_bonus}")
    if penalty:
        why.append(f"шум ({noise_why}) {penalty}")
    if foreign:
        why.append(f"другой регион ({foreign}) {foreign_pen:+.0f}")
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

# ----------------------------------------------------------------------------- #
#  Краткая суть (выжимка из текста источника)
# ----------------------------------------------------------------------------- #

SUMMARY_JUNK = ("подписывайтесь", "подписаться", "прислать новость", "читайте нас", "читайте также",
                "подробнее", "реклама", "фото:", "источник:", "наш канал", "присылайте")


def _plain(text):
    """Текст без ссылок, хэштегов, упоминаний и эмодзи."""
    text = re.sub(r"https?://\S+|www\.\S+|#\w+|@\w+", " ", text or "")
    text = re.sub(r"[^\w\s.,;:!?…«»\"'()%№/+\-–—−₽°$€]", " ", text)
    return _clean(text)


def _finish(s):
    """Предложение, оборванное на полуслове (источник обрезал текст), заканчиваем многоточием."""
    if re.search(r"[.!?…»)\"]$", s):
        return s
    return (s.rsplit(" ", 1)[0] if " " in s else s).rstrip(" ,;:-–—") + "…"


def make_summary(title, text, cfg, limit=280):
    """Краткая суть: 1-2 самых содержательных предложения из текста источника.
    Сначала убираем ссылки, хэштеги, эмодзи и повтор заголовка; потом предложения получают баллы
    (слова тем из config.yaml, привязка к региону, цифры, позиция в тексте); берём лучшие
    и ставим в исходном порядке. Пустая строка, если сказать больше заголовка нечего."""
    text = _plain(text)
    truncated = _plain(title).endswith("…")  # заголовок Telegram-поста обрезан на полуслове
    base = _plain(title).rstrip("…").strip()
    if base and text.lower().startswith(base.lower()):
        rest = text[len(base):].lstrip(" .:-–—")
        if truncated and rest:
            # продолжаем с небольшим перекрытием, чтобы фраза не начиналась с обрубка
            text = "…" + " ".join(base.split()[-4:]) + " " + rest
        else:
            text = rest
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?…])\s+(?=[А-ЯЁA-Z«\"0-9])", text)
    cands = []
    for s in parts:
        s = s.strip()
        if len(s) < 25 or any(j in s.lower() for j in SUMMARY_JUNK):
            continue
        if base and similarity(s, base, strict=False) >= 70:
            continue  # пересказ заголовка
        cands.append(s)
    if not cands:
        return ""
    words = [w for spec in (cfg.get("categories") or {}).values() for w in spec.get("words", [])]
    geo_weights = cfg.get("geo_weights", {})

    def value(i):
        s = cands[i]
        low = s.lower()
        topic = sum(min(w["w"], 15) for w in words if _hit(w["word"], low))
        geo = 10 if match_geo(s, geo_weights)[0] else 0
        pos = {0: 12, 1: 6, 2: 3}.get(i, 0)
        num = 3 if re.search(r"\d", s) else 0
        return topic + geo + pos + num

    chosen, used = [], 0
    for i in sorted(range(len(cands)), key=lambda k: -value(k)):
        s = cands[i]
        if not chosen:
            chosen.append(i)
            used = min(len(s), limit)
        elif len(chosen) < 2 and used + 1 + len(s) <= limit:
            chosen.append(i)
            used += 1 + len(s)
    return " ".join(_finish(shorten(cands[i], limit)) for i in sorted(chosen))


# ----------------------------------------------------------------------------- #
#  Обратная связь редактора: кнопки 👍/👎 под новостью, радар подстраивает баллы
# ----------------------------------------------------------------------------- #

def feedback_markup(url_hash, voted=None):
    """Две кнопки под уведомлением. voted: 1 или -1: выбранная отмечается галочкой."""
    h = (url_hash or "demo")[:16]
    up = "👍 Важно" + (" ✓" if voted == 1 else "")
    down = "👎 Не то" + (" ✓" if voted == -1 else "")
    return {"inline_keyboard": [[{"text": up, "callback_data": f"fb:u:{h}"},
                                 {"text": down, "callback_data": f"fb:d:{h}"}]]}


def load_feedback_stats(conn):
    """{('src', источник): (👍, 👎), ('cat', тема): (👍, 👎)} по всем отзывам в базе."""
    stats = {}
    rows = conn.execute("SELECT i.source, i.category, f.vote FROM feedback f "
                        "JOIN items i ON i.url_hash = f.url_hash").fetchall()
    for src, cat, vote in rows:
        for key in (("src", src), ("cat", cat)):
            up, down = stats.get(key, (0, 0))
            stats[key] = (up + (vote > 0), down + (vote < 0))
    return stats


def feedback_adjust(stats, source, category, cfg):
    """Поправка к баллу по отзывам редактора: источник и тема, которые чаще получали 👎, теряют баллы,
    с 👍 получают. Работает, когда по группе есть минимум feedback_min_votes отзывов;
    максимум ±feedback_weight на группу. feedback_weight: 0 в config.yaml отключает обучение."""
    weight = float(cfg.get("feedback_weight", 8))
    min_votes = int(cfg.get("feedback_min_votes", 3))
    total, parts = 0.0, []
    for key, label in ((("src", source), "источник"), (("cat", category), "тема")):
        up, down = stats.get(key, (0, 0))
        n = up + down
        if weight and n >= min_votes:
            adj = weight * (up - down) / (n + 2)  # +2: осторожность при малом числе отзывов
            total += adj
            parts.append(f"{label} {adj:+.0f} ({up}👍 {down}👎)")
    return total, "; ".join(parts)


def handle_feedback(token, allowed, cb):
    """Нажатие 👍/👎: сохраняем отзыв, отвечаем всплывающей подсказкой и отмечаем выбранную кнопку."""
    api = f"https://api.telegram.org/bot{token}"
    msg = cb.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id"))

    def answer(text=""):
        try:
            requests.post(f"{api}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": text}, timeout=15)
        except requests.RequestException:
            pass

    m = re.match(r"^fb:([ud]):([0-9a-f]{4,32}|demo)$", cb.get("data") or "")
    if chat not in allowed or not m:
        answer()
        return
    vote = 1 if m.group(1) == "u" else -1
    key = m.group(2)
    if key == "demo":
        answer("Это демо-новость: отзыв не сохраняется")
        return
    conn = db_connect()
    try:
        row = conn.execute("SELECT url_hash FROM items WHERE url_hash LIKE ?", (key + "%",)).fetchone()
        if not row:
            answer("Эта новость уже удалена из истории")
            return
        conn.execute(
            """INSERT INTO feedback (url_hash, chat_id, vote, ts) VALUES (?, ?, ?, ?)
               ON CONFLICT(url_hash, chat_id) DO UPDATE SET vote = excluded.vote, ts = excluded.ts""",
            (row[0], chat, vote, now_iso()))
        conn.commit()
    finally:
        conn.close()
    answer("Учту: такие новости важны 👍" if vote > 0 else "Учту: такие новости не нужны 👎")
    try:
        requests.post(f"{api}/editMessageReplyMarkup",
                      json={"chat_id": chat, "message_id": msg.get("message_id"),
                            "reply_markup": feedback_markup(key, vote)}, timeout=15)
    except requests.RequestException:
        pass


def quality_text(conn, cfg):
    """Ответ на /quality: насколько редактор согласен с тем, что присылает радар."""
    min_score = cfg.get("notify_min_score", 35)
    sent = conn.execute("SELECT COUNT(*) FROM items WHERE notified = 1 AND score >= ?", (min_score,)).fetchone()[0]
    voted = conn.execute("SELECT COUNT(DISTINCT url_hash) FROM feedback").fetchone()[0]
    up, down = conn.execute("SELECT COALESCE(SUM(vote > 0), 0), COALESCE(SUM(vote < 0), 0) FROM feedback").fetchone()
    if not (up or down):
        return ("📈 <b>Качество радара</b>\nОценок редактора пока нет. Нажимайте 👍 «Важно» или 👎 «Не то» "
                "под новостями: радар учтёт это в следующих проверках.")
    lines = [
        "📈 <b>Качество радара по оценкам редактора</b>",
        f"Оценено новостей: {voted} из {sent} присланных",
        f"👍 {up} · 👎 {down}: точность {100 * up / (up + down):.0f}% (доля нужных из оценённых)",
    ]
    rows = conn.execute(
        """SELECT i.source, SUM(f.vote > 0), SUM(f.vote < 0) FROM feedback f
           JOIN items i ON i.url_hash = f.url_hash GROUP BY i.source ORDER BY COUNT(*) DESC LIMIT 8""").fetchall()
    if rows:
        lines.append("\n<b>По источникам</b>")
        lines += [f"{html_mod.escape(s)}: 👍 {u} · 👎 {d}" for s, u, d in rows]
    lines.append("\nОценки влияют на баллы: источники и темы с 👎 получают меньше, с 👍 больше.")
    return "\n".join(lines)


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
    summary = shorten(item.get("summary") or "", 300)
    title = item["title"]
    # если «суть» просто повторяет заголовок, второй раз её не показываем
    summary_line = html_mod.escape(summary) + "\n" if summary and not summary.startswith(title[:60]) else ""
    conf = item.get("confirmations", 1)
    conf_line = ""
    if conf > 1:
        conf_line = f"✅ Подтверждено источниками: {conf} ({html_mod.escape(', '.join(item.get('also_in', [])))})\n"
    trust_line = html_mod.escape(verification_text(item.get("trust", 3), conf)) + "\n"
    cov_line = html_mod.escape(coverage_status(item["source"], item.get("also_in", []))[1]) + "\n"
    why = item.get("why", "")
    why_line = f"<i>Почему: {html_mod.escape(why)}</i>\n" if why else ""
    return (
        f"{icon} <b>{item['score']:.0f} {_plural(item['score'], 'балл', 'балла', 'баллов')}</b> · {html_mod.escape(item['category'])}\n"
        f"<b>{html_mod.escape(shorten(title, 180))}</b>\n"
        f"{summary_line}\n"
        f"Источник: {html_mod.escape(item['source'])}\n"
        f"{cov_line}"
        f"{trust_line}"
        f"{conf_line}"
        f"{why_line}"
        f"🔗 {html_mod.escape(item['url'])}\n"
        f"🕐 {pub}"
    )


def telegram_creds(cfg):
    # Секреты лучше хранить в .env, а не в config.yaml (см. env.example)
    tg = cfg.get("telegram") or {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id", "")
    return str(token or "").strip(), str(chat_id or "").strip()


# Кнопки под полем ввода в Telegram: нажатие отправляет боту текст кнопки
KEYBOARD = {
    "keyboard": [
        [{"text": "🏆 Топ"}, {"text": "🕘 История"}],
        [{"text": "🛰 Источники"}, {"text": "📊 Статус"}],
        [{"text": "📈 Качество"}, {"text": "🧪 Проверка"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}
BUTTONS = {
    "🏆 Топ": "/top",
    "🕘 История": "/history",
    "🛰 Источники": "/sources",
    "📊 Статус": "/status",
    "📈 Качество": "/quality",
    "🧪 Проверка": "/demo",
}


def telegram_chat_ids(cfg):
    """В TELEGRAM_CHAT_ID можно указать нескольких получателей через запятую."""
    _, raw = telegram_creds(cfg)
    return [x for x in re.split(r"[,\s;]+", raw) if x]


def _send_one(token, chat_id, text, preview, markup=None):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": not preview, "reply_markup": markup or KEYBOARD},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[warn] Telegram недоступен: {e}")
        return "stop"
    if r.status_code == 200:
        return "ok"
    print(f"[warn] Telegram вернул {r.status_code} для чата {chat_id}: {r.text[:160]}")
    return "stop" if r.status_code == 429 or r.status_code >= 500 else "skip"


def send_telegram(cfg, text, preview=True, to=None, markup=None):
    """None — бот не настроен; 'ok' — доставлено хотя бы одному получателю;
    'stop' — временный сбой (повторим позже); 'skip' — Telegram отклонил насовсем.
    to — отправить только в этот чат (так бот отвечает на команды)."""
    token, _ = telegram_creds(cfg)
    ids = [str(to)] if to else telegram_chat_ids(cfg)
    if not (token and ids):
        return None
    results = [_send_one(token, cid, text, preview, markup) for cid in ids]
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
        res = send_telegram(cfg, msg, markup=feedback_markup(it["url_hash"]))
        if res in (None, "ok", "skip"):
            delivered.add(it["url_hash"])
            if res == "ok":
                time.sleep(0.4)  # у Telegram лимит на частоту сообщений
        else:
            stopped = True
            print("[warn] Отправка прервана, остаток уйдёт при следующей проверке.")
    return delivered


def _lead_text(found_at):
    """«Радар заметил это за N мин до публикации в СМИ» (точность — шаг проверки источников)."""
    dt = parse_dt(found_at or "")
    if not dt:
        return ""
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    return f"⏱ Радар заметил это за {mins} мин до публикации в СМИ\n" if mins >= 1 else ""


def lead_stats(conn):
    """(сколько находок подхватили СМИ, среднее опережение в минутах, сколько ещё без СМИ у первоисточника)."""
    rows = conn.execute("SELECT found_at, media_at FROM items WHERE COALESCE(media_at, '') != ''").fetchall()
    leads = []
    for f, m in rows:
        a, b = parse_dt(f), parse_dt(m)
        if a and b and b >= a:
            leads.append((b - a).total_seconds() / 60)
    first = sum(1 for src, also in conn.execute("SELECT source, also_in FROM items WHERE score >= 35").fetchall()
                if coverage_status(src, split_also(also))[0] == "first")
    return len(leads), (sum(leads) / len(leads) if leads else 0.0), first


def send_confirmations(cfg, upgrades):
    """Дополнение к уже показанной новости: её подтвердил надёжный источник."""
    for old, by in upgrades[:5]:
        msg = (
            "✅ <b>Подтверждено надёжным источником</b>\n"
            f"<b>{html_mod.escape(shorten(old['title'], 180))}</b>\n\n"
            f"Первоисточник находки: {html_mod.escape(old['source'])}\n"
            f"Подтвердил: {html_mod.escape(by)}\n"
            f"{_lead_text(old.get('found_at'))}"
            f"🔗 {html_mod.escape(old['url'])}"
        )
        print("\n" + re.sub(r"<[^>]+>", "", html_mod.unescape(msg)))
        send_telegram(cfg, msg, preview=False)
        time.sleep(0.4)


def load_pending(conn, cfg):
    """Находки, которые пора показать редактору, но ещё не доставлены (новые + неотправленные раньше)."""
    since = now_iso(hours=cfg.get("pending_hours", 12))
    rows = conn.execute(
        """SELECT url_hash, url, source, title, summary, category, score, published,
                  confirmations, also_in, why, trust
           FROM items WHERE notified = 0 AND score >= ? AND found_at >= ?
           ORDER BY score DESC LIMIT ?""",
        (cfg.get("notify_min_score", 35), since, cfg.get("max_alerts_per_cycle", 10)),
    ).fetchall()
    keys = ["url_hash", "url", "source", "title", "summary", "category", "score", "published",
            "confirmations", "also_in", "why", "trust"]
    items = []
    for row in rows:
        d = dict(zip(keys, row))
        d["also_in"] = split_also(d["also_in"])
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
.legend{font-size:12px;color:#555;margin-top:6px}
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
            """SELECT found_at, score, category, source, title, url, confirmations, why, summary, trust
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
            f"<tr><td>{esc(_found_time(f))}</td><td>{badge(s)}</td><td>{esc(c or '')}</td>"
            f"<td>{trust_dot(tr)} {esc(src)}</td>"
            f"<td>{n}</td><td><a href='{esc(u, quote=True)}' target='_blank'>{esc(shorten(t, 140))}</a>"
            f"<div class='why'>{esc(w or '')}</div></td></tr>"
            for f, s, c, src, t, u, n, w, _summary, tr in rows
        )
        status_html = "".join(
            f"<tr><td>{'<span class=ok>✅</span>' if not st['error'] else '<span class=bad>⚠️</span>'}</td>"
            f"<td>{esc(name)}</td>"
            f"<td>{trust_dot(st.get('trust'))} {st.get('trust', 3)}/5 · {esc(KIND_RU.get(st.get('kind'), '—'))}</td>"
            f"<td>{st['count']}</td><td>{esc(st['time'])}</td>"
            f"<td class='bad'>{esc(st['error'] or '')}</td></tr>"
            for name, st in SOURCE_STATUS.items()
        ) or "<tr><td colspan=6>Ещё не было ни одной проверки</td></tr>"
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
<table><tr><th></th><th>Название</th><th>Доверие · тип</th><th>Записей</th><th>Проверен</th><th>Ошибка</th></tr>{status_html}</table>
<div class='legend'>🟢 надёжный источник · 🟡 средний уровень доверия · 🔴 «народный» канал: перепроверяйте</div></div>
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


class SingleServer(ThreadingHTTPServer):
    allow_reuse_address = False  # на Windows иначе две копии радара смогут занять один порт


def start_dashboard():
    try:
        server = SingleServer(("127.0.0.1", DASH_PORT), DashHandler)
    except OSError:
        print(f"[error] Порт {DASH_PORT} занят: радар уже запущен (в другом окне, в PyCharm или в фоне). "
              "Остановите его (красный квадрат в PyCharm или stop_radar.bat) и запустите снова.")
        return False
    Thread(target=server.serve_forever, daemon=True).start()
    print(f"[dashboard] Открыт на http://127.0.0.1:{DASH_PORT}")
    return True


# ----------------------------------------------------------------------------- #
#  Команды бота в Telegram: /top /history /latest /sources /status /demo /help
# ----------------------------------------------------------------------------- #

def _found_time(ts):
    """found_at хранится в UTC; показываем по времени Сургута."""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:  # запись без пояса: считаем, что это уже время Сургута
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return ts or ""


def _icon(score):
    return "🔥" if score >= 60 else ("❗" if score >= 45 else "ℹ️")


def _list_items(rows):
    esc = html_mod.escape
    blocks = []
    for found_at, score, category, source, title, url, conf, trust in rows:
        mark = f" · ✅×{conf}" if conf and conf > 1 else ""
        blocks.append(
            f"{_icon(score)} <b>{score:.0f}</b> · {esc(category or '—')}{mark}\n"
            f"<a href=\"{esc(url, quote=True)}\">{esc(shorten(title, 110))}</a>\n"
            f"<i>{trust_dot(trust)} {esc(source)} · {_found_time(found_at)}</i>"
        )
    return "\n\n".join(blocks)


BOT_COMMANDS = [
    ("top", "Топ новостей за сутки по баллам (/top 10)"),
    ("history", "Вся история по убыванию баллов (/history 15)"),
    ("latest", "Самые свежие находки по времени"),
    ("sources", "Источники, приоритеты, доверие и состояние"),
    ("demo", "Тестовое уведомление (проверка связи)"),
    ("quality", "Качество: оценки редактора 👍/👎"),
    ("status", "Состояние радара и настройки"),
    ("help", "Список команд"),
]

BOT_HELP = (
    "📡 <b>Новостной радар Сургута и Югры</b>\n"
    "Сам присылаю важные новости. А ещё умею:\n\n"
    + "\n".join(f"/{c} — {d}" for c, d in BOT_COMMANDS)
    + "\n\n🟢 надёжный источник · 🟡 средний · 🔴 «народный» канал, перепроверьте"
)


def bot_description(cfg):
    """Описание бота для пустого чата (как у EcoWatch): Telegram сам ставит над ним заголовок
    «Что умеет этот бот?», поэтому здесь его нет. Лимит Telegram: 512 символов, без форматирования."""
    every = cfg.get("interval_minutes", 15)
    return (
        "Бот для мониторинга новостей Сургута и Югры 📡\n\n"
        "С этим ботом вы можете:\n"
        f"- Получать важные новости каждые {every} минут 🔥\n"
        "- Видеть оценку важности от 0 до 100 📈\n"
        "- Понимать, можно ли верить источнику 🟢🟡🔴\n"
        "- Смотреть топ и историю находок 🕘\n"
        "- Отмечать новости 👍/👎: радар учится на ваших оценках 🧠"
    )


# Короткое описание: видно в профиле бота и при поиске (лимит 120 символов)
BOT_SHORT_DESCRIPTION = "Новостной радар Сургута и Югры: важное из десятков источников сразу в Telegram 📡"


def setup_bot_profile(token, cfg):
    """Прописывает описание бота в Telegram (то, что видно в пустом чате до нажатия «Начать»)."""
    api = f"https://api.telegram.org/bot{token}"
    ok = True
    for method, payload in (("setMyDescription", {"description": bot_description(cfg)}),
                            ("setMyShortDescription", {"short_description": BOT_SHORT_DESCRIPTION})):
        r = requests.post(f"{api}/{method}", json=payload, timeout=15)
        if r.status_code != 200:
            ok = False
            print(f"[warn] Описание бота не обновилось ({method}): {r.status_code} {r.text[:120]}")
    if ok:
        print("[bot] Описание бота обновлено (видно в пустом чате; старый чат нужно очистить)")
    return ok


def welcome_text(cfg):
    """Ответ на /start: то же описание, с заголовком."""
    return "<b>Что умеет этот бот?</b>\n\n" + html_mod.escape(bot_description(cfg))


def bot_action(cfg, cmd, asker):
    """Команды-действия (не просто показать данные). True — команда обработана."""
    if cmd == "/demo":
        send_demo(cfg, to=asker)  # проверка: тестовое уведомление тому, кто нажал
        return True
    return False


def bot_reply(cfg, command):
    """Готовит ответ на команду бота. Данные берём из той же базы, что и дашборд."""
    esc = html_mod.escape
    parts = command.split()
    cmd = parts[0].split("@")[0].lower()
    arg = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None  # например /top 10
    if cmd == "/start":
        return welcome_text(cfg)
    if cmd == "/help":
        return BOT_HELP
    if cmd == "/demo":
        return format_alert(make_demo_item(cfg))
    conn = db_connect()
    try:
        cols = "found_at, score, category, source, title, url, confirmations, trust"
        if cmd == "/top":
            n = min(arg or 5, 10)
            since = now_iso(hours=24)
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
                trust = src_trust(src)
                lines.append(
                    f"{icon} <b>{esc(src['name'])}</b>\n"
                    f"    {trust_dot(trust)} доверие {trust}/5 · приоритет {src.get('priority', 3)}/5 · {esc(info)}"
                )
            return "🛰 <b>Источники</b>\n\n" + "\n".join(lines)
        if cmd == "/quality":
            return quality_text(conn, cfg)
        if cmd == "/status":
            total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            min_score = cfg.get("notify_min_score", 35)
            important = conn.execute("SELECT COUNT(*) FROM items WHERE score >= ?", (min_score,)).fetchone()[0]
            enabled = sum(1 for s in cfg.get("sources", []) if s.get("enabled", True))
            n_lead, avg_lead, n_first = lead_stats(conn)
            lead_line = (f"\nОпережение СМИ: в среднем {avg_lead:.0f} мин (по {n_lead} находкам)" if n_lead else "")
            return (
                "📊 <b>Состояние радара</b>\n"
                f"Последняя проверка: {esc(LAST_CYCLE['time'] or '—')}\n"
                f"Проверка каждые {cfg.get('interval_minutes', 15)} мин, источников включено: {enabled}\n"
                f"Находок в истории: {total}, из них важных (от {min_score} баллов): {important}\n"
                f"🎯 Важных, о которых СМИ ещё не писали: {n_first}{lead_line}"
            )
    finally:
        conn.close()
    return "Не знаю такую команду. Список команд: /help"


GUEST_TEXT = (
    "🔒 <b>Это закрытый бот редакции.</b>\n"
    "Новостной радар присылает находки только сотрудникам, чьи чаты подключены администратором.\n\n"
    "Хотите получать уведомления? Передайте администратору ваш ID:\n"
    "<code>{chat_id}</code>"
)


def _send_plain(token, chat_id, text):
    """Обычное сообщение без кнопок (для чужих чатов: им кнопки радара не нужны)."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except requests.RequestException:
        pass


def bot_commands_loop(cfg):
    """Слушает команды редактора в Telegram. Отвечает только в чат из .env."""
    token, _ = telegram_creds(cfg)
    allowed = set(telegram_chat_ids(cfg))
    if token:
        try:
            setup_bot_profile(token, cfg)
        except requests.RequestException as e:
            print(f"[warn] Описание бота: не удалось подключиться: {e}")
    if not (token and allowed):
        return
    api = f"https://api.telegram.org/bot{token}"
    offset, warned = None, False
    told = {}  # когда последний раз объясняли чужому чату, что бот закрытый
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
            params = {"timeout": 30, "allowed_updates": '["message", "callback_query"]'}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{api}/getUpdates", params=params, timeout=45)
            if r.status_code != 200:
                if not warned:
                    if r.status_code == 409:
                        print("[warn] Команды бота: этот токен уже слушает ДРУГОЙ запущенный радар (ошибка 409). "
                              "Остановите лишние копии: красный квадрат в PyCharm или stop_radar.bat.")
                    else:
                        print(f"[warn] Команды бота: Telegram вернул {r.status_code}")
                    warned = True
                time.sleep(10)
                continue
            warned = False
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                if upd.get("callback_query"):  # нажали 👍/👎 под новостью
                    handle_feedback(token, allowed, upd["callback_query"])
                    continue
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                asker = str((msg.get("chat") or {}).get("id"))
                text = BUTTONS.get(text, text)  # нажатая кнопка = команда
                if asker not in allowed:
                    # Чужой чат (например, зашёл посмотреть жюри): вежливо объясняем и даём его ID.
                    # Не чаще раза в минуту, чтобы не отвечать на спам.
                    if asker != "None" and time.time() - told.get(asker, 0) > 60:
                        told[asker] = time.time()
                        guest = GUEST_TEXT.format(chat_id=asker)
                        if text.startswith("/start"):  # новичку на /start сначала показываем «что умеет бот»
                            guest = welcome_text(cfg) + "\n\n" + guest
                        _send_plain(token, asker, guest)
                        print(f"[bot] Чужой чат {asker} написал боту: ответили, что доступ закрыт. "
                              "Чтобы подключить его, добавьте этот ID в TELEGRAM_CHAT_ID (.env).")
                    continue
                if not text.startswith("/"):
                    continue  # обычные сообщения от своих игнорируем
                cmd = text.split()[0].split("@")[0].lower()
                if bot_action(cfg, cmd, asker):
                    continue
                send_telegram(cfg, bot_reply(cfg, text), preview=False, to=asker)
        except requests.RequestException:
            time.sleep(10)
        except Exception as e:
            print(f"[warn] Команды бота: {e}")
            time.sleep(10)


# ----------------------------------------------------------------------------- #
#  Цикл: сбор -> отбор -> уведомление
# ----------------------------------------------------------------------------- #

CYCLE_LOCK = Lock()


def run_cycle(cfg):
    """Один проход. Блокировка не даёт двум проверкам идти одновременно (по расписанию и из бота)."""
    with CYCLE_LOCK:
        return _run_cycle(cfg)


def _run_cycle(cfg):
    conn = db_connect()
    days = cfg.get("history_days", 7)
    threshold = cfg.get("dup_threshold", 75)
    min_score = cfg.get("notify_min_score", 35)
    baseline_on = cfg.get("baseline_on_first_run", True)
    seen_sources = {r[0] for r in conn.execute("SELECT name FROM seen_sources")}
    fb_stats = load_feedback_stats(conn)  # отзывы редактора 👍/👎 по источникам и темам
    new_sources = set()  # источники, которых радар раньше не видел
    new_items, upgrades, errors = [], [], 0

    for src, raw, err in fetch_all(cfg.get("sources", [])):
        stamp = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
        trust = src_trust(src)
        SOURCE_STATUS[src["name"]] = {"time": stamp, "count": len(raw), "error": err,
                                      "trust": trust, "kind": src.get("kind", "")}
        if err:
            errors += 1
            print(f"[warn] {src['name']}: {err}")
            continue
        print(f"[src] {src['name']}: получено записей {len(raw)}")
        if src["name"] not in seen_sources:
            new_sources.add(src["name"])
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
                    twin["trust"] = max(twin["trust"], trust)
                continue
            # ...или в истории за последние дни?
            old = find_similar_in_db(conn, raw_item["title"], days, threshold)
            if old:
                set_media = (src.get("kind") in MEDIA_KINDS and SOURCE_KINDS.get(old["source"]) not in MEDIA_KINDS
                             and not any(SOURCE_KINDS.get(x) in MEDIA_KINDS for x in split_also(old["also_in"])))
                if add_confirmation(conn, old, src["name"], trust, min_score, set_media):
                    upgrades.append((old, src["name"]))
                continue
            score, cat, why = score_item(
                {**raw_item, "source_priority": src.get("priority", 3),
                 "geo_hint": src.get("geo_hint", ""), "source_trust": trust}, cfg
            )
            fb_adj, fb_why = feedback_adjust(fb_stats, src["name"], cat, cfg)
            if fb_adj:
                score = round(min(max(score + fb_adj, 0), 100), 1)
                why += f"; отзывы редактора: {fb_why}"
            new_items.append({
                "url_hash": url_hash,
                "url": raw_item["url"],
                "source": src["name"],
                "title": raw_item["title"],
                "summary": make_summary(raw_item["title"], raw_item["summary"], cfg)
                           or shorten(raw_item["summary"], 280),
                "category": cat,
                "score": score,
                "why": why,
                "published": raw_item["published"],
                "confirmations": 1,
                "also_in": [],
                "trust": trust,
            })

    # Чем больше независимых источников написали об этом, тем важнее и достовернее
    for it in new_items:
        if it["confirmations"] > 1:
            bonus = 6 * (it["confirmations"] - 1)
            it["score"] = min(100, it["score"] + bonus)
            it["why"] += f"; подтверждено {it['confirmations']} источниками +{bonus}"

    # Новый источник (или первый запуск): всё, что лежит там сейчас, не «свежее», а просто
    # накопленное. Запоминаем это тихо, чтобы редактору не прилетело 10 старых новостей.
    silent_sources = new_sources if baseline_on else set()
    if silent_sources and new_items:
        print(f"[init] Новые источники ({len(silent_sources)}): запоминаем текущие записи без уведомлений.")
    for it in new_items:
        silent = all(s in silent_sources for s in [it["source"], *it["also_in"]])
        important = it["score"] >= min_score
        # notified: 0 = надо показать редактору, 1 = показано/не требуется, 2 = важное, но тихо (baseline)
        it["notified"] = 0 if (important and not silent) else (2 if important else 1)

    purge_old(conn, days)
    for it in new_items:
        save_item(conn, it)
    conn.executemany("INSERT OR IGNORE INTO seen_sources (name) VALUES (?)", [(n,) for n in new_sources])
    conn.commit()

    sent = deliver_pending(conn, cfg)
    conn.close()
    if upgrades:
        send_confirmations(cfg, upgrades)

    LAST_CYCLE["time"] = datetime.now(LOCAL_TZ).strftime("%d.%m %H:%M:%S")
    print(f"\n[cycle] {datetime.now():%H:%M} | новых записей: {len(new_items)} | "
          f"уведомлений: {sent} | подтверждений: {len(upgrades)} | ошибок источников: {errors}")
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
    score, cat, why = score_item(
        {**raw, "source_priority": 5, "geo_hint": "сургут", "source_trust": 4}, cfg
    )
    return {
        "url_hash": "demo", "url": raw["url"], "source": DEMO_SOURCE, "title": raw["title"],
        "summary": raw["summary"], "category": cat, "score": score, "why": why,
        "published": raw["published"], "confirmations": 1, "also_in": [], "trust": 4,
    }


def send_demo(cfg, to=None):
    """Проверка связи: тестовое уведомление (в историю не попадает). to — только этому чату."""
    msg = format_alert(make_demo_item(cfg))
    print("\n" + re.sub(r"<[^>]+>", "", html_mod.unescape(msg)))
    return send_telegram(cfg, msg, to=to, markup=feedback_markup("demo"))


def run_demo(cfg):
    """Отправляет тестовую новость всем получателям. В историю и дашборд она не попадает."""
    notify(cfg, [make_demo_item(cfg)])
    print("[demo] Готово. В историю демо-новость не сохраняется.")


def run_test_telegram(cfg):
    token, chat_id = telegram_creds(cfg)
    if not (token and chat_id):
        print("[test] Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID (файл .env).")
        return
    try:
        setup_bot_profile(token, cfg)
    except requests.RequestException as e:
        print(f"[warn] Описание бота: не удалось подключиться: {e}")
    res = send_telegram(cfg, "✅ <b>Новостной радар подключён.</b>\nСюда будут приходить важные новости Сургута и Югры.")
    print("[test] Готово: проверьте Telegram." if res == "ok" else "[test] Не получилось, причина в строке [warn] выше.")


# ----------------------------------------------------------------------------- #
#  Режимы запуска: вместо команд в терминале вводится цифра
# ----------------------------------------------------------------------------- #

MODES = {
    "1": "ЗАПУСТИТЬ РАДАР в фоне: проверки каждые N минут + бот в Telegram + дашборд (достаточно один раз)",
    "2": "ДЕМО: прислать в Telegram тестовую «горячую» новость (в историю не попадает)",
    "3": "ТЕСТ TELEGRAM: отправить «радар подключён» (проверка токена и chat_id из .env)",
    "4": "ОДНА ПРОВЕРКА СЕЙЧАС: сразу опросить источники и показать результат",
    "5": "СБРОС ИСТОРИИ: стереть базу находок (после этого радар начнёт с чистого листа)",
    "6": "ПРОВЕРКА ИСТОЧНИКОВ: какие сайты и каналы реально отдают новости (без уведомлений)",
}

RADAR = {"running": False}


def radar_loop(cfg):
    """Проверки по расписанию, пока программа открыта."""
    while True:
        try:
            run_cycle(cfg)
        except Exception as e:
            print(f"[error] сбой цикла: {e}")
        time.sleep(int(cfg.get("interval_minutes", 15)) * 60)


def start_radar(cfg):
    """Запускает радар в фоне (один раз): дашборд, команды бота и проверки по расписанию."""
    if RADAR["running"]:
        print("[radar] Радар уже работает.")
        return True
    if not start_dashboard():
        return False
    Thread(target=bot_commands_loop, args=(cfg,), daemon=True).start()
    Thread(target=radar_loop, args=(cfg,), daemon=True).start()
    RADAR["running"] = True
    return True


def clear_history():
    conn = db_connect()
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM seen_sources")  # после сброса источники снова «новые»: первый заход тихий
    conn.commit()
    conn.close()
    print("[reset] История стёрта.")


def do_action(cfg, mode):
    if mode == "1":
        start_radar(cfg)
    elif mode == "2":
        run_demo(cfg)
    elif mode == "3":
        run_test_telegram(cfg)
    elif mode == "4":
        run_cycle(cfg)
    elif mode == "5":
        clear_history()
    elif mode == "6":
        check_sources(cfg)


def show_menu():
    state = "работает ✅" if RADAR["running"] else "не запущен (введите 1)"
    print(f"\n=== Новостной радар Сургута и Югры · радар: {state} ===")
    for num, text in MODES.items():
        print(f"  {num} — {text}")
    print("  0 — выход (радар остановится)")


def interactive(cfg, first_mode=None):
    """Меню, которое не закрывается: после каждого действия появляется снова. Выход только цифрой 0."""
    if first_mode:
        do_action(cfg, first_mode)
    while sys.stdin is not None:
        show_menu()
        try:
            choice = input("Введите цифру и нажмите Enter: ").strip()
        except EOFError:
            break
        if choice == "0":
            print("[stop] Остановлено.")
            return
        if choice not in MODES:
            print("Нет такой цифры, попробуйте ещё раз.")
            continue
        if choice == "5" and input("Точно стереть всю историю? (да/нет): ").strip().lower() not in ("да", "y", "yes"):
            print("Отменено.")
            continue
        do_action(cfg, choice)
    # Ввода с клавиатуры нет (фоновый запуск без окна): просто работаем, меню не нужно
    if not start_radar(cfg):
        sys.exit(1)
    while True:
        time.sleep(3600)


def main():
    parser = argparse.ArgumentParser(description="Новостной радар Сургута и Югры")
    parser.add_argument("mode", nargs="?", choices=list(MODES), help="цифра режима (см. меню в начале файла)")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--dashboard", action="store_true", help="поднять веб-дашборд")
    parser.add_argument("--demo", action="store_true", help="подбросить тестовую горячую новость")
    parser.add_argument("--test-telegram", action="store_true", help="отправить тестовое сообщение в Telegram")
    parser.add_argument("--check-sources", action="store_true", help="проверить, какие источники отдают новости")
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

    # Запуск без флагов (или с цифрой): меню, которое не закрывается.
    # Старые флаги (--dashboard, --once, --demo ...) работают как раньше; ими пользуется start_radar.bat.
    if args.mode or len(sys.argv) == 1:
        try:
            interactive(cfg, args.mode)
        except KeyboardInterrupt:
            print("\n[stop] Остановлено.")
        return

    if args.check_sources:
        check_sources(cfg)
        return
    if args.test_telegram:
        run_test_telegram(cfg)
        return
    if args.demo:
        run_demo(cfg)
        return
    if args.dashboard and not start_dashboard():
        sys.exit(1)
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
        radar_loop(cfg)
    except KeyboardInterrupt:
        print("\n[stop] Остановлено.")
        sys.exit(0)


if __name__ == "__main__":
    main()
