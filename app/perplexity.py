"""Веб-поиск по официальным источникам через Perplexity — шаг «Интернет» схемы n8n.

Сюда уходят только поисковые запросы на испанском, треды и переписка — никогда.
Любой сбой (нет ключа, таймаут, ошибка, пустой ответ) даёт None: ответ тогда
собирается из опыта чата.
"""
import logging

import httpx

from app import config

logger = logging.getLogger(__name__)

URL = "https://api.perplexity.ai/chat/completions"


def search(prompt: str) -> str | None:
    if not config.PERPLEXITY_API_KEY:
        return None
    try:
        r = httpx.post(
            URL,
            headers={"Authorization": f"Bearer {config.PERPLEXITY_API_KEY}"},
            json={"model": config.PERPLEXITY_MODEL,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=config.PERPLEXITY_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or None
    except Exception as e:
        logger.warning("perplexity failed: %s", str(e)[:300])
        return None
