#!/usr/bin/env python3
"""Ночной прогон: докачка → пересборка тредов → векторы → отчёт.

    python -m ingest.nightly                # всё
    python -m ingest.nightly --skip-fetch   # только пересборка и векторы
    python -m ingest.nightly --window 30    # окно пересборки, дней

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
    ap.add_argument("--groups", default=",".join(config.GROUPS))
    a = ap.parse_args()

    groups = [g.strip() for g in a.groups.split(",") if g.strip()]
    since = datetime.now(timezone.utc) - timedelta(days=a.since_days) if a.since_days else None
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "groups": groups}
    t0 = time.time()

    with db.connect() as conn:
        # 1. докачка
        if not a.skip_fetch:
            from ingest import fetch
            report["fetch"] = fetch.run(conn, groups=groups, since=since)
            print(f"докачка: {report['fetch']}", flush=True)

        # 2. пересборка тредов за окно
        report["threads"] = [
            threads_mod.rebuild(conn, g, window_days=a.window) for g in groups
        ]
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
                                  (SELECT max(last_activity_at) FROM threads) AS freshest""")
            totals = cur.fetchone()
    totals["freshest"] = totals["freshest"].isoformat() if totals["freshest"] else None
    report["totals"] = totals
    report["seconds"] = round(time.time() - t0)

    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    failed = report.get("embeddings", {}).get("failed", 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
