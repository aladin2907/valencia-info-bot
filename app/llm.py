"""Обёртка над LLM. Любой OpenAI-совместимый эндпоинт (по умолчанию OpenRouter)."""
import json
import random
import re
import time

import httpx

from app import config

_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=config.LLM_BASE_URL,
            timeout=config.LLM_TIMEOUT,
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}",
                     "Content-Type": "application/json"},
        )
    return _client


class LLMError(RuntimeError):
    pass


def complete(prompt: str, system: str = "", max_tokens: int = 1500,
             temperature: float = 0.2, attempts: int = 4) -> str:
    """Ответ модели. На 429 (лимит запросов) ждём с нарастающей паузой —
    без этого повтор прилетает в тот же лимит и запрос теряется."""
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    body = {"model": config.LLM_MODEL, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    last = ""
    for a in range(attempts):
        try:
            r = client().post("/chat/completions", json=body)
            if r.status_code == 429:
                raise httpx.HTTPStatusError("429", request=r.request, response=r)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if content:
                return content
            last = "пустой ответ модели"
        except Exception as e:  # сеть, 429, кривой JSON — лечится повтором
            last = str(e)
        if a < attempts - 1:
            time.sleep(min(60, 5 * (2 ** a)) + random.uniform(0, 3))
    raise LLMError(last)


def complete_json(prompt: str, system: str = "", max_tokens: int = 900) -> dict | None:
    try:
        text = complete(prompt, system=system, max_tokens=max_tokens)
    except LLMError:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
