"""Client for the Jev decision-model reranker (OpenRouter alpha/decisions API).

One HTTP call per document, run in parallel. The alternative measured first —
the whole pool as one state, threads grouped by ~25 per call — was discarded:
it produced a visibly worse order (Spearman agreement with our bge reranker,
top-30 overlap) than one-thread-per-call. See
knowledge/reports/jev-rerank/jev_variants_test.py and
knowledge/decisions/2026-09-21-jev-reranker.md for the comparison.
"""
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from app import config

_client: httpx.Client | None = None

# Short pause before a retry after a 429 (rate limit), seconds.
_RETRY_AFTER_429 = 0.5

# Statuses that mean "this key or request will never work": no point asking
# again for the remaining documents of the same batch.
_HARD_STATUSES = frozenset({"400", "401", "402", "403", "404"})
_HARD_FAIL_LIMIT = 5


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            # Read timeout is the per-call budget (JEV_TIMEOUT); the pool timeout
            # is how long a thread waits for a free connection from the shared
            # pool — kept short so a second concurrent question doesn't sit idle
            # for the same duration as a slow Jev call.
            timeout=httpx.Timeout(connect=5.0, read=config.JEV_TIMEOUT,
                                   write=config.JEV_TIMEOUT, pool=5.0),
            headers={
                "Authorization": f"Bearer {config.JEV_API_KEY}",
                "Content-Type": "application/json",
            },
            # Enough connections for several concurrent questions, not just one.
            limits=httpx.Limits(max_connections=config.JEV_CONCURRENCY * 4,
                                 max_keepalive_connections=config.JEV_CONCURRENCY),
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


def _call_one(question: str, document: str, deadline: float,
              abort: threading.Event) -> tuple[float | None, str | None]:
    """One thread, one question. Two attempts total (one retry); None if both fail.

    Retries only network errors, timeouts and 5xx — a stale key or bad request
    (4xx other than 429) returns None right away instead of repeating a call
    that cannot succeed. A 429 gets one short pause before the retry. Each
    attempt is skipped once the shared rank() budget (JEV_BUDGET) has passed,
    or once rank() has given up on the whole batch (see abort).
    Returns (score, reason) — reason is set (a status code or exception class
    name) whenever score is None, for rank() to aggregate into one log line.
    """
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
    reason = "budget_exceeded"
    for attempt in range(2):
        if abort.is_set():
            return None, "aborted"
        if time.monotonic() >= deadline:
            return None, reason
        try:
            r = client.post(config.JEV_URL, json=body)
        except httpx.TimeoutException:
            reason = "timeout"
            continue
        except httpx.RequestError as e:
            reason = type(e).__name__
            continue
        if r.status_code < 400:
            try:
                return _value((r.json().get("answers") or {}).get("useful")), None
            except Exception as e:
                return None, type(e).__name__
        if r.status_code == 429:
            reason = "429"
            if attempt == 0:
                time.sleep(_RETRY_AFTER_429)
            continue
        if r.status_code >= 500:
            reason = str(r.status_code)
            continue
        return None, str(r.status_code)  # other 4xx — no retry, e.g. a stale key
    return None, reason


def rank(question: str, documents: list[str]) -> tuple[list[float | None], str]:
    """Score each document's usefulness for the question, in parallel.

    Returns (scores, fail_summary). scores has one entry per document, same
    order as input; a document Jev could not score gets None. fail_summary is
    empty when every document got a score, otherwise a short aggregate like
    "timeout=3 500=1" over the documents that did not — meant for a single
    fallback log line, never one line per call. This function never raises, to
    keep search working without Jev.

    The whole call is bounded by JEV_BUDGET seconds (wall clock, not per call):
    once the deadline passes, calls still in flight are left to finish but no
    further attempt is started. A key that is rejected outright (no credit,
    revoked, wrong model) would otherwise cost one pointless call per document,
    so after _HARD_FAIL_LIMIT such answers the rest of the batch is skipped.
    """
    if not documents:
        return [], ""
    deadline = time.monotonic() + config.JEV_BUDGET
    reasons: list[str] = []
    reasons_lock = threading.Lock()
    abort = threading.Event()
    hard_failures = 0

    def _scored(doc: str) -> float | None:
        nonlocal hard_failures
        score, reason = _call_one(question, doc, deadline, abort)
        if reason is not None:
            with reasons_lock:
                reasons.append(reason)
                if reason in _HARD_STATUSES:
                    hard_failures += 1
                    if hard_failures >= _HARD_FAIL_LIMIT:
                        abort.set()
        return score

    with ThreadPoolExecutor(max_workers=config.JEV_CONCURRENCY) as pool:
        scores = list(pool.map(_scored, documents))

    fail_summary = ""
    if reasons:
        fail_summary = " ".join(f"{reason}={n}" for reason, n in Counter(reasons).most_common())
    return scores, fail_summary
