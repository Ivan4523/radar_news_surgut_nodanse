# -*- coding: utf-8 -*-
"""Быстрые проверки: python -m pytest test_radar.py   (или просто python test_radar.py)"""
import news_radar as nr

DUPLICATES = [
    ("Пожар в Сургуте: горит склад на Югорском тракте", "На Югорском тракте в Сургуте загорелся склад"),
    ("В Сургуте на Югорском тракте столкнулись два автомобиля", "На Югорском тракте в Сургуте произошло ДТП: столкнулись два автомобиля"),
    ("Нижневартовск признали лучшим по экологичности автобусов", "Нижневартовск признан лучшим в России по экологичности автобусов"),
]
DIFFERENT = [
    ("В Сургуте отключат горячую воду в районе ТЦ", "В Сургуте отключат свет в районе ТЦ"),
    ("В Сургуте перекроют улицу Энгельса из-за ремонта", "В Сургуте перекроют улицу Мира из-за ремонта теплотрассы"),
    ("Мэр Сургута проведёт приём граждан 20 ноября", "Мэр Сургута проведёт приём граждан 27 ноября"),
    ("В Сургуте задержали водителя за пьяную езду", "В Сургуте задержали мужчину за кражу из магазина"),
]


def test_duplicates_detected():
    for a, b in DUPLICATES:
        assert nr.similarity(a, b) >= 75, (a, b)


def test_different_news_not_merged():
    for a, b in DIFFERENT:
        assert nr.similarity(a, b) < 75, (a, b)


def test_official_boilerplate_below_threshold():
    cfg = nr.load_config()
    item = {"title": "О назначении публичных слушаний на 01.12.2026", "summary": "", "published": "",
            "source_priority": 5, "geo_hint": "сургут", "source_trust": 5}
    assert nr.score_item(item, cfg)[0] < cfg["notify_min_score"]


def test_emergency_passes_threshold():
    cfg = nr.load_config()
    item = {"title": "В Сургуте горит склад, эвакуировали людей", "summary": "", "published": "",
            "source_priority": 4, "geo_hint": "сургут", "source_trust": 2}
    assert nr.score_item(item, cfg)[0] >= cfg["notify_min_score"]


def test_split_also_with_commas_in_names():
    nr.load_config()
    got = nr.split_also("SiTV, Сургутинформтв (сайт), К-информ Сургут (Telegram)")
    assert got == ["SiTV, Сургутинформтв (сайт)", "К-информ Сургут (Telegram)"]


def test_coverage_status():
    nr.load_config()
    assert nr.coverage_status("Мэрия Сургута (Telegram)", [])[0] == "first"
    assert nr.coverage_status("Мэрия Сургута (Telegram)", ["SiTV, Сургутинформтв (сайт)"])[0] == "media"
    assert nr.coverage_status("К-информ Сургут (Telegram)", [])[0] == "rumor"
    assert nr.coverage_status("К-информ Сургут (Telegram)", ["СИА-ПРЕСС (RSS)"])[0] == "own"


def test_media_lead_two_cycles(tmp_path=None):
    """Цикл 1: пост мэрии. Цикл 2 (через 45 мин): подхватило СМИ. Радар должен посчитать опережение."""
    import pathlib, sqlite3, tempfile
    nr.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    cfg = nr.load_config()
    cfg["baseline_on_first_run"] = False
    src = {s["name"]: s for s in cfg["sources"]}
    mayor, sitv = src["Мэрия Сургута (Telegram)"], src["SiTV, Сургутинформтв (сайт)"]
    cfg["sources"] = [mayor, sitv]
    mk = lambda t, u: {"title": t, "summary": t + ". Подробности уточняются.", "url": u, "published": ""}
    state = {mayor["name"]: [mk("В Сургуте на улице Энергетиков горит склад, эвакуированы люди", "http://m/1")]}
    nr.fetch_all = lambda sources: [(s, state.get(s["name"], []), None) for s in sources]
    nr.send_telegram = lambda *a, **k: "skip"
    nr._run_cycle(cfg)
    c = sqlite3.connect(nr.DB_PATH)
    c.execute("UPDATE items SET found_at=?", (nr.now_iso(minutes=45),)); c.commit(); c.close()
    state[sitv["name"]] = [mk("Горит склад на улице Энергетиков в Сургуте, людей эвакуировали", "http://s/1")]
    nr._run_cycle(cfg)
    conn = nr.db_connect()
    n, avg, first = nr.lead_stats(conn)
    assert n == 1 and 44 <= avg <= 46 and first == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("OK", name)
