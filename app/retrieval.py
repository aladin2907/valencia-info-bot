"""Поиск тредов: вектор → пул кандидатов → реранкер → топ-N.

Ровно та связка, что победила в замере (docs/QUALITY.md, раунды 3 и 5).
Полнотекстовая ветка и свежесть остаются доступны через настройки, но по
умолчанию не участвуют: замер не показал от них пользы.
"""
from dataclasses import dataclass, field
from datetime import datetime

from app import config, db, models_client


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

    if config.USE_RERANK:
        scores = models_client.rerank(question, [t.content for t in threads])
        for t, s in zip(threads, scores):
            t.rerank_score = s
        # если реранкер вернул меньше оценок, чем кандидатов, безоценочные
        # уходят в конец, а не роняют сортировку
        threads.sort(key=lambda t: (t.rerank_score if t.rerank_score is not None
                                    else float("-inf")), reverse=True)

    return Retrieved(threads=threads[:top_k], pool_size=len(rows))


def build_context(threads: list[Thread]) -> str:
    """Даты идут в промпт: по ним модель отличает свежее от устаревшего."""
    return "\n\n---\n\n".join(
        f"[обсуждение от {t.last_activity_at.date()}]\n{t.content[:config.THREAD_CHARS]}"
        for t in threads
    )
