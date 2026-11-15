#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
НОВОСТНОЙ РАДАР СУРГУТА И ЮГРЫ: графический интерфейс (Qt / PySide6).

Заменяет консольное меню из news_radar.py. Сам радар (сбор, отбор, оценка,
Telegram) остаётся в news_radar.py: этот файл только показывает его работу красиво.

КАК ЗАПУСТИТЬ
    1. pip install -r requirements.txt   (поставится и PySide6)
    2. Положите этот файл в ту же папку, где лежат news_radar.py и config.yaml
    3. Запустите radar_gui.py (кнопка Run в PyCharm)

ЧТО ВНУТРИ
    Лента находок  : карточки новостей с баллом, источником, достоверностью и «почему такой балл»
    Источники      : какие каналы и сайты подключены и живы ли они
    Журнал         : всё, что раньше печаталось в консоли, плюс демо и тест Telegram
"""

import sqlite3
import sys
import threading
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSlider,
    QStackedWidget, QVBoxLayout, QWidget,
)

import news_radar as nr  # сам радар: его функции мы вызываем из окна

# ----------------------------------------------------------------------------- #
#  Оформление (тёмная тема в красках СИА-ПРЕСС). Меняйте цвета здесь.
# ----------------------------------------------------------------------------- #

COLORS = {
    "@BG@": "#0e1015",
    "@PANEL@": "#14171e",
    "@CARD@": "#1a1e27",
    "@CHIP@": "#252a36",
    "@BORDER@": "#252b37",
    "@TEXT@": "#e8eaf0",
    "@MUTED@": "#8a92a6",
    "@ACCENT@": "#e0301e",
    "@ACCENT_SOFT@": "#2a1816",
}

QSS = """
* { font-family: "Segoe UI", "Inter", "Roboto", sans-serif; font-size: 13px; color: @TEXT@; }
QMainWindow, QWidget#root { background: @BG@; }
QWidget#sidebar { background: @PANEL@; border-right: 1px solid @BORDER@; }
QLabel#logoMark { background: @ACCENT@; color: white; font-weight: 800; font-size: 16px; border-radius: 11px; }
QLabel#appTitle { font-size: 15px; font-weight: 700; }
QLabel#appSub { color: @MUTED@; font-size: 12px; }
QLabel#tgChip { background: @CARD@; border: 1px solid @BORDER@; border-radius: 10px; padding: 8px 10px; font-size: 12px; }

QPushButton#nav { text-align: left; padding: 11px 14px; border: none; border-radius: 10px;
                  color: @MUTED@; font-weight: 600; background: transparent; }
QPushButton#nav:hover { background: @CARD@; color: @TEXT@; }
QPushButton#nav:checked { background: @ACCENT_SOFT@; color: @TEXT@; }

QLabel#pageTitle { font-size: 26px; font-weight: 800; }
QLabel#pageSub { color: @MUTED@; font-size: 13px; }

QFrame#stat { background: @CARD@; border: 1px solid @BORDER@; border-radius: 14px; }
QLabel#statTitle { color: @MUTED@; font-size: 12px; }
QLabel#statValue { font-size: 26px; font-weight: 800; }

QFrame#card { background: @CARD@; border: 1px solid @BORDER@; border-radius: 14px; }
QFrame#card:hover { border: 1px solid @ACCENT@; }
QLabel#cardTitle { font-size: 15px; font-weight: 700; }
QLabel#muted { color: @MUTED@; font-size: 12px; }
QLabel#chip { background: @CHIP@; border-radius: 8px; padding: 3px 9px; font-size: 12px; }
QLabel#chipOk { background: rgba(46,204,113,0.16); color: #4be08b; border-radius: 8px; padding: 3px 9px; font-size: 12px; }
QLabel#chipDemo { background: @ACCENT@; color: white; border-radius: 8px; padding: 3px 9px; font-size: 12px; font-weight: 700; }
QLabel#empty { color: @MUTED@; font-size: 15px; padding: 60px; }

QPushButton#primary { background: @ACCENT@; color: white; border: none; border-radius: 12px;
                      padding: 12px 24px; font-weight: 700; font-size: 14px; }
