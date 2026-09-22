"""Поиск тредов: вектор + полнотекст → пул кандидатов → реранкер → топ-N по дате.

Реранкер — из замера (docs/QUALITY.md, раунды 3 и 5). Полнотекстовая ветка и
порядок по дате включены решением владельца и замером пока не подтверждены:
на 50 вопросах полнотекст с весом 0.3 не менял метрики ни в одну сторону.
Свежесть как множитель ранга (USE_RECENCY) — отдельная, до сих пор выключенная
история: она меняет отбор, а порядок по дате — только чтение.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from app import config, db, jev_client, models_client

logger = logging.getLogger(__name__)


@dataclass
class Thread:
    id: int
    group_slug: str
    content: str
    last_activity_at: datetime
    tg_link: str | None
    score: float
    rerank_score: float | None = None

    def as_source(self) -> dict:
        return {
            "thread_id": self.id,
            "group": self.group_slug,
            "date": self.last_activity_at.date().isoformat(),
            "link": self.tg_link,
            "excerpt": self.content[:200],
        }


@dataclass
class Retrieved:
    threads: list[Thread]
    pool_size: int
    facts: list[dict] = field(default_factory=list)


def _rerank_scores(question: str, documents: list[str]) -> list[float | None] | None:
    """Score documents with the configured backend.

    Returns None when the backend is unusable — call failed, key missing, or
    too few documents got a score to trust the order — the caller then keeps
    the hybrid-search order, exactly like today with no reranker. Otherwise
    returns one entry per document (None for a document that could not be
    scored); the caller reorders only the scored documents and leaves the rest
    where the hybrid search put them. Same rule for both backends: a reranker
    failure of any kind falls back to the hybrid order instead of surfacing an
    error to the user.
    """
    backend = config.RERANK_BACKEND
    total = len(documents)
    started = time.monotonic()
    fail_summary = ""

    if backend == "jev":
        if not config.JEV_API_KEY:
            logger.warning("rerank backend=jev skipped: JEV_API_KEY is empty, hybrid order kept")
            return None
        try:
            scores, fail_summary = jev_client.rank(question, documents)
        except Exception as e:
            logger.warning("rerank backend=jev call failed (%s), hybrid order kept", type(e).__name__)
            return None
    else:
        try:
            scores = models_client.rerank(question, documents)
        except Exception as e:
            logger.warning("rerank backend=local call failed (%s), hybrid order kept", type(e).__name__)
            return None

    scored = sum(1 for s in scores if s is not None)
    elapsed = time.monotonic() - started
    if scored < config.CONTEXT_THREADS:
        logger.info(
            "rerank backend=%s scored=%d/%d elapsed=%.2fs -> hybrid order kept%s",
            backend, scored, total, elapsed,
            f" (reason={fail_summary})" if fail_summary else "",
        )
        return None
    logger.info("rerank backend=%s scored=%d/%d elapsed=%.2fs", backend, scored, total, elapsed)
    return scores


def search(question: str, top_k: int | None = None,
           groups: list[str] | None = None,
           date_from: datetime | None = None,
           date_to: datetime | None = None) -> Retrieved:
    top_k = top_k or config.CONTEXT_THREADS
    half_life, floor = config.recency_args()

    embedding = models_client.embed_one(question, kind="query")

    rows = db.query(
        """SELECT id, group_slug, content, last_activity_at, tg_link, score
           FROM hybrid_search(%s, %s::vector, %s, %s, %s, 50, %s, %s, %s, %s, %s)""",
        (
            question if config.USE_FTS else None,
            str(embedding),
            config.POOL_SIZE,
            config.FTS_WEIGHT if config.USE_FTS else 0.0,
            1.0,
            half_life,
            floor,
            groups,
            date_from,
            date_to,
        ),
    )
    threads = [Thread(**r) for r in rows]
    if not threads:
        return Retrieved(threads=[], pool_size=0)

    if config.USE_RERANK and config.RERANK_BACKEND != "off":
        scores = _rerank_scores(question, [t.content for t in threads])
        if scores is not None:
            for t, s in zip(threads, scores):
                t.rerank_score = s
            # стабильная частичная сортировка: переставляем между собой только
            # оценённые треды, неоценённые остаются на своих гибридных местах —
            # иначе несколько случайных тредов без оценки вытесняют хороший
            # гибридный топ (см. knowledge/decisions/2026-09-21-jev-reranker.md)
            scored_positions = [i for i, s in enumerate(scores) if s is not None]
            scored_threads = sorted(
                (threads[i] for i in scored_positions),
                key=lambda t: t.rerank_score,
                reverse=True,
            )
            for pos, t in zip(scored_positions, scored_threads):
                threads[pos] = t
        # scores is None: реранкер недоступен или оценил меньше CONTEXT_THREADS
        # тредов — оставляем гибридный порядок как есть

    best = threads[:top_k]
    if config.CONTEXT_SORT_BY_DATE:
        # порядок в промпте — по дате; отбор тредов при этом остаётся за
        # реранкером, дата решает только что модель прочтёт первым
        best.sort(key=lambda t: t.last_activity_at, reverse=True)

    return Retrieved(threads=best, pool_size=len(rows))


def build_context(threads: list[Thread]) -> str:
    """Даты идут в промпт: по ним модель отличает свежее от устаревшего."""
    return "\n\n---\n\n".join(
        f"[обсуждение от {t.last_activity_at.date()}]\n{t.content[:config.THREAD_CHARS]}"
        for t in threads
    )
