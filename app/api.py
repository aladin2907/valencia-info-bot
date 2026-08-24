"""HTTP-ядро. Telegram-бот и мобильное приложение — два равноправных клиента."""
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import answer as answer_mod
from app import config, db, llm, models_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.pool()
    yield
    db.close_pool()


app = FastAPI(title="Valencia Info Bot API", version="1.0", lifespan=lifespan)


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    user_id: str | None = None
    platform: str = "telegram"
    lang: str | None = None
    groups: list[str] | None = None


class AskOut(BaseModel):
    answer: str
    sources: list[dict]
    facts_used: list[dict]
    latency_ms: int


@app.get("/health")
def health():
    out = {"status": "ok", "db": "?", "models": "?"}
    try:
        db.query("SELECT 1 AS ok")
        out["db"] = "ok"
    except Exception as e:
        out["db"] = f"error: {e}"
        out["status"] = "degraded"
    try:
        out["models"] = models_client.health().get("status", "?")
    except Exception as e:
        out["models"] = f"error: {e}"
        out["status"] = "degraded"
    return out


@app.get("/stats")
def stats():
    rows = db.query(
        """SELECT (SELECT count(*) FROM threads) AS threads,
                  (SELECT count(*) FROM thread_embeddings WHERE status='ready') AS embedded,
                  (SELECT count(*) FROM messages) AS messages,
                  (SELECT max(last_activity_at) FROM threads) AS freshest""")
    return rows[0]


def _rate_limit(platform: str, external_id: str) -> tuple[int, int]:
    """Возвращает (user_id, сколько секунд ждать). 0 — можно отвечать.

    Разница считается целиком в SQL. Если сравнивать время базы со временем
    приложения, новый пользователь получает отказ на первом же вопросе: строка
    создаётся временем базы, а оно всегда чуть позже снятого в приложении.
    """
    rows = db.query(
        """INSERT INTO users (platform, external_id)
           VALUES (%s, %s)
           ON CONFLICT (platform, external_id) DO UPDATE
             SET last_interaction_at = now()
           RETURNING id,
                     greatest(0, ceil(extract(epoch FROM
                         (next_allowed_message_at - now()))))::int AS wait""",
        (platform, external_id),
    )
    user = rows[0]
    if user["wait"] > 0:
        return user["id"], user["wait"]
    db.execute(
        """UPDATE users
              SET next_allowed_message_at = now() + %s::interval,
                  message_count_today = message_count_today + 1
            WHERE id = %s""",
        (timedelta(seconds=config.RATE_LIMIT_SECONDS), user["id"]),
    )
    return user["id"], 0


@app.post("/ask", response_model=AskOut)
def ask(body: AskIn):
    user_id = None
    if body.user_id:
        user_id, wait = _rate_limit(body.platform, body.user_id)
        if wait:
            raise HTTPException(429, f"Следующий вопрос можно задать через {wait // 60 + 1} мин.")
    try:
        result = answer_mod.ask(body.question, user_id=user_id, groups=body.groups)
    except llm.LLMError as e:
        raise HTTPException(503, f"Модель сейчас недоступна: {e}")
    return AskOut(answer=result.answer, sources=result.sources,
                  facts_used=result.facts_used, latency_ms=result.latency_ms)
