"""Пересчёт векторов — только для тредов, у которых изменился текст.

Сравнение идёт по content_hash: если тред не менялся, вектор не трогаем. Это и
делает ночной прогон дешёвым, и позволяет перезапускать его сколько угодно раз.
"""
import time

from app import config, models_client

# Треды без вектора или с вектором от старой версии текста.
SQL_PENDING = """
SELECT t.id, t.content, t.content_hash
  FROM threads t
  LEFT JOIN thread_embeddings e ON e.thread_id = t.id AND e.model = %s
 WHERE e.thread_id IS NULL
    OR e.status <> 'ready'
    OR e.source_hash IS DISTINCT FROM t.content_hash
 ORDER BY t.last_activity_at DESC
 LIMIT %s
"""

SQL_WRITE = """
INSERT INTO thread_embeddings (thread_id, model, dimensions, source_hash,
                               embedding, status, attempt_count, updated_at)
VALUES (%s,%s,%s,%s,%s,'ready',0, now())
ON CONFLICT (thread_id, model) DO UPDATE
   SET embedding = EXCLUDED.embedding,
       source_hash = EXCLUDED.source_hash,
       dimensions = EXCLUDED.dimensions,
       status = 'ready',
       last_error = NULL,
       updated_at = now()
"""

SQL_FAIL = """
INSERT INTO thread_embeddings (thread_id, model, dimensions, source_hash, status,
                               attempt_count, last_error, updated_at)
VALUES (%s,%s,1024,%s,'pending',1,%s, now())
ON CONFLICT (thread_id, model) DO UPDATE
   SET status = 'pending',
       attempt_count = thread_embeddings.attempt_count + 1,
       last_error = EXCLUDED.last_error,
       updated_at = now()
"""


def embed_pending(conn, limit: int = 1_000_000, progress: bool = True) -> dict:
    model = config.EMBED_MODEL
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(SQL_PENDING, (model, limit))
        todo = cur.fetchall()
    if not todo:
        return {"pending": 0, "done": 0, "failed": 0, "seconds": 0}

    done = failed = 0
    batch = config.EMBED_BATCH
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            vectors = models_client.embed([r["content"] for r in chunk], kind="document")
        except Exception as e:
            # Сбой модели не роняет весь прогон: помечаем pending, доедет следующей ночью
            with conn.cursor() as cur:
                for r in chunk:
                    cur.execute(SQL_FAIL, (r["id"], model, r["content_hash"], str(e)[:500]))
            conn.commit()
            failed += len(chunk)
            continue
        with conn.cursor() as cur:
            for r, v in zip(chunk, vectors):
                cur.execute(SQL_WRITE, (r["id"], model, len(v), r["content_hash"], str(v)))
        conn.commit()
        done += len(chunk)
        if progress:
            elapsed = time.time() - t0
            print(f"  векторы {done}/{len(todo)} ({done / max(elapsed, 0.1):.1f}/с)", flush=True)

    return {"pending": len(todo), "done": done, "failed": failed,
            "seconds": round(time.time() - t0)}
