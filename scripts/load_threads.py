#!/usr/bin/env python3
"""Загрузка готовых тредов (data/<group>/threads.json) в базу с эмбеддингами.

    python scripts/load_threads.py data/it_ua_valencia/threads.json it_ua_valencia
    python scripts/load_threads.py data/it_ua_valencia/threads.json it_ua_valencia --no-embed

Идемпотентно: повторный запуск обновляет треды по (group_slug, root_message_id)
и пересчитывает вектор только там, где изменился текст.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from tqdm import tqdm

load_dotenv()

log = logging.getLogger("load_threads")
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "100"))


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL не задан — скопируй .env.example в .env и заполни")
    return url


def parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upsert_threads(conn, group_slug: str, threads: list[dict]) -> int:
    """Кладёт треды в базу. Вектор помечается pending, если текст изменился."""
    inserted = 0
    with conn.cursor(row_factory=dict_row) as cur:
        for t in tqdm(threads, desc=f"upsert {group_slug}", unit="thread"):
            content = t.get("text") or ""
            if not content.strip():
                continue
            root_id = int(t["thread_id"])
            activity = parse_date(t.get("thread_date"))
            content_hash = hashlib.sha256(content.encode()).hexdigest()

            cur.execute(
                """
                INSERT INTO threads (group_slug, root_message_id, content, message_count,
                                     started_at, last_activity_at, content_hash, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (group_slug, root_message_id) DO UPDATE
                    SET content          = EXCLUDED.content,
                        last_activity_at = EXCLUDED.last_activity_at,
                        content_hash     = EXCLUDED.content_hash,
                        updated_at       = now()
                RETURNING id, content_hash
                """,
                (
                    group_slug,
                    root_id,
                    content,
                    t.get("message_count", content.count("\n") + 1),
                    activity,
                    activity,
                    content_hash,
                    json.dumps({"total_characters": t.get("total_characters")}),
                ),
            )
            thread_id = cur.fetchone()["id"]

            # вектор ставится в очередь, только если текста ещё нет или он изменился
            cur.execute(
                """
                INSERT INTO thread_embeddings (thread_id, model, source_hash, status)
                VALUES (%s, %s, %s, 'pending')
                ON CONFLICT (thread_id, model) DO UPDATE
                    SET status      = CASE WHEN thread_embeddings.source_hash = EXCLUDED.source_hash
                                           THEN thread_embeddings.status ELSE 'pending' END,
                        source_hash = EXCLUDED.source_hash,
                        updated_at  = now()
                """,
                (thread_id, EMBED_MODEL, content_hash),
            )
            inserted += 1
        conn.commit()
    return inserted


def embed_pending(conn) -> int:
    """Считает эмбеддинги для всех тредов со статусом pending."""
    from openai import OpenAI  # импорт здесь: загрузка с --no-embed не требует openai

    client = OpenAI()
    done = 0
    while True:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT e.thread_id, t.content
                FROM thread_embeddings e
                JOIN threads t ON t.id = e.thread_id
                WHERE e.status = 'pending' AND e.model = %s AND e.attempt_count < 3
                LIMIT %s
                """,
                (EMBED_MODEL, BATCH_SIZE),
            )
            batch = cur.fetchall()
            if not batch:
                break

            texts = [row["content"][:8000] for row in batch]
            try:
                resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
            except Exception as exc:  # сеть/лимиты — не роняем весь прогон
                log.warning("batch failed: %s", exc)
                cur.execute(
                    """
                    UPDATE thread_embeddings
                       SET attempt_count = attempt_count + 1, last_error = %s, updated_at = now()
                     WHERE thread_id = ANY(%s) AND model = %s
                    """,
                    (str(exc)[:500], [r["thread_id"] for r in batch], EMBED_MODEL),
                )
                conn.commit()
                continue

            for row, item in zip(batch, resp.data):
                cur.execute(
                    """
                    UPDATE thread_embeddings
                       SET embedding = %s, status = 'ready', last_error = NULL, updated_at = now()
                     WHERE thread_id = %s AND model = %s
                    """,
                    (str(item.embedding), row["thread_id"], EMBED_MODEL),
                )
            conn.commit()
            done += len(batch)
            print(f"  embedded {done}", end="\r", flush=True)
    return done


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("threads_file")
    ap.add_argument("group_slug")
    ap.add_argument("--no-embed", action="store_true", help="только загрузить, без эмбеддингов")
    args = ap.parse_args()

    with open(args.threads_file, encoding="utf-8") as fh:
        threads = json.load(fh)
    log.info("файл: %s тредов", len(threads))

    with psycopg.connect(database_url()) as conn:
        count = upsert_threads(conn, args.group_slug, threads)
        log.info("загружено/обновлено: %s", count)
        if not args.no_embed:
            log.info("эмбеддинги: %s", embed_pending(conn))


if __name__ == "__main__":
    main()
