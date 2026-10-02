"""Настройки. Значения по умолчанию — ровно те, что дали лучшие цифры в замере.

Всё, что не проверено замером (переформулировка запроса, вес свежести, слой
фактов, проверка по официальным источникам), по умолчанию ВЫКЛЮЧЕНО. Правило из
docs/QUALITY.md: изменение остаётся, только если метрики выросли.
"""
import logging
import os
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip()
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number, using default %s", name, raw, default)
        return default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using default %s", name, raw, default)
        return default


# --- база -------------------------------------------------------------------
_DB_LOCAL_DEFAULT = "postgresql://valencia:test@localhost:5432/valencia"


# Required when composing the URL from discrete vars. DB_PORT/DB_NAME have safe
# defaults and DB_SSLMODE is optional, so they are not in this list.
_DB_REQUIRED = ("DB_USER", "DB_PASSWORD", "DB_HOST")


def _database_url() -> str:
    """Postgres connection string, by priority:

    1. DATABASE_URL set and non-empty -> use as-is (local dev, docker-compose,
       manual overrides).
    2. Else, if any DB_* var is present -> compose from the discrete vars. Fail
       fast if a required one is missing, so a misconfigured deployment stops
       with a clear error instead of silently connecting elsewhere.
    3. Else (nothing set) -> local default, for a bare dev machine.

    DB_USER and DB_PASSWORD are URL-encoded (quote, safe=""): a password can
    contain / @ : + = which otherwise break the connection string.
    """
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        return explicit

    db_vars = {k: os.getenv(k, "").strip()
               for k in ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_NAME", "DB_SSLMODE")}
    if any(db_vars.values()):
        missing = [k for k in _DB_REQUIRED if not db_vars[k]]
        if missing:
            raise RuntimeError(
                "DB_* configuration is incomplete, missing: " + ", ".join(missing)
                + ". Set these, or provide DATABASE_URL for local dev."
            )
        user = quote(db_vars["DB_USER"], safe="")
        password = quote(db_vars["DB_PASSWORD"], safe="")
        host = db_vars["DB_HOST"]
        port = db_vars["DB_PORT"] or "5432"
        name = db_vars["DB_NAME"] or "valencia"
        sslmode = db_vars["DB_SSLMODE"] or "require"
        return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"

    return _DB_LOCAL_DEFAULT


DATABASE_URL = _database_url()
DB_POOL_MIN = _i("DB_POOL_MIN", 1)
DB_POOL_MAX = _i("DB_POOL_MAX", 8)
# Cap the connection/handshake wait so a stale credential or unreachable DB
# fails fast (seconds) instead of hanging -- the health check must be able to
# report a dead DB promptly.
DB_CONNECT_TIMEOUT = _i("DB_CONNECT_TIMEOUT", 5)

# --- модели на своём железе -------------------------------------------------
MODELS_URL = os.getenv("MODELS_URL", "http://localhost:8081")
MODELS_TIMEOUT = _f("MODELS_TIMEOUT", 120.0)
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")

# --- LLM: OpenRouter, OpenAI напрямую или любой совместимый ------------------
# Явные LLM_* всегда главнее: так переключение на OpenAI — три строки в .env,
# а старые ключи OpenRouter можно не удалять.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = (os.getenv("LLM_API_KEY")
               or ("api.openai.com" in LLM_BASE_URL and os.getenv("OPENAI_API_KEY"))
               or os.getenv("OPENROUTER_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("OPENROUTER_MODEL", "minimax/minimax-m3:free")
# Собственный потолок API должен укладываться в терпение бота: он ждёт ответа
# 300 с (bot/main.py). При LLM_TIMEOUT=300 и четырёх попытках худший случай был
# около 20 минут — бот давно ушёл, а запрос продолжал жечь токены. Измеренный
# ответ занимает около 7 с, так что 60 с на попытку — восьмикратный запас.
LLM_TIMEOUT = _f("LLM_TIMEOUT", 60.0)
LLM_ATTEMPTS = _i("LLM_ATTEMPTS", 3)

# --- поиск -------------------------------------------------------------------
# 100 кандидатов из базы: при 30 тредах в ответе пул в 50 оставлял реранкеру
# слишком мало работы. Платим временем — реранкер оценивает вдвое больше пар.
POOL_SIZE = _i("POOL_SIZE", 100)
# 30 тредов в ответе — решение владельца (05.09.2026). Замер был на 8; при 30
# из пула в 50 реранкер отсеивает лишь двоих из трёх, то есть работает вполсилы.
# Вернуть его влияние можно, подняв POOL_SIZE до 100 — ценой времени ответа.
CONTEXT_THREADS = _i("CONTEXT_THREADS", 30)
ANSWER_MAX_TOKENS = _i("ANSWER_MAX_TOKENS", 3000)  # с 1200 ответы обрывались
THREAD_CHARS = _i("THREAD_CHARS", 2500)  # обрезка треда в промпте
# Треды в промпте идут от свежих к старым, а не по релевантности: так модель
# видит, что новее, и не выдаёт прошлогоднюю цену за сегодняшнюю.
CONTEXT_SORT_BY_DATE = _b("CONTEXT_SORT_BY_DATE", True)

USE_RERANK = _b("USE_RERANK", True)      # главный рычаг качества, раунд 3
# Гибридный поиск включён по решению владельца. Вес 0.3 — измеренный безвредный:
# при равном весе выдача была вдвое хуже (раунд 2), при 0.3 — как без него (раунд 4).
USE_FTS = _b("USE_FTS", True)
FTS_WEIGHT = _f("FTS_WEIGHT", 0.3)

# --- реранкер: свой bge или внешняя модель решений Jev ----------------------
# RERANK_BACKEND выбирает модель; USE_RERANK остаётся главным выключателем —
# при USE_RERANK=false реранкера нет независимо от RERANK_BACKEND. По умолчанию
# "local", чтобы ничего не менялось без явной настройки (см. decisions/2026-09-21-jev-reranker.md).
# Пустая строка (незаполненная переменная в .env) тоже означает "local".
_RERANK_BACKENDS = ("off", "local", "jev")
RERANK_BACKEND = (os.getenv("RERANK_BACKEND") or "local").strip().lower()
if RERANK_BACKEND not in _RERANK_BACKENDS:
    raise ValueError(
        f"RERANK_BACKEND={RERANK_BACKEND!r} is invalid, expected one of {_RERANK_BACKENDS}"
    )
JEV_API_KEY = os.getenv("JEV_API_KEY", "")
JEV_URL = os.getenv("JEV_URL") or "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = os.getenv("JEV_MODEL") or "~typesafe/jev-latest"
JEV_CHARS = _i("JEV_CHARS", 1200)        # обрезка треда для оценки Jev
JEV_CONCURRENCY = _i("JEV_CONCURRENCY", 12)  # параллельных вызовов, один тред на вызов
JEV_TIMEOUT = _f("JEV_TIMEOUT", 10.0)    # замер: типичный вызов ~0.3с
JEV_BUDGET = _f("JEV_BUDGET", 60.0)      # общий бюджет rank() на весь пул, секунд

# --- ответ по схеме n8n (decisions/2026-09-24-n8n-answer-pipeline.md) --------
# Переформулировка включена, как в n8n. Без ключа Perplexity ответ собирается
# из опыта чата — пользователь без ответа не остаётся.
USE_QUERY_REWRITE = _b("USE_QUERY_REWRITE", True)
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
PERPLEXITY_MODEL = os.getenv("PERPLEXITY_MODEL") or "sonar"
PERPLEXITY_TIMEOUT = _f("PERPLEXITY_TIMEOUT", 30.0)
# Диалог: сколько последних вопросов пользователя с ответами бота помнить.
# 0 — каждое сообщение отдельный вопрос, как раньше.
DIALOG_HISTORY = _i("DIALOG_HISTORY", 5)

# --- не проверено замером: по умолчанию выключено ---------------------------
USE_RECENCY = _b("USE_RECENCY", False)
RECENCY_HALF_LIFE_DAYS = _f("RECENCY_HALF_LIFE_DAYS", 365.0)
RECENCY_FLOOR = _f("RECENCY_FLOOR", 0.6)
# Слой фактов (таблица facts, функция match_facts) в схеме есть, но пока никем
# не наполняется — флага для него нет намеренно: переключатель, который ничего
# не включает, хуже его отсутствия. Появится ночное извлечение фактов — появится флаг.

# --- бот --------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Токен боевого бота сейчас занят n8n-workflow. Бот не снимет чужой webhook сам:
# переключение — осознанное действие, не побочный эффект запуска.
ALLOW_WEBHOOK_TAKEOVER = _b("ALLOW_WEBHOOK_TAKEOVER", False)
API_URL = os.getenv("API_URL", "http://localhost:8080")
# Пауза между вопросами одного пользователя, от вопроса до вопроса: 3 минуты
# (решение владельца 26.09, было 5). 0 — без лимита.
RATE_LIMIT_SECONDS = _i("RATE_LIMIT_SECONDS", 180)

# --- ingest -----------------------------------------------------------------
GROUPS = [g.strip() for g in os.getenv(
    "GROUPS", "it_ua_valencia,matusi_valencia,valencia_parents_kids_schools").split(",") if g.strip()]


def _chats() -> dict[str, int | str]:
    """Наши имена групп ↔ адреса в Telegram: «slug=@username,slug=-100...».

    Внутренние имена (it_ua_valencia) в Telegram ничего не значат, а у части
    групп нет и username — остаётся числовой id. Держим это в настройках, а не
    в коде: id приватных групп в открытый репозиторий класть незачем.
    """
    out: dict[str, int | str] = {}
    for pair in os.getenv("TG_CHATS", "").split(","):
        slug, _, peer = pair.partition("=")
        slug, peer = slug.strip(), peer.strip()
        if slug and peer:
            out[slug] = int(peer) if peer.lstrip("-").isdigit() else peer
    return out


TG_CHATS = _chats()
THREAD_REBUILD_DAYS = _i("THREAD_REBUILD_DAYS", 14)
EMBED_BATCH = _i("EMBED_BATCH", 64)


def recency_args() -> tuple[float, float]:
    """Аргументы свежести для hybrid_search. Выключено — множитель ровно 1."""
    if USE_RECENCY:
        return RECENCY_HALF_LIFE_DAYS, RECENCY_FLOOR
    return 1e9, 1.0
