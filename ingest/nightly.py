#!/usr/bin/env python3
"""Ночной прогон: докачка → пересборка тредов → векторы → отчёт.

    python -m ingest.nightly                # всё
    python -m ingest.nightly --skip-fetch   # только пересборка и векторы
    python -m ingest.nightly --window 30    # окно пересборки, дней
    python -m ingest.nightly --backfill-days 3650   # плюс история назад

Прогон идемпотентный: упал на середине — перезапуск догоняет, дублей не будет.
Слой фактов (facts) в прогон пока не входит: он в схеме есть, но замером не
проверен — см. docs/QUALITY.md.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from app import config, db
from ingest import embed as embed_mod
from ingest import threads as threads_mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fetch", action="store_true", help="не ходить в Telegram")
    ap.add_argument("--skip-embed", action="store_true", help="не считать векторы")
    ap.add_argument("--window", type=int, default=config.THREAD_REBUILD_DAYS,
                    help="окно пересборки тредов, дней")
    ap.add_argument("--since-days", type=int, default=0,
                    help="докачивать только сообщения свежее N дней")
    ap.add_argument("--limit", type=int, default=None,
                    help="максимум сообщений на группу за прогон")
    ap.add_argument("--backfill-days", type=int, default=0,
                    help="докачать историю назад, от самого старого сохранённого до N дней назад")
    ap.add_argument("--groups", default=",".join(config.GROUPS))
    a = ap.parse_args()

    groups = [g.strip() for g in a.groups.split(",") if g.strip()]
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=a.since_days) if a.since_days else None
    until = now - timedelta(days=a.backfill_days) if a.backfill_days else None
    # Окно пересборки не может быть уже окна докачки: иначе скачанные сообщения
    # осядут в архиве и никогда не станут тредами — окно ползёт только вперёд.
    window = max(a.window, a.since_days, a.backfill_days)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "groups": groups}
    t0 = time.time()

    with db.connect() as conn:
        # 1. докачка
        if not a.skip_fetch:
            from ingest import fetch
            report["fetch"] = fetch.run(conn, groups=groups, since=since, limit=a.limit,
                                        backfill_until=until)
            print(f"докачка: {report['fetch']}", flush=True)

        # 2. пересборка тредов за окно. Падение на одной группе не должно
        # лишать остальные ни тредов, ни векторов.
        report["threads"] = []
        for g in groups:
            try:
                report["threads"].append(threads_mod.rebuild(conn, g, window_days=window))
            except Exception as e:
                conn.rollback()
                report["threads"].append({"group": g, "error": str(e)})
        print(f"треды: {report['threads']}", flush=True)

        # 3. векторы для изменившихся
        if not a.skip_embed:
            report["embeddings"] = embed_mod.embed_pending(conn)
            print(f"векторы: {report['embeddings']}", flush=True)

        # 4. отчёт
        with conn.cursor() as cur:
            cur.execute("""SELECT (SELECT count(*) FROM messages) AS messages,
                                  (SELECT count(*) FROM threads) AS threads,
                                  (SELECT count(*) FROM thread_embeddings
                                    WHERE status='ready') AS embedded,
                                  (SELECT min(started_at) FROM threads) AS oldest,
                                  (SELECT max(last_activity_at) FROM threads) AS freshest""")
            totals = cur.fetchone()
    for k in ("oldest", "freshest"):
        totals[k] = totals[k].isoformat() if totals[k] else None
    report["totals"] = totals
    report["seconds"] = round(time.time() - t0)

    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    # Код возврата — единственное, что видит cron. Ошибка докачки или
    # пересборки тоже должна его красить, иначе тихий сбой не заметит никто.
    broken = sum(1 for r in report.get("fetch", []) + report.get("threads", []) if "error" in r)
    failed = report.get("embeddings", {}).get("failed", 0)
    return 1 if (broken or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
