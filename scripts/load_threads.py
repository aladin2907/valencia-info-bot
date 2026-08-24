#!/usr/bin/env python3
"""Загрузка готовых тредов (data/<group>/threads.json) в базу.

    python scripts/load_threads.py data/it_ua_valencia/threads.json it_ua_valencia
    python scripts/load_threads.py data/it_ua_valencia/threads.json it_ua_valencia --no-embed
    python scripts/load_threads.py data/it_ua_valencia/threads.json it_ua_valencia --limit 500

Нужен для восстановления: выгрузки чатов уже собраны в треды, их не надо
качать заново. Векторы считает сервис моделей (models/server.py), тот же, что и
в ночном прогоне. Идемпотентно: повторный запуск обновляет треды по
(group_slug, root_message_id) и пересчитывает вектор только там, где изменился текст.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone

from app import config, db
from ingest.embed import embed_pending

log = logging.getLogger("load_threads")

UPSERT = """
INSERT INTO threads (group_slug, root_message_id, content, message_count,
                     started_at, last_activity_at, content_hash, metadata)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (group_slug, root_message_id) DO UPDATE
   SET content          = EXCLUDED.content,
       last_activity_at = EXCLUDED.last_activity_at,
       content_hash     = EXCLUDED.content_hash,
       updated_at       = now()
"""


def parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upsert_threads(conn, group_slug: str, threads: list[dict]) -> int:
    count = 0
    with conn.cursor() as cur:
        for t in threads:
            content = (t.get("text") or t.get("content") or "").strip()
            if len(content) < 50:
                continue
            activity = parse_date(t.get("thread_date"))
            cur.execute(UPSERT, (
                group_slug, int(t["thread_id"]), content,
                t.get("message_count", content.count("\n") + 1),
                activity, activity,
                hashlib.sha256(content.encode()).hexdigest(),
                json.dumps({"total_characters": t.get("total_characters")}),
            ))
            count += 1
            if count % 1000 == 0:
                conn.commit()
                print(f"  {count}", end="\r", flush=True)
    conn.commit()
    return count


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("threads_file")
    ap.add_argument("group_slug")
    ap.add_argument("--no-embed", action="store_true", help="только загрузить, без векторов")
    ap.add_argument("--limit", type=int, default=0, help="взять только первые N тредов")
    args = ap.parse_args()

    with open(args.threads_file, encoding="utf-8") as fh:
        threads = json.load(fh)
    if args.limit:
        threads = threads[:args.limit]
    log.info("файл: %s тредов", len(threads))

    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL не задан — скопируй .env.example в .env и заполни")

    with db.connect() as conn:
        log.info("загружено/обновлено: %s", upsert_threads(conn, args.group_slug, threads))
        if not args.no_embed:
            log.info("векторы: %s", embed_pending(conn))


if __name__ == "__main__":
    main()
