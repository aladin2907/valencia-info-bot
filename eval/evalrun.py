#!/usr/bin/env python3
"""Прогон одного раунда: поиск → ответ → оценка. Метрики по 50 вопросам.

    python evalrun.py --name r1-baseline --vector
    python evalrun.py --name r2-hybrid --vector --fts
    python evalrun.py --name r3-rerank --vector --fts --rerank
"""
import argparse, json, os, pathlib, random, re, statistics, threading, time
from concurrent.futures import ThreadPoolExecutor
import urllib.request

import psycopg
from psycopg.rows import dict_row

HERE = pathlib.Path(__file__).parent
ROOT = pathlib.Path("/Users/macbook/PetProjects/valencia_info_bot")
DSN = os.getenv("DATABASE_URL", "postgresql://valencia:test@localhost:55433/valencia")
LLM_MODEL = "stealth/ox-alpha"
KEY = next(l.split("=", 1)[1].strip() for l in open(ROOT / ".env")
           if l.startswith("OPENROUTER_API_KEY="))
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
QUERY_PREFIX = os.getenv("QUERY_PREFIX", "")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


def llm(prompt, system="", max_tokens=1500, temperature=0.2):
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    body = json.dumps({"model": LLM_MODEL, "messages": msgs, "max_tokens": max_tokens,
                       "temperature": temperature}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    for a in range(6):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read())
            c = d["choices"][0]["message"]["content"]
            if c:
                return c
            time.sleep(2 + a * 3)
        except Exception as e:
            # 429 — упёрлись в лимит: ждём с нарастающей паузой, иначе повтор
            # прилетает в тот же лимит и весь раунд обнуляется
            wait = min(60, 5 * (2 ** a)) + random.uniform(0, 3)
            if a == 5:
                return f"__ERROR__ {e}"
            time.sleep(wait)
    return "__ERROR__"


def jparse(t):
    if not t or t.startswith("__ERROR__"):
        return None
    m = re.search(r"\{.*\}", t, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


REWRITE_SYS = """Ты — оптимизатор запросов для поиска по обсуждениям в чатах Валенсии.
Убери вводные слова ("подскажите", "кто знает"), исправь опечатки, сохрани ключевые термины,
названия документов, имена, адреса. Добавь синонимы на русском И украинском.
Верни JSON: {"key_phrase": "<очищенный запрос + русская версия + украинская версия, одной строкой>"}"""

ANSWER_SYS = """Ты — бот-помощник чата "Жизнь в Валенсии": опытный местный житель, пишешь кратко и по-человечески.
ЯЗЫК ОТВЕТА — тот же, что у вопроса.
Отвечай ТОЛЬКО по содержимому обсуждений ниже. Используй 1-3 короткие цитаты участников.
Точно копируй адреса, телефоны, ссылки, цены, сроки, названия документов.
Если в обсуждениях ответа нет — напиши "Информация не найдена".
Без заголовков, списков и форматирования — единый связный текст."""

JUDGE_SYS = """Ты — строгий экзаменатор Q&A-бота по чатам Валенсии.
Дан вопрос, эталонные факты (из реального обсуждения) и ответ бота.
Верни СТРОГО JSON:
{
 "covered": <сколько эталонных фактов реально присутствует в ответе>,
 "total": <всего эталонных фактов>,
 "score": <0-5: 5 — отвечает полно и точно; 3 — частично; 0 — не отвечает или выдумывает>,
 "hallucination": true/false,
 "why": "одно предложение"
}"""

_embedder = _reranker = None


def embed(texts):
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBED_MODEL, device="mps", trust_remote_code=True)
    return _embedder.encode(texts, batch_size=8, normalize_embeddings=True)


def rerank_batch(pairs):
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(RERANK_MODEL, device="mps", trust_remote_code=True)
    return _reranker.predict(pairs, batch_size=8)


