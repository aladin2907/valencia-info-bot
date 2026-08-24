"""Сборка ответа: поиск → промпт → LLM. Промпты перенесены из старого n8n-workflow.

Порядок шагов повторяет измеренную конфигурацию. Шаги, которые замер не
проверял (переформулировка запроса, слой фактов), включаются настройками и по
умолчанию выключены.
"""
import time
from dataclasses import dataclass

from app import config, db, llm, retrieval

ANSWER_SYS = """Ты — бот-помощник чата "Жизнь в Валенсии": опытный местный житель, пишешь кратко и по-человечески.
ЯЗЫК ОТВЕТА — тот же, что у вопроса.
Отвечай ТОЛЬКО по содержимому обсуждений ниже. Используй 1-3 короткие цитаты участников.
Точно копируй адреса, телефоны, ссылки, цены, сроки, названия документов.
Если в обсуждениях ответа нет — напиши "Информация не найдена".
Без заголовков, списков и форматирования — единый связный текст."""

REWRITE_SYS = """Ты — оптимизатор запросов для поиска по обсуждениям в чатах Валенсии.
Убери вводные слова ("подскажите", "кто знает"), исправь опечатки, сохрани ключевые термины,
названия документов, имена, адреса. Добавь синонимы на русском И украинском.
Верни JSON: {"key_phrase": "<очищенный запрос + русская версия + украинская версия, одной строкой>"}"""

NOT_FOUND = "Информация не найдена"


@dataclass
class Answer:
    answer: str
    sources: list[dict]
    facts_used: list[dict]
    key_phrase: str | None
    latency_ms: int
    thread_ids: list[int]


def _rewrite(question: str) -> str:
    data = llm.complete_json(f"Текст: {question}", system=REWRITE_SYS, max_tokens=500)
    return (data or {}).get("key_phrase") or question


def ask(question: str, user_id: int | None = None,
        groups: list[str] | None = None) -> Answer:
    t0 = time.time()
    key_phrase = _rewrite(question) if config.USE_QUERY_REWRITE else None
    found = retrieval.search(key_phrase or question, groups=groups)

    if not found.threads:
        return Answer(NOT_FOUND, [], [], key_phrase,
                      int((time.time() - t0) * 1000), [])

    context = retrieval.build_context(found.threads)
    text = llm.complete(
        f"Вопрос: {question}\n\nОбсуждения:\n{context}",
        system=ANSWER_SYS,
        max_tokens=config.ANSWER_MAX_TOKENS,
    )

    result = Answer(
        answer=text.strip(),
        sources=[t.as_source() for t in found.threads],
        facts_used=found.facts,
        key_phrase=key_phrase,
        latency_ms=int((time.time() - t0) * 1000),
        thread_ids=[t.id for t in found.threads],
    )
    _log(question, result, user_id)
    return result


def _log(question: str, a: Answer, user_id: int | None) -> None:
    """Журнал вопросов — по нему потом видно, на чём бот промахивается."""
    try:
        db.execute(
            """INSERT INTO query_log (user_id, question, key_phrase, thread_ids,
                                      answer, latency_ms)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (user_id, question, a.key_phrase, a.thread_ids, a.answer, a.latency_ms),
        )
    except Exception:
        pass  # журнал не должен ронять ответ пользователю
