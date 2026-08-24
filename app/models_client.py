"""Клиент к сервису моделей (models/server.py)."""
import httpx

from app import config

_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=config.MODELS_URL, timeout=config.MODELS_TIMEOUT)
    return _client


def embed(texts: list[str], kind: str = "query") -> list[list[float]]:
    r = client().post("/embed", json={"texts": texts, "kind": kind})
    r.raise_for_status()
    return r.json()["vectors"]


def embed_one(text: str, kind: str = "query") -> list[float]:
    return embed([text], kind=kind)[0]


def rerank(query: str, documents: list[str]) -> list[float]:
    if not documents:
        return []
    r = client().post("/rerank", json={"query": query, "documents": documents})
    r.raise_for_status()
    return r.json()["scores"]


def health() -> dict:
    r = client().get("/health")
    r.raise_for_status()
    return r.json()
