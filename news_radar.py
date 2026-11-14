#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
НОВОСТНОЙ РАДАР СУРГУТА И ЮГРЫ — прототип для хакатона СИА-ПРЕСС.

Пайплайн: Сбор -> Отбор -> Уведомление (+ история находок в SQLite и веб-дашборд).

Запуск:
    pip install -r requirements.txt
    python news_radar.py --once          # один проход (для теста)
    python news_radar.py                 # цикл: проверка каждые N минут
    python news_radar.py --dashboard     # + веб-дашборд на http://localhost:8900

Настройки — в config.yaml (создаётся автоматически при первом запуске).
"""

import argparse
import hashlib
import html as html_mod
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urljoin

import requests
import yaml

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

DEFAULT_CONFIG = r"""
# ===== Настройки новостного радара =====

# Как часто проверять источники, минут
interval_minutes: 15

# Минимальный балл, начиная с которого редактору приходит уведомление
notify_min_score: 35

# Сколько дней хранить и учитывать историю при удалении дублей
history_days: 7

# Уникальных находок в одном уведомлении (анти-спам пачками)
max_alerts_per_cycle: 10

# Telegram-бот: создайте бота через @BotFather, вставьте токен и chat_id
# Пока токена нет — находки печатаются в консоль и видны на дашборде.
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
      - {word: "погиб", w: 14}
      - {word: "пострадал", w: 12}
      - {word: "пропал", w: 10}
      - {word: "угроза", w: 8}
      - {word: "отключен", w: 7}
      - {word: "энергосб", w: 7}
      - {word: "разлив нефт", w: 12}
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

