#!/usr/bin/env python3
"""Индексация всего корпуса тредов в Postgres с self-hosted эмбеддингами."""
import hashlib, json, os, pathlib, sys, time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from sentence_transformers import SentenceTransformer

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.getenv("DATABASE_URL", "postgresql://valencia:valencia_local@localhost:55432/valencia")
MODEL_ID = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
DOC_PREFIX = os.getenv("DOC_PREFIX", "")
GROUPS = ["valencia_parents_kids_schools", "it_ua_valencia", "matusi_valencia"]

def _device() -> str:
    """cuda → mps → cpu; переопределяется переменной DEVICE."""
    if os.getenv("DEVICE"):
        return os.environ["DEVICE"]
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


DEVICE = _device()



def parse_date(v):
    if not v:
        return datetime.now(timezone.utc)
    d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def main():
    t0 = time.time()
    rows = []
    for g in GROUPS:
        p = ROOT / "data" / g / "threads.json"
        if not p.exists():
            continue
        for t in json.load(open(p, encoding="utf-8")):
            txt = (t.get("text") or "").strip()
            if len(txt) < 50:
                continue
            rows.append((g, int(t["thread_id"]), txt, parse_date(t.get("thread_date"))))
    print(f"тредов к индексации: {len(rows)}", flush=True)

    with psycopg.connect(DSN) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) c FROM threads")
        if cur.fetchone()["c"] == 0:
            for g, rid, txt, dt in rows:
                cur.execute(
                    """INSERT INTO threads (group_slug, root_message_id, content, message_count,
                                            started_at, last_activity_at, content_hash)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (group_slug, root_message_id) DO NOTHING""",
                    (g, rid, txt, txt.count("\n") + 1, dt, dt,
                     hashlib.sha256(txt.encode()).hexdigest()))
            conn.commit()
            print(f"треды загружены за {time.time()-t0:.0f}s", flush=True)

        cur.execute("""SELECT t.id, t.content FROM threads t
                       LEFT JOIN thread_embeddings e
                              ON e.thread_id = t.id AND e.model = %s AND e.status='ready'
                       WHERE e.thread_id IS NULL ORDER BY t.id""", (MODEL_ID,))
        todo = cur.fetchall()
        print(f"нужно посчитать векторов: {len(todo)}", flush=True)
        if not todo:
            return

        model = SentenceTransformer(MODEL_ID, device=DEVICE, trust_remote_code=True)
        # окно 1024 токенов: покрывает почти все треды целиком и вдвое быстрее 8192
        model.max_seq_length = int(os.getenv("MAX_SEQ_LEN", "1024"))
        B = 256
        for i in range(0, len(todo), B):
            chunk = todo[i:i + B]
            vecs = model.encode([DOC_PREFIX + r["content"][:4000] for r in chunk],
                                batch_size=32, normalize_embeddings=True)
            with conn.cursor() as c2:
                for r, v in zip(chunk, vecs):
                    c2.execute(
                        """INSERT INTO thread_embeddings (thread_id, model, dimensions,
                                                          source_hash, embedding, status)
                           VALUES (%s,%s,%s,'x',%s,'ready')
                           ON CONFLICT (thread_id, model) DO UPDATE
                             SET embedding=EXCLUDED.embedding, status='ready'""",
                        (r["id"], MODEL_ID, len(v), str(v.tolist())))
            conn.commit()
            done = min(i + B, len(todo))
            el = time.time() - t0
            print(f"  {done}/{len(todo)} ({done/el:.1f}/s)", flush=True)
    print(f"готово за {(time.time()-t0)/60:.1f} мин")


if __name__ == "__main__":
    main()
