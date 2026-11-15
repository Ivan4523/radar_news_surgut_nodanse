#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ОЦЕНКА КАЧЕСТВА РАДАРА: насколько отбор совпадает с тем, что нужно редактору.

Два способа измерить качество:

1. По размеченной выборке (файл labeled_sample.csv):
       python eval_radar.py
   В файле у каждой новости колонка label: 1 = редактору стоило сообщить, 0 = не стоило.
   Скрипт заново оценивает каждую новость текущими настройками из config.yaml
   и считает, сколько нужного радар поймал и сколько лишнего прислал.

2. По живым отметкам 👍/👎 из Telegram:
       python eval_radar.py --live

Как собрать свою разметку (лучше, если её сделает сам редактор):
       python eval_radar.py --export my_sample.csv
   Откройте файл в Excel, впишите в колонку label 1 или 0, сохраните и запустите:
       python eval_radar.py --csv my_sample.csv
"""

import argparse
import csv
import sqlite3
import sys

import news_radar as nr

FIELDS = ["id", "source", "title", "summary", "published", "found_at", "stored_score", "label"]


def export_rows(path):
    conn = sqlite3.connect(nr.DB_PATH)
    rows = conn.execute("SELECT id, source, title, summary, published, found_at, score FROM items ORDER BY id").fetchall()
    conn.close()
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for r in rows:
            w.writerow([r[0], r[1], r[2], (r[3] or "").replace("\n", " "), r[4], r[5], r[6], ""])
    print(f"Выгружено {len(rows)} находок в {path}. Заполните колонку label (1 или 0).")


def rescore(path, cfg):
    """Читает разметку и заново оценивает каждую новость текущим алгоритмом."""
    by_name = {s["name"]: s for s in cfg.get("sources", [])}
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    items, unlabeled = [], 0
    for r in rows:
        label = (r.get("label") or "").strip()
        if label not in ("0", "1"):
            unlabeled += 1
            continue
        src = by_name.get(r["source"], {})
        score, cat, why = nr.score_item(
            {"title": r["title"], "summary": r.get("summary") or "", "published": r.get("published") or "",
             "source_priority": src.get("priority", 3), "geo_hint": src.get("geo_hint", ""),
             "source_trust": nr.src_trust(src) if src else 3},
            cfg, now=nr.parse_dt(r.get("found_at") or ""))  # «сейчас» = момент, когда новость нашли
        old = r.get("stored_score")
        items.append({"title": r["title"], "source": r["source"], "label": int(label), "score": score,
                      "old": float(old) if old not in (None, "") else None, "why": why})
    return items, unlabeled


def metrics(items, key, threshold):
    sel = [i for i in items if i[key] is not None]
    tp = sum(1 for i in sel if i[key] >= threshold and i["label"] == 1)
    fp = sum(1 for i in sel if i[key] >= threshold and i["label"] == 0)
    fn = sum(1 for i in sel if i[key] < threshold and i["label"] == 1)
    tn = sum(1 for i in sel if i[key] < threshold and i["label"] == 0)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return tp, fp, fn, tn, prec, rec, f1


def precision_at(items, key, k):
    top = sorted((i for i in items if i[key] is not None), key=lambda i: -i[key])[:k]
    return sum(i["label"] for i in top) / len(top) if top else 0.0


def run_labeled(path, cfg):
    items, unlabeled = rescore(path, cfg)
    if not items:
        print("В файле нет размеченных строк (колонка label: 1 или 0).")
        return
    T = cfg.get("notify_min_score", 35)
    pos = sum(i["label"] for i in items)
    print(f"\n=== Оценка по разметке: {len(items)} новостей, из них нужных редактору: {pos} ({100 * pos / len(items):.0f}%) ===")
    if unlabeled:
        print(f"(строк без разметки пропущено: {unlabeled})")
    print(f"Порог уведомления: {T} баллов (notify_min_score)\n")

    print(f"{'Вариант':<34}{'Прислано':>9}{'Точность':>10}{'Полнота':>9}{'F1':>7}")
    print(f"{'Присылать всё подряд':<34}{len(items):>9}{100 * pos / len(items):>9.0f}%{'100':>8}%{2 * (pos / len(items)) / (1 + pos / len(items)):>7.2f}")
    rows = [("Радар (текущие настройки)", "score")]
    if any(i["old"] is not None for i in items):
        rows.append(("Радар (баллы из базы, как было)", "old"))
    for name, key in rows:
        tp, fp, fn, tn, p, r, f1 = metrics(items, key, T)
        print(f"{name:<34}{tp + fp:>9}{100 * p:>9.0f}%{100 * r:>8.0f}%{f1:>7.2f}")

    print("\nТочность в топе по баллам (текущие настройки):")
    for k in (5, 10, 20):
        if len(items) >= k:
            print(f"  первые {k:>2}: {100 * precision_at(items, 'score', k):.0f}% нужных")

    print("\nЕсли менять порог уведомления:")
    print(f"  {'порог':>6}{'прислано':>10}{'точность':>10}{'полнота':>9}{'F1':>7}")
    for t in range(20, 75, 5):
        tp, fp, fn, tn, p, r, f1 = metrics(items, "score", t)
        mark = "  <- сейчас" if t == T else ""
        print(f"  {t:>6}{tp + fp:>10}{100 * p:>9.0f}%{100 * r:>8.0f}%{f1:>7.2f}{mark}")

    tp, fp, fn, tn, *_ = metrics(items, "score", T)
    fps = sorted((i for i in items if i["score"] >= T and i["label"] == 0), key=lambda i: -i["score"])
    fns = sorted((i for i in items if i["score"] < T and i["label"] == 1), key=lambda i: -i["score"])
    print(f"\nЛишнее, что радар прислал бы ({fp}):")
    for i in fps:
        print(f"  {i['score']:>5.0f}  {nr.shorten(i['title'], 80)}  [{i['source'][:22]}]")
    print(f"\nНужное, что радар пропустил бы ({fn}):")
    for i in fns:
        print(f"  {i['score']:>5.0f}  {nr.shorten(i['title'], 80)}  [{i['source'][:22]}]")


def run_live():
    conn = nr.db_connect()
    up, down = conn.execute("SELECT COALESCE(SUM(vote > 0), 0), COALESCE(SUM(vote < 0), 0) FROM feedback").fetchone()
    if not (up or down):
        print("Отметок 👍/👎 пока нет. Нажимайте кнопки под новостями в Telegram.")
        return
    print(f"\n=== Оценки редактора в Telegram: 👍 {up} · 👎 {down} · точность {100 * up / (up + down):.0f}% ===")
    for title, col in (("По источникам", "source"), ("По темам", "category")):
        print(f"\n{title}:")
        for name, u, d in conn.execute(
                f"""SELECT i.{col}, SUM(f.vote > 0), SUM(f.vote < 0) FROM feedback f
                    JOIN items i ON i.url_hash = f.url_hash GROUP BY i.{col} ORDER BY COUNT(*) DESC"""):
            print(f"  {name:<46} 👍 {u:<3} 👎 {d:<3} точность {100 * u / (u + d):.0f}%")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Оценка качества новостного радара")
    ap.add_argument("--csv", default=str(nr.BASE_DIR / "labeled_sample.csv"), help="файл с разметкой")
    ap.add_argument("--export", metavar="ФАЙЛ", help="выгрузить находки из базы для разметки")
    ap.add_argument("--live", action="store_true", help="метрики по отметкам 👍/👎 из Telegram")
    args = ap.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows
    if args.export:
        export_rows(args.export)
    elif args.live:
        run_live()
    else:
        run_labeled(args.csv, nr.load_config())


if __name__ == "__main__":
    main()