QPushButton#primary:hover { background: #f0432f; }
QPushButton#ghost { background: @CARD@; border: 1px solid @BORDER@; border-radius: 10px;
                    padding: 9px 16px; font-weight: 600; }
QPushButton#ghost:hover { border-color: @ACCENT@; }
QPushButton#ghost:disabled { color: @MUTED@; border-color: @BORDER@; }
QPushButton#danger { background: @CARD@; border: 1px solid #5a2a26; border-radius: 10px;
                     padding: 9px 16px; font-weight: 600; color: #ff7a6b; }
QPushButton#danger:hover { background: #2a1816; }
QPushButton#small { background: @CHIP@; border: none; border-radius: 8px; padding: 6px 12px; font-weight: 600; font-size: 12px; }
QPushButton#small:hover { background: @ACCENT@; color: white; }

QLabel#live { background: rgba(46,204,113,0.10); border: 1px solid rgba(46,204,113,0.45);
              border-radius: 12px; padding: 11px 18px; font-weight: 700; }

QLineEdit, QComboBox { background: @CARD@; border: 1px solid @BORDER@; border-radius: 10px; padding: 9px 12px; }
QLineEdit:focus, QComboBox:focus { border-color: @ACCENT@; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: @CARD@; border: 1px solid @BORDER@;
                              selection-background-color: @ACCENT@; outline: none; }
QSlider::groove:horizontal { height: 6px; background: @BORDER@; border-radius: 3px; }
QSlider::sub-page:horizontal { background: @ACCENT@; border-radius: 3px; }
QSlider::handle:horizontal { background: white; width: 16px; margin: -6px 0; border-radius: 8px; }

QPlainTextEdit#log { background: #0a0c10; border: 1px solid @BORDER@; border-radius: 12px; padding: 10px;
                     font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px; color: #c9d1e0; }

QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @BORDER@; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #3a4150; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QStatusBar { background: @PANEL@; color: @MUTED@; border-top: 1px solid @BORDER@; }
QToolTip { background: @CARD@; color: @TEXT@; border: 1px solid @BORDER@; padding: 6px; }
QMessageBox { background: @PANEL@; }
"""
for _k, _v in COLORS.items():
    QSS = QSS.replace(_k, _v)

PAGES = [
    ("Лента находок", "Новости, которые радар счёл важными для Сургута и Югры"),
    ("Источники", "Откуда радар берёт новости и живы ли эти источники"),
    ("Журнал", "Всё, что делает радар, и ручные действия"),
]


# ----------------------------------------------------------------------------- #
#  Служебные классы
# ----------------------------------------------------------------------------- #

class LogStream(QObject):
    """Подменяет консоль: всё, что news_radar печатает через print, попадает в окно «Журнал»."""
    text = Signal(str)

    def write(self, s):
        try:
            if s:
                self.text.emit(str(s))
        except RuntimeError:  # окно уже закрыто
            pass
        return len(s or "")

    def flush(self):
        pass

    def isatty(self):
        return False


class Bridge(QObject):
    """Мост: фоновый поток сообщает окну, что работа закончилась."""
    done = Signal(str, object, object)  # название задачи, результат, ошибка


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


def make_label(text="", name=None, wrap=False):
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


# ----------------------------------------------------------------------------- #
#  Чтение истории находок из базы radar_history.db
# ----------------------------------------------------------------------------- #

def read_items(min_score, query, mode):
    """Находки из базы. mode 0: сначала новые, 1: сначала важные."""
    try:
        conn = sqlite3.connect(nr.DB_PATH)
        conn.row_factory = sqlite3.Row
        order = "found_at DESC" if mode == 0 else "score DESC, found_at DESC"
        cur = conn.execute(f"SELECT * FROM items WHERE score >= ? ORDER BY {order} LIMIT 600", (min_score,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except sqlite3.Error:
        return []
    if query:
        rows = [r for r in rows
                if query in " ".join(str(r.get(k) or "") for k in ("title", "source", "category", "summary")).lower()]
    return rows[:150]


def read_stats(threshold):
    """(всего находок, важных, за последние сутки)"""
    try:
        conn = sqlite3.connect(nr.DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        important = conn.execute("SELECT COUNT(*) FROM items WHERE score >= ?", (threshold,)).fetchone()[0]
        since = nr.now_iso(hours=24)
        day = conn.execute("SELECT COUNT(*) FROM items WHERE found_at >= ?", (since,)).fetchone()[0]
        conn.close()
        return total, important, day
    except sqlite3.Error:
        return 0, 0, 0


# ----------------------------------------------------------------------------- #
#  Виджеты-карточки
# ----------------------------------------------------------------------------- #

class ScoreBadge(QFrame):
    """Цветной квадрат с баллом важности."""

    def __init__(self, score):
        super().__init__()
        if score >= 60:
            color, cap = "#e0301e", "ГОРЯЧЕЕ"
        elif score >= 45:
            color, cap = "#d9822b", "ВАЖНОЕ"
        elif score >= 35:
            color, cap = "#2f6fe0", "ВНИМАНИЕ"
        else:
            color, cap = "#3a4150", "ФОН"
        self.setObjectName("badge")
        self.setStyleSheet(f"QFrame#badge {{ background: {color}; border-radius: 12px; }}")
        self.setFixedSize(70, 64)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 6, 2, 6)
        lay.setSpacing(0)
        num = QLabel(f"{score:.0f}")
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setStyleSheet("background: transparent; color: white; font-size: 24px; font-weight: 800;")
        txt = QLabel(cap)
        txt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        txt.setStyleSheet("background: transparent; color: rgba(255,255,255,0.85); font-size: 9px; font-weight: 700;")
        lay.addWidget(num)
        lay.addWidget(txt)


class NewsCard(QFrame):
    """Карточка одной находки."""

    def __init__(self, row, demo=False):
        super().__init__()
        self.setObjectName("card")
        score = float(row.get("score") or 0)
        trust = row.get("trust") or 3
        conf = row.get("confirmations") or 1
        also = row.get("also_in") or ""
        if isinstance(also, list):
            also = ", ".join(also)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(16)
        lay.addWidget(ScoreBadge(score), 0, Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(7)
        body.addWidget(make_label(row.get("title") or "", "cardTitle", wrap=True))

        meta = QHBoxLayout()
        meta.setSpacing(8)
        if demo:
            meta.addWidget(make_label("ДЕМО", "chipDemo"))
        meta.addWidget(make_label(row.get("category") or "Общий поток", "chip"))
        if conf and conf > 1:
            meta.addWidget(make_label(f"✓ подтверждено ×{conf}", "chipOk"))
        found = nr._found_time(row.get("found_at")) if row.get("found_at") else nr._human_time(row.get("published"))
        meta.addWidget(make_label(f"{row.get('source') or ''}  ·  {found}", "muted"))
        meta.addStretch(1)
        body.addLayout(meta)

        body.addWidget(make_label(nr.verification_text(trust, conf), "muted"))
        if also:
            body.addWidget(make_label(f"Также писали: {also}", "muted", wrap=True))
        if row.get("why"):
            body.addWidget(make_label("Почему такой балл: " + nr.shorten(row["why"], 220), "muted", wrap=True))

        btns = QHBoxLayout()
        btns.setSpacing(8)
        open_btn = QPushButton("Открыть новость")
        open_btn.setObjectName("small")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        url = row.get("url") or ""
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        copy_btn = QPushButton("Копировать заголовок")
        copy_btn.setObjectName("small")
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        title = row.get("title") or ""
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(title))
        btns.addWidget(open_btn)
        btns.addWidget(copy_btn)
        btns.addStretch(1)
        body.addLayout(btns)

        lay.addLayout(body, 1)


class StatCard(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("stat")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(2)
        lay.addWidget(make_label(title, "statTitle"))
        self.value = make_label("0", "statValue")
        lay.addWidget(self.value)

    def set_value(self, text):
        self.value.setText(str(text))


# ----------------------------------------------------------------------------- #
#  Главное окно
# ----------------------------------------------------------------------------- #

class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.sources = cfg.get("sources", []) or []
        self.threshold = int(cfg.get("notify_min_score", 35))
        self.interval = int(cfg.get("interval_minutes", 15))
        self.demo_items = []
        self.busy = set()
        self.job_buttons = {}
        self.source_rows = []
        self._feed_sig = None
        self._pulse_on = True

        self.setWindowTitle("Новостной радар · Сургут и Югра")
        self.resize(1320, 820)
        self.setMinimumSize(1020, 660)

        self.bridge = Bridge()
        self.bridge.done.connect(self.on_job_done)

        self.build_ui()

        # Всё, что печатает news_radar, теперь летит в окно «Журнал»
        self.stream = LogStream()
        self.stream.text.connect(self.append_log)
        self._old_streams = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = self.stream

        try:
            nr.db_connect().close()  # создаёт базу, если её ещё нет
        except Exception as e:
            print(f"[error] база данных: {e}")

        self.update_running_ui()
        self.refresh_all()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start(4000)
        self.pulse = QTimer(self)
        self.pulse.timeout.connect(self.pulse_tick)
        self.pulse.start(800)

        print("Интерфейс готов. Нажмите «Запустить радар», чтобы начать следить за источниками.")

    # ------------------------------------------------------------------ интерфейс

    def build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.build_sidebar())

        main = QVBoxLayout()
        main.setContentsMargins(28, 24, 28, 18)
        main.setSpacing(16)

        # --- шапка: заголовок страницы и главные кнопки
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.page_title = make_label(PAGES[0][0], "pageTitle")
        self.page_sub = make_label(PAGES[0][1], "pageSub")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_sub)
        head.addLayout(titles, 1)

        self.cycle_btn = QPushButton("Проверить сейчас")
        self.cycle_btn.setObjectName("ghost")
        self.cycle_btn.setToolTip("Сразу опросить все источники один раз")
        self.cycle_btn.clicked.connect(self.on_cycle)
        self.job_buttons["cycle"] = [self.cycle_btn]
        head.addWidget(self.cycle_btn)

        self.start_btn = QPushButton("▶  Запустить радар")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.on_start)
        head.addWidget(self.start_btn)
        self.live = QLabel()
        self.live.setObjectName("live")
        head.addWidget(self.live)
        main.addLayout(head)

        # --- плитки со статистикой
        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.st_total = StatCard("Находок в базе")
        self.st_day = StatCard("За последние сутки")
        self.st_imp = StatCard(f"Важных (от {self.threshold} баллов)")
        self.st_src = StatCard("Источников работает")
        self.st_last = StatCard("Последняя проверка")
        for w in (self.st_total, self.st_day, self.st_imp, self.st_src, self.st_last):
            stats.addWidget(w)
        main.addLayout(stats)

        # --- страницы
        self.stack = QStackedWidget()
        self.stack.addWidget(self.build_feed_page())
        self.stack.addWidget(self.build_sources_page())
        self.stack.addWidget(self.build_log_page())
        main.addWidget(self.stack, 1)

        wrap = QWidget()
        wrap.setLayout(main)
        outer.addWidget(wrap, 1)
        self.statusBar().showMessage("Готово к работе")

    def build_sidebar(self):
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(240)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(16, 22, 16, 16)
        lay.setSpacing(6)

        logo = QHBoxLayout()
        mark = QLabel("СП")
        mark.setObjectName("logoMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(42, 42)
        logo.addWidget(mark)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        txt.addWidget(make_label("Новостной радар", "appTitle"))
        txt.addWidget(make_label("Сургут · Югра", "appSub"))
        logo.addLayout(txt, 1)
        lay.addLayout(logo)
        lay.addSpacing(22)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for i, name in enumerate(["Лента находок", "Источники", "Журнал"]):
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if i == 0:
                b.setChecked(True)
            b.clicked.connect(lambda _=False, idx=i: self.go(idx))
            self.nav_group.addButton(b)
            lay.addWidget(b)

        lay.addStretch(1)

        token, chat = nr.telegram_creds(self.cfg)
        self.tg_chip = QLabel()
        self.tg_chip.setObjectName("tgChip")
        self.tg_chip.setWordWrap(True)
        if token and chat:
            self.tg_chip.setText('<span style="color:#2ecc71">●</span>  Telegram подключён')
        else:
            self.tg_chip.setText('<span style="color:#e0a02b">●</span>  Telegram не настроен: '
                                 'находки показываются только здесь (токен в файле .env)')
        lay.addWidget(self.tg_chip)

        dash = QPushButton("Открыть дашборд в браузере")
        dash.setObjectName("ghost")
        dash.clicked.connect(self.on_dashboard)
        lay.addWidget(dash)
        conf = QPushButton("Открыть настройки (config.yaml)")
        conf.setObjectName("ghost")
        conf.clicked.connect(self.on_open_config)
        lay.addWidget(conf)
        return side

    def build_feed_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        tools = QHBoxLayout()
        tools.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по заголовку, источнику, теме…")
        self.search.textChanged.connect(lambda _=None: self.refresh_feed())
        tools.addWidget(self.search, 1)

        self.sort = QComboBox()
        self.sort.addItems(["Сначала новые", "Сначала важные"])
        self.sort.currentIndexChanged.connect(lambda _=None: self.refresh_feed())
        tools.addWidget(self.sort)

        self.min_label = QLabel()
        self.min_label.setObjectName("muted")
        tools.addWidget(self.min_label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(min(100, max(0, self.threshold)))
        self.slider.setFixedWidth(170)
        self.slider.setToolTip("Показывать находки с баллом не ниже этого значения")
        self.slider.valueChanged.connect(lambda _=None: self.refresh_feed())
        tools.addWidget(self.slider)
        v.addLayout(tools)

        self.feed_scroll = QScrollArea()
        self.feed_scroll.setWidgetResizable(True)
        self.feed_body = QWidget()
        self.feed_layout = QVBoxLayout(self.feed_body)
        self.feed_layout.setContentsMargins(0, 0, 8, 0)
        self.feed_layout.setSpacing(10)
        self.feed_scroll.setWidget(self.feed_body)
        v.addWidget(self.feed_scroll, 1)
        return page

    def build_sources_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        tools = QHBoxLayout()
        tools.addWidget(make_label(
            "Источники задаются в config.yaml. Зелёная точка: источник отвечает, красная: ошибка.", "muted"), 1)
        btn = QPushButton("Проверить источники")
        btn.setObjectName("ghost")
        btn.clicked.connect(self.on_check_sources)
        self.job_buttons["sources"] = [btn]
        tools.addWidget(btn)
        v.addLayout(tools)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(10)
        for src in self.sources:
            card = QFrame()
            card.setObjectName("card")
            row = QHBoxLayout(card)
            row.setContentsMargins(16, 12, 16, 12)
            row.setSpacing(14)
            dot = QLabel("●")
            dot.setFixedWidth(18)
            row.addWidget(dot)
            info = QVBoxLayout()
            info.setSpacing(2)
            trust = nr.src_trust(src)
            info.addWidget(make_label(src.get("name", "?"), "cardTitle"))
            meta = (f"{nr.KIND_RU.get(src.get('kind'), '—')} · доверие {trust}/5 · "
                    f"приоритет {src.get('priority', 3)}/5 · {src.get('type', '')}")
            info.addWidget(make_label(meta, "muted"))
            row.addLayout(info, 1)
            status = make_label("", "muted", wrap=True)
            status.setMaximumWidth(420)
            status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(status)
            lay.addWidget(card)
            self.source_rows.append((src, dot, status))
        lay.addStretch(1)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)
        return page

    def build_log_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        tools = QHBoxLayout()
        tools.setSpacing(10)
        demo = QPushButton("Демо-новость")
        demo.setObjectName("ghost")
        demo.setToolTip("Прислать тестовую «горячую» новость (в историю не попадает)")
        demo.clicked.connect(self.on_demo)
        self.job_buttons["demo"] = [demo]
        tg = QPushButton("Тест Telegram")
        tg.setObjectName("ghost")
        tg.setToolTip("Отправить «радар подключён» и проверить токен и chat_id")
        tg.clicked.connect(self.on_test_telegram)
        self.job_buttons["tg"] = [tg]
        clear_log = QPushButton("Очистить журнал")
        clear_log.setObjectName("ghost")
        reset = QPushButton("Сбросить историю находок")
        reset.setObjectName("danger")
        reset.clicked.connect(self.on_reset)
        for w in (demo, tg, clear_log):
            tools.addWidget(w)
        tools.addStretch(1)
        tools.addWidget(reset)
        v.addLayout(tools)

        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        clear_log.clicked.connect(self.log.clear)
        v.addWidget(self.log, 1)
        return page

    # ------------------------------------------------------------------ обновление экрана

    def go(self, idx):
        self.stack.setCurrentIndex(idx)
        self.page_title.setText(PAGES[idx][0])
        self.page_sub.setText(PAGES[idx][1])
        if idx == 1:
            self.update_sources()

    def append_log(self, s):
        if s.strip():
            self.log.appendPlainText(s.rstrip("\n"))

    def refresh_all(self):
        self.update_stats()
        self.refresh_feed()
        self.update_sources()
        self.update_running_ui()

    def update_stats(self):
        total, important, day = read_stats(self.threshold)
        enabled = [s for s in self.sources if s.get("enabled", True)]
        ok = sum(1 for s in enabled
                 if nr.SOURCE_STATUS.get(s["name"]) and not nr.SOURCE_STATUS[s["name"]].get("error"))
        self.st_total.set_value(total)
        self.st_day.set_value(day)
        self.st_imp.set_value(important)
        self.st_src.set_value(f"{ok} / {len(enabled)}")
        self.st_last.set_value(nr.LAST_CYCLE.get("time") or "—")

    def refresh_feed(self):
        min_score = self.slider.value()
        self.min_label.setText(f"Мин. балл: {min_score}")
        query = self.search.text().strip().lower()
        mode = self.sort.currentIndex()
        rows = read_items(min_score, query, mode)
        sig = (min_score, query, mode, len(self.demo_items),
               tuple((r.get("id"), r.get("confirmations")) for r in rows))
        if sig == self._feed_sig:
            return  # ничего не изменилось: не перерисовываем (чтобы лента не дёргалась)
        self._feed_sig = sig

        bar = self.feed_scroll.verticalScrollBar()
        pos = bar.value()
        clear_layout(self.feed_layout)
        for item in self.demo_items:
            self.feed_layout.addWidget(NewsCard(item, demo=True))
        for r in rows:
            self.feed_layout.addWidget(NewsCard(r))
        if not rows and not self.demo_items:
            self.feed_layout.addWidget(make_label(
                "Пока пусто.\nНажмите «Запустить радар»: найденные новости появятся здесь.\n"
                "Можно также нажать «Демо-новость» на странице «Журнал».", "empty"),
                0, Qt.AlignmentFlag.AlignCenter)
        self.feed_layout.addStretch(1)
        QTimer.singleShot(0, lambda: bar.setValue(pos))

    def update_sources(self):
        for src, dot, status in self.source_rows:
            if not src.get("enabled", True):
                color, text = "#4b5263", "выключен в config.yaml"
            else:
                st = nr.SOURCE_STATUS.get(src["name"])
                if not st:
                    color, text = "#8a92a6", "ещё не проверялся"
                elif st.get("error"):
                    color, text = "#ff4d3d", "ошибка: " + nr.shorten(str(st["error"]), 90)
                else:
                    color = "#2ecc71"
                    text = f"работает · записей: {st.get('count', 0)} · {st.get('time', '')}"
                    if st.get("sample"):
                        status.setToolTip("Пример: " + st["sample"])
            dot.setStyleSheet(f"color: {color}; font-size: 16px;")
            status.setText(text)

    def update_running_ui(self):
        running = bool(nr.RADAR.get("running"))
        self.start_btn.setVisible(not running)
        self.live.setVisible(running)
        if running:
            self.pulse_tick(keep=True)

    def pulse_tick(self, keep=False):
        if not nr.RADAR.get("running"):
            return
        if not keep:
            self._pulse_on = not self._pulse_on
        c = "#2ecc71" if self._pulse_on else "#1b7a45"
        self.live.setText(f'<span style="color:{c}">●</span>  Радар работает · проверка каждые {self.interval} мин')

    # ------------------------------------------------------------------ действия

    def run_job(self, label, fn):
        """Запускает долгую работу в фоне, чтобы окно не зависало."""
        if label in self.busy:
            return
        self.busy.add(label)
        for b in self.job_buttons.get(label, []):
            b.setEnabled(False)

        def target():
            try:
                res, err = fn(), None
            except Exception as e:  # ошибку покажем в журнале
                res, err = None, e
            self.bridge.done.emit(label, res, err)

        threading.Thread(target=target, daemon=True).start()

    def on_job_done(self, label, result, err):
        self.busy.discard(label)
        for b in self.job_buttons.get(label, []):
            b.setEnabled(True)
        if err:
            print(f"[error] {err}")
            self.statusBar().showMessage(f"Ошибка: {err}", 10000)
            return
        if label == "cycle" and result:
            new, sent, errors = result
            self.statusBar().showMessage(
                f"Проверка завершена: новых записей {new}, уведомлений {sent}, ошибок источников {errors}", 10000)
        elif label == "demo" and result:
            self.demo_items.insert(0, result)
            self.go(0)
            self.nav_group.buttons()[0].setChecked(True)
            self.statusBar().showMessage("Демо-новость добавлена наверх ленты", 8000)
        elif label == "tg":
            self.statusBar().showMessage("Тест Telegram выполнен, подробности в журнале", 8000)
        elif label == "sources":
            self.statusBar().showMessage("Проверка источников завершена", 8000)
        self.refresh_all()

    def on_start(self):
        if nr.start_radar(self.cfg):
            self.statusBar().showMessage("Радар запущен: первая проверка идёт прямо сейчас", 10000)
        else:
            QMessageBox.warning(
                self, "Не удалось запустить",
                f"Порт {nr.DASH_PORT} занят: радар уже запущен в другом окне или в PyCharm.\n"
                "Закройте другой запуск и попробуйте снова.")
        self.update_running_ui()

    def on_cycle(self):
        self.statusBar().showMessage("Опрашиваю источники…")
        self.run_job("cycle", lambda: nr.run_cycle(self.cfg))

    def on_demo(self):
        def job():
            item = nr.make_demo_item(self.cfg)
            nr.notify(self.cfg, [item])
            return item
        self.run_job("demo", job)

    def on_test_telegram(self):
        self.run_job("tg", lambda: nr.run_test_telegram(self.cfg))

    def on_check_sources(self):
        self.statusBar().showMessage("Проверяю источники…")

        def job():
            enabled = [s for s in self.sources if s.get("enabled", True)]
            ok = bad = 0
            for src, raw, err in nr.fetch_many(enabled):
                stamp = datetime.now(nr.LOCAL_TZ).strftime("%H:%M:%S")
                nr.SOURCE_STATUS[src["name"]] = {
                    "time": stamp, "count": len(raw), "error": err,
                    "trust": nr.src_trust(src), "kind": src.get("kind", ""),
                    "sample": raw[0]["title"] if raw else "",
                }
                if err:
                    bad += 1
                    print(f"[check] ⚠️ {src['name']}: {err}")
                else:
                    ok += 1
                    print(f"[check] ✅ {src['name']}: записей {len(raw)}")
            print(f"[check] Итог: работает {ok}, с ошибкой {bad}.")
        self.run_job("sources", job)

    def on_reset(self):
        box = QMessageBox(self)
        box.setWindowTitle("Сброс истории")
        box.setText("Стереть всю историю находок?")
        box.setInformativeText("Радар начнёт с чистого листа: источники снова будут «новыми», "
                               "первый заход в каждый пройдёт тихо.")
        yes = box.addButton("Да, стереть", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            nr.clear_history()
            self.demo_items.clear()
            self._feed_sig = None
            self.refresh_all()
            self.statusBar().showMessage("История стёрта", 8000)

    def on_dashboard(self):
        if nr.RADAR.get("running"):
            QDesktopServices.openUrl(QUrl(f"http://127.0.0.1:{nr.DASH_PORT}"))
        else:
            self.statusBar().showMessage("Дашборд заработает после запуска радара", 8000)

    def on_open_config(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(nr.CONFIG_PATH)))
        self.statusBar().showMessage("После правки config.yaml перезапустите программу", 10000)

    def closeEvent(self, event):
        sys.stdout, sys.stderr = self._old_streams
        super().closeEvent(event)


def main():
    cfg = nr.load_config()
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    win = MainWindow(cfg)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
