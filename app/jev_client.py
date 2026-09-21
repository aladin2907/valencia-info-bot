"""Client for the Jev decision-model reranker (OpenRouter alpha/decisions API).

One HTTP call per document, run in parallel. The alternative measured first —
the whole pool as one state, threads grouped by ~25 per call — was discarded:
it produced a visibly worse order (Spearman agreement with our bge reranker,
top-30 overlap) than one-thread-per-call. See
knowledge/reports/jev-rerank/jev_variants_test.py and
knowledge/decisions/2026-09-21-jev-reranker.md for the comparison.
"""
from concurrent.futures import ThreadPoolExecutor

import httpx

from app import config

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=config.JEV_TIMEOUT,
            headers={
                "Authorization": f"Bearer {config.JEV_API_KEY}",
                "Content-Type": "application/json",
            },
            limits=httpx.Limits(max_connections=config.JEV_CONCURRENCY + 4),
        )
    return _client


def _value(item) -> float | None:
    """The answer carries its value under a key named after the question type."""
    if isinstance(item, (int, float)):
        return float(item)
    if isinstance(item, dict):
        for key in ("noul", "score", "probability", "value"):
            if isinstance(item.get(key), (int, float)):
                return float(item[key])
        probs = item.get("probabilities")
        if isinstance(probs, dict):
            for key in ("true", "yes"):
                if key in probs:
                    return float(probs[key])
    return None


def _call_one(question: str, document: str) -> float | None:
    """One thread, one question. Two attempts total (one retry); None if both fail."""
    body = {
        "model": config.JEV_MODEL,
        "state": {"question": question, "discussion": document[: config.JEV_CHARS]},
        "questions": {
            "useful": {
                "type": "noul",
                "instructions": "Обсуждение помогает ответить на вопрос пользователя?",
                "criteria": {
                    "true": "в обсуждении есть ответ или его часть",
                    "false": "обсуждение о другом",
                },
            }
        },
    }
    client = _get_client()
    for _attempt in range(2):
        try:
            r = client.post(config.JEV_URL, json=body)
            if r.status_code < 400:
                return _value((r.json().get("answers") or {}).get("useful"))
        except Exception:
            pass  # network error, timeout or bad payload — retry once, then give up
    return None


def rank(question: str, documents: list[str]) -> list[float | None]:
    """Score each document's usefulness for the question, in parallel.

    Returns one entry per document, same order as input. A document Jev could
    not score (after the retry) gets None — the caller decides what to do with
    that, this function never raises to keep search working without Jev.
    """
    if not documents:
        return []
    with ThreadPoolExecutor(max_workers=config.JEV_CONCURRENCY) as pool:
        return list(pool.map(lambda doc: _call_one(question, doc), documents))
