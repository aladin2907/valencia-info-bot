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


# Капризы модели, которые она уже назвала в ошибке: узнаём один раз на процесс.
# Без этого каждый вопрос заново тратил два запроса на одни и те же 400.
_quirks: dict[str, set[str]] = {}


def _apply_quirks(body: dict) -> None:
    q = _quirks.get(body["model"], set())
    if "max_completion_tokens" in q and "max_tokens" in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
    if "no_temperature" in q:
        body.pop("temperature", None)


def _relax(body: dict, error_text: str) -> bool:
    """Подгоняет запрос под капризы модели. True — есть что исправить и повторить."""
    q = _quirks.setdefault(body["model"], set())
    if "max_tokens" in error_text and "max_tokens" in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
        q.add("max_completion_tokens")
        return True
    if "temperature" in error_text and "temperature" in body:
        body.pop("temperature")
        q.add("no_temperature")
        return True
    return False


def complete(prompt: str, system: str = "", max_tokens: int = 1500,
             temperature: float = 0.2, attempts: int | None = None) -> str:
    """Ответ модели. На 429 (лимит запросов) ждём с нарастающей паузой —
    без этого повтор прилетает в тот же лимит и запрос теряется."""
    attempts = config.LLM_ATTEMPTS if attempts is None else attempts
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    body = {"model": config.LLM_MODEL, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    _apply_quirks(body)
    last = ""
    failed = 0
    while failed < attempts:
        try:
            r = client().post("/chat/completions", json=body)
            if r.status_code == 429:
                raise httpx.HTTPStatusError("429", request=r.request, response=r)
            if r.status_code == 400 and _relax(body, r.text):
                # рассуждающие модели OpenAI не принимают max_tokens и чужую
                # температуру; узнаём об этом только из текста ошибки. Подстройка
                # не считается попыткой: раньше она съедала две из трёх, и на
                # настоящий ответ оставалась одна. Цикл конечен — каждая
                # подстройка убирает поле из запроса, а полей два.
                continue
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if content:
                return content
            last = "пустой ответ модели"
        except Exception as e:  # сеть, 429, кривой JSON — лечится повтором
            last = str(e)
        failed += 1
        if failed < attempts:
            time.sleep(min(60, 5 * (2 ** (failed - 1))) + random.uniform(0, 3))
    raise LLMError(last)


def parse_json(text: str) -> dict | None:
    """JSON-объект из ответа модели, даже если он обёрнут в ```json и пояснения."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def complete_json(prompt: str, system: str = "", max_tokens: int = 900) -> dict | None:
    try:
        text = complete(prompt, system=system, max_tokens=max_tokens)
    except LLMError:
        return None
    return parse_json(text)