def retrieve(cfg, query_text, emb):
    with psycopg.connect(DSN) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT id, group_slug, content, last_activity_at, score
               FROM hybrid_search(%s, %s::vector, %s, %s, %s, 50, %s, %s)""",
            (query_text if cfg["fts"] else None,
             str(emb.tolist()) if cfg["vector"] else None,
             cfg["pool"], cfg["ftsw"] if cfg["fts"] else 0.0, 1.0 if cfg["vector"] else 0.0,
             365.0 if cfg["recency"] else 1e9, 0.6 if cfg["recency"] else 1.0))
        return cur.fetchall()


def gold_ids(cases):
    with psycopg.connect(DSN) as conn, conn.cursor(row_factory=dict_row) as cur:
        out = []
        for c in cases:
            cur.execute("SELECT id FROM threads WHERE group_slug=%s AND root_message_id=%s",
                        (c["source_group"], c["source_thread_id"]))
            r = cur.fetchone()
            out.append(r["id"] if r else None)
        return out


def cache_path(name):
    return HERE / f"cache_{name}.json"


def answer_and_judge(args):
    case, rows, cfg, cache, lock = args
    key = case["question"][:120]
    with lock:
        if key in cache:
            return cache[key]["ans"], cache[key]["j"]
    ctx = "\n\n---\n\n".join(
        f"[обсуждение от {r['last_activity_at'].date()}]\n{r['content'][:2500]}"
        for r in rows[:cfg["ctx"]])
    ans = llm(f"Вопрос: {case['question']}\n\nОбсуждения:\n{ctx}",
              system=ANSWER_SYS, max_tokens=3000)
    j = jparse(llm(f"Вопрос: {case['question']}\n\nЭталонные факты:\n" +
                   "\n".join(f"- {g}" for g in case["gold_points"]) +
                   f"\n\nОтвет бота:\n{ans}", system=JUDGE_SYS, max_tokens=900)) or {}
    if not ans.startswith("__ERROR__") and j:
        with lock:
            cache[key] = {"ans": ans, "j": j}
            json.dump(cache, open(cache_path(cfg["name"]), "w"), ensure_ascii=False)
    return ans, j


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--vector", action="store_true")
    ap.add_argument("--fts", action="store_true")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--rewrite", action="store_true")
    ap.add_argument("--recency", action="store_true")
    ap.add_argument("--pool", type=int, default=50)
    ap.add_argument("--topk", type=int, default=15)
    ap.add_argument("--ctx", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ftsw", type=float, default=1.0)
    ap.add_argument("--offset", type=int, default=0)
    a = ap.parse_args()
    cfg = dict(vector=a.vector, fts=a.fts, rerank=a.rerank, rewrite=a.rewrite,
               recency=a.recency, pool=a.pool, topk=a.topk, ctx=a.ctx, ftsw=a.ftsw, name=a.name,
               embed_model=EMBED_MODEL)

    cases = json.load(open(HERE / "dataset.json", encoding="utf-8"))
    if a.offset or a.limit:
        cases = cases[a.offset:(a.offset + a.limit) if a.limit else None]
    print(f"[{a.name}] вопросов: {len(cases)} | {cfg}", flush=True)
    t0 = time.time()

    # 1. переформулировка запросов (сеть)
    queries = [c["question"] for c in cases]
    if cfg["rewrite"]:
        with ThreadPoolExecutor(max_workers=5) as ex:
            rw = list(ex.map(lambda q: jparse(llm(f"Текст: {q}", system=REWRITE_SYS,
                                                  max_tokens=500)), queries))
        queries = [(r or {}).get("key_phrase") or q for r, q in zip(rw, queries)]
        print(f"  переформулировано за {time.time()-t0:.0f}s", flush=True)

    # 2. эмбеддинги запросов (GPU, одним батчем)
    embs = embed([QUERY_PREFIX + q for q in queries]) if cfg["vector"] else [None] * len(queries)

    # 3. поиск (база)
    with ThreadPoolExecutor(max_workers=8) as ex:
        pools = list(ex.map(lambda p: retrieve(cfg, p[0], p[1]), zip(queries, embs)))

    # 4. реранк (GPU, последовательно по вопросам)
    tops = []
    for c, rows in zip(cases, pools):
        if cfg["rerank"] and rows:
            sc = rerank_batch([(c["question"], r["content"][:1800]) for r in rows])
            order = sorted(range(len(rows)), key=lambda i: -sc[i])
            tops.append([rows[i] for i in order[:cfg["topk"]]])
        else:
            tops.append(rows[:cfg["topk"]])
    print(f"  поиск готов за {time.time()-t0:.0f}s", flush=True)

    # 5. метрики поиска
    golds = gold_ids(cases)
    ranks = []
    for g, rows in zip(golds, tops):
        ids = [r["id"] for r in rows]
        ranks.append(ids.index(g) + 1 if g in ids else None)

    # 6. ответ + оценка (сеть)
    cache = {}
    if cache_path(a.name).exists():
        cache = json.load(open(cache_path(a.name), encoding="utf-8"))
        print(f"  из кэша: {len(cache)}", flush=True)
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=12) as ex:
        aj = list(ex.map(answer_and_judge,
                         [(c, t, cfg, cache, lock) for c, t in zip(cases, tops)]))

    res = []
    for c, rank, (ans, j), rows in zip(cases, ranks, aj, tops):
        res.append({"question": c["question"], "topic": c["topic"], "lang": c["language"],
                    "rank": rank, "score": j.get("score"), "covered": j.get("covered"),
                    "total": j.get("total"), "hallucination": j.get("hallucination"),
                    "why": j.get("why"), "answer": ans[:2000],
                    "retrieved": [r["id"] for r in rows[:10]]})

    sc = [r["score"] for r in res if isinstance(r["score"], (int, float))]
    cov = [(r["covered"] / r["total"]) for r in res
           if isinstance(r.get("covered"), (int, float)) and r.get("total")]
    summary = {
        "name": a.name, "cfg": cfg, "n": len(res),
        "recall@5": round(sum(1 for r in ranks if r and r <= 5) / len(ranks), 3),
        "recall@15": round(sum(1 for r in ranks if r and r <= 15) / len(ranks), 3),
        "mrr": round(sum(1 / r for r in ranks if r) / len(ranks), 3),
        "answer_score": round(statistics.mean(sc), 2) if sc else None,
        "good_answers": round(sum(1 for s in sc if s >= 4) / len(sc), 3) if sc else None,
        "fact_coverage": round(statistics.mean(cov), 3) if cov else None,
        "hallucinations": sum(1 for r in res if r["hallucination"]),
        "elapsed_s": round(time.time() - t0),
    }
    json.dump({"summary": summary, "cases": res},
              open(HERE / f"eval_{a.name}.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