# Источники. type: rss | html | telegram
# Приоритет источника 1..5 — множитель важности (фактически добавляет priority*3 к баллу).
sources:
  - name: СИА-ПРЕСС (RSS)
    type: rss
    url: https://siapress.ru/rss
    priority: 5
    enabled: true

  - name: Администрация Сургута
    type: html
    url: https://www.admsurgut.ru/press/news/
    item_selector: ".news-list-item a, .news-item a, a[href*='/news/']"
    priority: 5
    enabled: true

  - name: Правительство ХМАО — Югры
    type: html
    url: https://www.admhmao.ru/press/news/
    item_selector: ".news-item a, a[href*='/press/']"
    priority: 4
    enabled: true

  - name: Мэрия Сургута (Telegram)
    type: telegram
    url: https://t.me/s/admsurgut
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
            found_at TEXT
        )
    """)
    conn.commit()
    return conn


def normalize_title(t):
    t = unicodedata.normalize("NFKC", (t or "")).lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def make_hash(url, title):
    return hashlib.sha256(f"{(url or '').split('#')[0]}|{normalize_title(title)}".encode()).hexdigest()


def is_duplicate(conn, url_hash, title, history_days):
    """Точный дубль по хэшу ИЛИ очень похожий заголовок за последние N дней."""
    cur = conn.execute("SELECT 1 FROM items WHERE url_hash = ?", (url_hash,))
    if cur.fetchone():
        return True
    since = (datetime.now() - timedelta(days=history_days)).isoformat(timespec="seconds")
    cur = conn.execute("SELECT title FROM items WHERE found_at >= ?", (since,))
    norm = normalize_title(title)
    for (old,) in cur:
        if old and SequenceMatcher(None, norm, normalize_title(old)).ratio() >= 0.85:
            return True
    return False


def save_item(conn, item):
    conn.execute(
        """INSERT OR IGNORE INTO items
           (url_hash, url, source, title, summary, category, score, published, found_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item["url_hash"], item["url"], item["source"], item["title"],
            item["summary"], item["category"], item["score"],
            item["published"], datetime.now().isoformat(timespec="seconds"),
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

def _clean(text):
    return re.sub(r"\s+", " ", (text or "").strip())


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
    resp = session.get(source["url"], timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml" if "lxml" in (BeautifulSoup.__mro__[0].__module__ or "") else "html.parser")
    items = []
    selector = source.get("item_selector") or "a"
    for a in soup.select(selector)[:40]:
        title = _clean(a.get_text())
        href = a.get("href", "")
        if not title or len(title) < 15 or not href:
            continue
        link = urljoin(source["url"], href)
        # Небольшая выжимка: соседний текст с датой/анонсом, если есть
        parent_text = _clean(a.find_parent().get_text(" ", strip=True)) if a.parent else title
        summary = parent_text[:400]
        items.append({"title": title, "url": link, "summary": summary, "published": ""})
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
        text = _clean(text_node.get_text(" ", strip=True))
        time_tag = msg.select_one("time[datetime]")
        link_node = msg.select_one("a.tgme_widget_message_date")
        link = urljoin("https://t.me/", link_node["href"]) if link_node and link_node.get("href") else source["url"]
        published = time_tag["datetime"] if time_tag else ""
        title = text[:150]
        if title:
            items.append({"title": title, "url": link, "summary": text[:600], "published": published})
    return items


FETCHERS = {"rss": fetch_rss, "html": fetch_html, "telegram": fetch_telegram}


# ----------------------------------------------------------------------------- #
#  2. ОТБОР: гео + темы + оценка важности
# ----------------------------------------------------------------------------- #

def match_geo(text, geo_weights):
    low = text.lower()
    best = 0
    for geo, w in geo_weights.items():
        if geo and geo in low:
            best = max(best, w)
    return best


def match_topics(text, categories):
    low = text.lower()
    best_cat, best_score = "Общий поток", 0
    for cat, spec in categories.items():
        cat_score = sum(min(w, 15) for w in (word["w"] for word in spec.get("words", []) if word["word"] in low))
        if cat_score > best_score:
            best_cat, best_score = cat, cat_score * spec.get("weight", 1.0)
    return best_cat, min(best_score, 60)


def score_item(item, cfg):
    text = f"{item['title']} {item['summary']}"
    geo = match_geo(text, cfg.get("geo_weights", {}))
    cat, topic_score = match_topics(text, cfg.get("categories", {}))
    priority_boost = 3 * int(item.get("source_priority", 3))
    fresh_bonus = 0
    if item.get("published"):
        try:
            dt = datetime.fromisoformat(item["published"].replace("Z", "+00:00")).replace(tzinfo=None)
            hours = (datetime.now() - dt).total_seconds() / 3600
            if hours < 0:
                hours = 0
            fresh_bonus = 10 if hours <= 2 else (5 if hours <= 6 else 0)
        except ValueError:
            pass
    score = geo + topic_score + priority_boost + fresh_bonus
    if geo == 0:
        score = min(score, 20)  # без привязки к региону выше «фонового» не поднимаем
    return round(score, 1), cat


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
    """ISO-строку -> '14.11 11:20'; непонятный формат отдаём как есть."""
    if not ts:
        return datetime.now().strftime("%d.%m %H:%M")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
        return dt.strftime("%d.%m %H:%M")
    except ValueError:
        return ts


def format_alert(item):
    icon = "🔥" if item["score"] >= 60 else ("❗" if item["score"] >= 45 else "ℹ️")
    pub = _human_time(item.get("published", ""))
    return (
        f"{icon} <b>{item['score']:.0f} {_plural(item['score'], 'балл', 'балла', 'баллов')}</b> · {html_mod.escape(item['category'])}\n"
        f"<b>{html_mod.escape(item['title'][:180])}</b>\n"
        f"{html_mod.escape((item['summary'] or '')[:300])}\n\n"
        f"Источник: {html_mod.escape(item['source'])}\n"
        f"🔗 {item['url']}\n"
        f"🕐 {pub}"
    )


def notify(cfg, items):
    tg = cfg.get("telegram") or {}
    token, chat_id = tg.get("bot_token", ""), tg.get("chat_id", "")
    for it in items:
        msg = format_alert(it)
        if token and chat_id:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": False},
                    timeout=15,
                )
                if r.status_code != 200:
                    print(f"[warn] Telegram вернул {r.status_code}: {r.text[:120]}")
            except requests.RequestException as e:
                print(f"[warn] Telegram недоступен: {e}")
        print("\n" + re.sub(r"<[^>]+>", "", msg))  # дубль в консоль всегда


# ----------------------------------------------------------------------------- #
#  Веб-дашборд (история находок)
# ----------------------------------------------------------------------------- #

DASH_PORT = 8900

class DashHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        conn = db_connect()
        rows = conn.execute(
            "SELECT found_at, score, category, source, title, url FROM items ORDER BY score DESC, found_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
        rows_html = "".join(
            f"<tr><td>{f}</td><td><b>{s:.0f}</b></td><td>{c}</td><td>{src}</td>"
            f"<td><a href='{u}'>{t[:120]}</a></td></tr>"
            for f, s, c, src, t, u in rows
        )
        page = f"""<!doctype html><html lang='ru'><meta charset='utf-8'>
        <title>Радар Сургута и Югры</title>
        <style>body{{font-family:sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%}}
        td,th{{border:1px solid #ddd;padding:6px 10px;font-size:14px}}th{{background:#eee}}</style>
        <h2>📡 Новостной радар: Сургут — приоритет №1, далее вся Югра</h2>
        <p>История находок, отсортирована по баллу важности. Обновите страницу после цикла сбора.</p>
        <table><tr><th>Найдено</th><th>Балл</th><th>Тема</th><th>Источник</th><th>Заголовок / ссылка</th></tr>{rows_html}</table>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def log_message(self, *a):
        pass


def start_dashboard():
    Thread(target=lambda: HTTPServer(("127.0.0.1", DASH_PORT), DashHandler).serve_forever(), daemon=True).start()
    print(f"[dashboard] Открыт на http://127.0.0.1:{DASH_PORT}")


# ----------------------------------------------------------------------------- #
#  Цикл: сбор -> отбор -> уведомление
# ----------------------------------------------------------------------------- #

def run_cycle(cfg):
    conn = db_connect()
    session = requests.Session()
    session.headers["User-Agent"] = "SIA-Press News Radar/1.0 (hackathon prototype)"
    new_items, errors = [], 0

    for src in cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        fetch = FETCHERS.get(src["type"])
        if not fetch:
            print(f"[warn] неизвестный тип источника: {src['type']}")
            continue
        try:
            raw = fetch(src, session)
        except Exception as e:
            errors += 1
            print(f"[warn] {src['name']}: {e}")
            continue
        for raw_item in raw:
            url_hash = make_hash(raw_item["url"], raw_item["title"])
            if is_duplicate(conn, url_hash, raw_item["title"], cfg.get("history_days", 7)):
                continue
            score, cat = score_item({**raw_item, "source_priority": src.get("priority", 3)}, cfg)
            new_items.append({
                "url_hash": url_hash,
                "url": raw_item["url"],
                "source": src["name"],
                "title": raw_item["title"],
                "summary": raw_item["summary"],
                "category": cat,
                "score": score,
                "published": raw_item["published"],
            })

    purge_old(conn, cfg.get("history_days", 7))
    to_alert = sorted(
        [i for i in new_items if i["score"] >= cfg.get("notify_min_score", 35)],
        key=lambda x: -x["score"],
    )[: cfg.get("max_alerts_per_cycle", 10)]

    for it in new_items:
        save_item(conn, it)
    conn.close()

    if to_alert:
        notify(cfg, to_alert)

    print(f"\n[cycle] {datetime.now():%H:%M} | новых записей: {len(new_items)} | "
          f"на уведомление: {len(to_alert)} | ошибок источников: {errors}")
    return len(new_items), len(to_alert), errors


def main():
    parser = argparse.ArgumentParser(description="Новостной радар Сургута и Югры")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--dashboard", action="store_true", help="поднять веб-дашборд")
    args = parser.parse_args()

    cfg = load_config()
    if args.dashboard:
        start_dashboard()

    if args.once:
        run_cycle(cfg)
        return

    while True:
        try:
            run_cycle(cfg)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print(f"[error] сбой цикла: {e}")
        time.sleep(int(cfg.get("interval_minutes", 15)) * 60)


if __name__ == "__main__":
    main()
