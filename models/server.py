#!/usr/bin/env python3
"""Сервис моделей: эмбеддинги и реранкер на своём железе.

Два эндпоинта, оба синхронные — модели держат GIL и всё равно считают по очереди:

    POST /embed   {"texts": [...], "kind": "query"|"document"} -> {"vectors": [[...]]}
    POST /rerank  {"query": "...", "documents": [...]}         -> {"scores": [...]}

Настройки повторяют замер из docs/QUALITY.md: bge-m3 с окном 1024 токена
и bge-reranker-v2-m3. Менять их — значит менять то, что было измерено.
"""
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
MAX_SEQ_LEN = int(os.getenv("MAX_SEQ_LEN", "1024"))
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "8"))
RERANK_BATCH = int(os.getenv("RERANK_BATCH", "8"))
# сколько символов уходит в модель — обрезка ровно как в замере
DOC_CHARS = int(os.getenv("DOC_CHARS", "4000"))
RERANK_CHARS = int(os.getenv("RERANK_CHARS", "1800"))

_embedder = None
_reranker = None
_lock = threading.Lock()


def _device() -> str:
    """cuda → mps → cpu. На сервере обычно cpu, и это нормально."""
    forced = os.getenv("DEVICE")
    if forced:
        return forced
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _load():
    global _embedder, _reranker
    from sentence_transformers import CrossEncoder, SentenceTransformer
    dev = _device()
    t0 = time.time()
    _embedder = SentenceTransformer(EMBED_MODEL, device=dev)
    _embedder.max_seq_length = MAX_SEQ_LEN
    _reranker = CrossEncoder(RERANK_MODEL, device=dev)
    print(f"модели загружены на {dev} за {time.time() - t0:.0f}s", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load()
    yield


app = FastAPI(title="valencia-info-bot models", lifespan=lifespan)


class EmbedIn(BaseModel):
    texts: list[str] = Field(min_length=1)
    kind: str = "query"


class RerankIn(BaseModel):
    query: str
    documents: list[str]


@app.get("/health")
def health():
    return {"status": "ok" if _embedder is not None else "loading",
            "embed_model": EMBED_MODEL, "rerank_model": RERANK_MODEL,
            "device": _device(), "max_seq_len": MAX_SEQ_LEN}


@app.post("/embed")
def embed(body: EmbedIn):
    if _embedder is None:
        raise HTTPException(503, "модели ещё грузятся")
    limit = DOC_CHARS if body.kind == "document" else 2000
    texts = [t[:limit] for t in body.texts]
    with _lock:
        vecs = _embedder.encode(texts, batch_size=EMBED_BATCH, normalize_embeddings=True)
    return {"vectors": [v.tolist() for v in vecs], "model": EMBED_MODEL}


@app.post("/rerank")
def rerank(body: RerankIn):
    if _reranker is None:
        raise HTTPException(503, "модели ещё грузятся")
    if not body.documents:
        return {"scores": []}
    pairs = [(body.query, d[:RERANK_CHARS]) for d in body.documents]
    with _lock:
        scores = _reranker.predict(pairs, batch_size=RERANK_BATCH)
    return {"scores": [float(s) for s in scores], "model": RERANK_MODEL}
