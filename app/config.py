"""Настройки. Значения по умолчанию — ровно те, что дали лучшие цифры в замере.

Всё, что не проверено замером (переформулировка запроса, вес свежести, слой
фактов, проверка по официальным источникам), по умолчанию ВЫКЛЮЧЕНО. Правило из
docs/QUALITY.md: изменение остаётся, только если метрики выросли.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


# --- база -------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://valencia:test@localhost:5432/valencia")
DB_POOL_MIN = _i("DB_POOL_MIN", 1)
DB_POOL_MAX = _i("DB_POOL_MAX", 8)

# --- модели на своём железе -------------------------------------------------
MODELS_URL = os.getenv("MODELS_URL", "http://localhost:8081")
MODELS_TIMEOUT = _f("MODELS_TIMEOUT", 120.0)
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")

# --- LLM (OpenRouter или любой совместимый) ---------------------------------
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("OPENROUTER_MODEL") or os.getenv("LLM_MODEL", "stealth/ox-alpha")
LLM_TIMEOUT = _f("LLM_TIMEOUT", 300.0)

# --- поиск: измеренная конфигурация -----------------------------------------
POOL_SIZE = _i("POOL_SIZE", 50)          # кандидатов из базы
CONTEXT_THREADS = _i("CONTEXT_THREADS", 8)  # сколько тредов уходит в ответ
ANSWER_MAX_TOKENS = _i("ANSWER_MAX_TOKENS", 3000)  # с 1200 ответы обрывались
THREAD_CHARS = _i("THREAD_CHARS", 2500)  # обрезка треда в промпте

USE_RERANK = _b("USE_RERANK", True)      # главный рычаг качества, раунд 3
USE_FTS = _b("USE_FTS", False)           # замер: пользы нет, раунды 2 и 4
FTS_WEIGHT = _f("FTS_WEIGHT", 0.0)

# --- не проверено замером: по умолчанию выключено ---------------------------
USE_QUERY_REWRITE = _b("USE_QUERY_REWRITE", False)
USE_RECENCY = _b("USE_RECENCY", False)
RECENCY_HALF_LIFE_DAYS = _f("RECENCY_HALF_LIFE_DAYS", 365.0)
RECENCY_FLOOR = _f("RECENCY_FLOOR", 0.6)
USE_FACTS = _b("USE_FACTS", False)

# --- бот --------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_URL = os.getenv("API_URL", "http://localhost:8080")
RATE_LIMIT_SECONDS = _i("RATE_LIMIT_SECONDS", 300)

# --- ingest -----------------------------------------------------------------
GROUPS = [g.strip() for g in os.getenv(
    "GROUPS", "it_ua_valencia,matusi_valencia,valencia_parents_kids_schools").split(",") if g.strip()]
THREAD_REBUILD_DAYS = _i("THREAD_REBUILD_DAYS", 14)
EMBED_BATCH = _i("EMBED_BATCH", 64)


def recency_args() -> tuple[float, float]:
    """Аргументы свежести для hybrid_search. Выключено — множитель ровно 1."""
    if USE_RECENCY:
        return RECENCY_HALF_LIFE_DAYS, RECENCY_FLOOR
    return 1e9, 1.0
