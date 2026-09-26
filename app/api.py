"""HTTP-ядро. Telegram-бот и мобильное приложение — два равноправных клиента."""
import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import answer as answer_mod
from app import config, db, llm, models_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


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


def _user(body: AskIn) -> int | None:
    """Пользователь для журнала вопросов; лимит превышен — 429."""
    if not body.user_id:
        return None
    user_id, wait = _rate_limit(body.platform, body.user_id)
    if wait:
        raise HTTPException(429, f"Следующий вопрос можно задать через {wait // 60 + 1} мин.")
    return user_id


@app.post("/ask", response_model=AskOut)
def ask(body: AskIn):
    user_id = _user(body)
    try:
        result = answer_mod.ask(body.question, user_id=user_id, groups=body.groups)
    except llm.LLMError as e:
        raise HTTPException(503, f"Модель сейчас недоступна: {e}")
    return AskOut(answer=result.answer, sources=result.sources,
                  facts_used=result.facts_used, latency_ms=result.latency_ms)


@app.post("/ask/stream")
def ask_stream(body: AskIn):
    """То же, что /ask, но по ходу работы, одна строка JSON на событие: перед
    выжимкой, интернетом и сборкой — {"stage": ...}, последней — поля AskOut или
    {"error": ...}. Лимит проверяется до начала: 429 приходит обычным ответом."""
    user_id = _user(body)

    def lines():
        try:
            for item in answer_mod.ask_steps(body.question, user_id=user_id, groups=body.groups):
                if isinstance(item, str):
                    event = {"stage": item}
                else:
                    event = AskOut(answer=item.answer, sources=item.sources,
                                   facts_used=item.facts_used,
                                   latency_ms=item.latency_ms).model_dump()
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        except llm.LLMError as e:
            yield json.dumps({"error": f"Модель сейчас недоступна: {e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")
