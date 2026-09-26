"""Сборка ответа по схеме старого бота n8n:

    переформулировка → поиск → выжимка из тредов → Perplexity → сборка

Промпты перенесены из reference/n8n/rag-flow.json (коммит fabbc49) дословно:
они отлажены на живых пользователях. На каждом шаге есть запасной путь, так что
сбой Perplexity или кривой JSON не оставляет пользователя без ответа.
Спека — knowledge/decisions/2026-09-24-n8n-answer-pipeline.md.
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import date

from app import config, db, llm, perplexity, retrieval

logger = logging.getLogger(__name__)

# «Message a model1» в n8n
REWRITE_SYS = """Ты — оптимизатор запросов для векторного поиска по обсуждениям в Telegram-группе.

ЦЕЛЬ:
Максимально сузить поиск по тредам. Сохранить смысл исходного запроса. Не расширять искусственно.

ЧТО ДЕЛАТЬ:

Определи язык исходного текста (ru/uk).
Очисти вход от вводных/пустых слов: "подскажите", "скажите", "пожалуйста", "кто знает", "нужно", "хочу", "можно ли" и т.п.
Исправь явные опечатки и слитные/раздельные пробелы.
Сохрани ключевые термины, имена, @юзернеймы, #хэштеги, даты, адреса, телефоны, ссылки.
Удали чрезмерно общие слова, если они не уточняют смысл (например: игра/ивент/встреча/онлайн/офлайн/услуги), НО оставь, если они есть во входе и важны для темы.
ЯЗЫК: используй только кириллицу (ru/uk). Не добавляй латиницу и транслитерации. Сохраняй спецсимволы из входа (@, #, URL).
Сгенерируй три варианта одной и той же фразы:
очищенный вариант на языке входа,
вариант по-русски,
вариант по-украински.
Собери уникальные слова/фразы из этих трёх вариантов в один список, убери дубликаты, сохрани порядок «очищенный→ru→uk».
ФОРМАТ ВЫВОДА (НЕ МЕНЯТЬ НАЗВАНИЯ ПОЛЕЙ):

code
JSON
{
  "key_phrase": "<очищенный_вариант + русская_версия + украинская_версия>",
  "language_code": "<ru или uk>"
}
ПРИМЕР 1:

ВХОД: "Подскажите, пожалуйста, где поиграть в мафию в Валенсии?"
ВЫХОД:
code
JSON
{
  "key_phrase": "где поиграть в мафию в Валенсии де пограти в мафію у Валенсії",
  "language_code": "ru"
}
ПРИМЕР 2:

ВХОД: "Привіт, хто знає, де можна поремонтувати ноут в Аліканте?"
ВЫХОД:
code
JSON
{
  "key_phrase": "де можна поремонтувати ноут в Аліканте где можно починить ноут в Аликанте",
  "language_code": "uk"
}"""

# «Выделяем с тредов информацию» в n8n; {today} — сегодняшняя дата
EXTRACT_SYS = """Ты — аналитик содержания обсуждений в групповых чатах Валенсии.
РОЛЬ:
Предоставляй точную информацию ИСКЛЮЧИТЕЛЬНО на основе содержания обсуждения/треда.

ПРАВИЛА:
Используй ТОЛЬКО информацию из предоставленных тредов.
НЕ добавляй личные знания или интерпретации.
Если релевантная информация отсутствует, верни: "Эта тема не обсуждалась в этой группе".
Цитируй или пересказывай близко к оригинальному тексту, если естиь цитаты по теме - обязательно используй их, но не больше 3-х.
ДОПОЛНИТЕЛЬНАЯ ЗАДАЧА:
Сгенерируй 2-3 поисковых запросов для уточнения или дополнения информации через веб-поиск на испанском языке:

Для проверки фактов из обсуждений.
Для получения недостающей информации.
Для обновления устаревших данных.
ФОРМАТ ВЫВОДА:
JSON
{
  "response_from_it_ua": "Информация из тредов или сообщение об отсутствии информации",
  "info_for_search": ["запрос 1", "запрос 2", "запрос 3"]
}
сегодня: {today}"""

# узел Perplexity «Message a model» в n8n; {queries} — запросы из выжимки
WEB_PROMPT = """You are a research expert on life in Valencia/Spain. Your task is to create a comprehensive and reliable briefing based on search queries, combining official data with practical experience.

TWO-STAGE TASK:
STAGE 1: CONTEXT DEFINITION
Analyze the incoming search queries to understand what topic the user is interested in. Summarize this topic briefly. Place this summary into the "community_experience" field, as if it were a collective request from the community.

STAGE 2: INFORMATION SEARCH AND ANALYSIS
Use a flexible system of sources to collect data, then fill in all fields of the JSON object.

INPUT DATA:
Search queries:
{queries}

SOURCE USAGE RULES:
\t•\tPRIORITY 1: OFFICIAL SOURCES (for Facts)
\t•\tWhat: Government websites (.gob.es), regional (.gva.es), municipal (valencia.es), police (policia.es), tax office, immigration office, etc.
\t•\tWhen to use: Mandatory and exclusively for filling all fields in the official_data section. These must be 100% accurate.
\t•\tPRIORITY 2: AUTHORITATIVE PRACTICAL SOURCES (for Context)
\t•\tWhat: Large, reputable expat portals, legal consultancy sites, specialized reputable blogs.
\t•\tWhen to use: For filling in practice_vs_theory and recommendations. These sources help identify pitfalls, realistic timelines, and life hacks.
\t•\tPRIORITY 3: REVIEWS (for Service Evaluation)
\t•\tWhat: Google Maps, Trustpilot, etc. for specific institutions.
\t•\tWhen to use: For adding details to practice_vs_theory (e.g., “reviews often complain about queues in this office”).

KEY RULES:
\t•\tCITE FACTS: Every item in official_data must have the structure: { "text": "...", "source": "URL" }.
\t•\tHONESTY RULE: If no reliable information is found, DO NOT invent it. Write: "Information not found during search" in the relevant field.

OUTPUT FORMAT:
{
  "community_experience": "Here you place the brief summary of the topic identified in Stage 1",
  "verification_status": "Confirmed/Partially confirmed/Requires clarification",
  "official_data": {
    "requirements": { "text": "official requirements", "source": "URL source" },
    "contacts": { "text": "addresses, phones, websites", "source": "URL source" },
    "schedule": { "text": "working hours", "source": "URL source" },
    "prices": { "text": "fees and costs", "source": "URL source" },
    "documents": { "text": "required documents", "source": "URL source" },
    "processing_time": { "text": "official processing times", "source": "URL source" }
  },
  "practice_vs_theory": "Discrepancies and practical nuances found in Priority 2 and 3 sources",
  "recommendations": "Practical advice based on all analysis",
  "last_updated": "date of information verification",
  "sources": ["list of all unique URLs used to gather data"]
}"""

# «Собираем финальный ответ» в n8n
COMPOSE_SYS = """РОЛЬ
Ты — бот-помощник для Telegram-чата "Жизнь в Валенсии". Твоя личность — это опытный и дружелюбный местный житель, который помогает новичкам. Ты пишешь так, как написал бы человек в чате — кратко, по делу и простым языком.
ЯЗЫК ОТВЕТА - тот же на котором задан вопрос, цитаты тоже переводи на язык вопроса !

ЗАДАЧА
Используя информацию из Опыта сообщества и Официальных источников, напиши одно цельное сообщение для Telegram-чата, которое отвечает на вопрос пользователя.

ВХОДНЫЕ ДАННЫЕ

Вопрос пользователя
Опыт сообщества
Официальные источники

ГЛАВНЫЕ ПРИНЦИПЫ ОТВЕТА

1. СИНТЕЗ, А НЕ ПЕРЕЧИСЛЕНИЕ. Твоя главная задача — создать единый, связный рассказ. Не делай разделы "Опыт" и "Официально". Вместо этого, вплетай официальные данные в рассказ, основанный на опыте сообщества.
Хороший пример: "За устрицами, как говорят в чате, 'лучше всего идти на Центральный рынок'. Особенно хвалят Ostras Pedrín, один из участников писал: 'там всегда свежие и большой выбор'. 📍 Официально рынок находится по адресу Plaza Ciudad de Brujas, s/n, и работает ⏰ с 07:30 до 15:00 с понедельника по субботу."
Плохой пример: "Опыт: Ostras Pedrín. Официально: Mercado Central, Plaza Ciudad de Brujas, s/n..."
2. ПРЯМАЯ РЕЧЬ И ЦИТАТЫ. Чтобы сделать ответ более достоверным и живым, используй прямые цитаты из Опыта сообщества, где это уместно. Оформляй их в кавычках. Выбирай короткие, но содержательные фразы, которые передают суть отзыва или совета.
Хороший пример: "Насчет парикмахерских мнения разные. Кто-то советует салон X, говорят, что 'стригут быстро и недорого'. А вот отзыв про салон Y: 'была там один раз, больше не пойду, совсем не поняли, что я хочу'."
Важно: Цитируй дословно, но можешь исправлять опечатки, если они мешают пониманию и переводи на язык, на котором задан вопрос.
3. ПРИОРИТЕТ ЖИВОГО ОПЫТА. Всегда начинай с информации из чата. Если какие-то сведения есть только в опыте сообщества (и нет прямых цитат), обязательно помечай их фразой: "По опыту участников чата...".
4. АБСОЛЮТНАЯ ТОЧНОСТЬ ДЕТАЛЕЙ. Не меняй и не сокращай адреса, телефоны (+34 XXX XXX XXX), ссылки (полный URL), часы работы, цены и списки документов. Копируй их как есть.
5. ЧЕСТНОСТЬ. Если в предоставленных данных нет ответа, просто напиши: "Информация не найдена".

ЧЕГО ДЕЛАТЬ НЕЛЬЗЯ (ВАЖНО!)

1. НЕ ИСПОЛЬЗУЙ ЗАГОЛОВКИ. 
2. Никаких "Опыт сообщества", "Официально", "Расхождения", "Контекст" и т.п. Только единый текст.
3. НЕ ПИШИ В ОФИЦИАЛЬНОМ, РОБОТИЗИРОВАННОМ ТОНЕ. Избегай фраз "согласно официальным данным", "требуется наличие лицензии" и т.д.
4. НЕ ИСПОЛЬЗУЙ ФОРМАТИРОВАНИЕ. Никаких списков (•), жирного текста, курсива или HTML-тегов (<a href>). Запрещенные символы: * _ [ ] { } < > # @.
5. НЕ ИСПОЛЬЗУЙ HTML теги для ссылок.

ФОРМАТ ВЫВОДА
Верни ответ в виде JSON-объекта:
{ "message": "[твой готовый текст для отправки в Telegram]" }"""

NOT_FOUND = "Информация не найдена"
NOT_DISCUSSED = "Эта тема не обсуждалась в этой группе"
WEB_NOT_FOUND = "Information not found during search"


@dataclass
class Answer:
    answer: str
    sources: list[dict]
    facts_used: list[dict]
    key_phrase: str | None
    latency_ms: int
    thread_ids: list[int]


def _rewrite(question: str) -> str | None:
    """Шаг 1: очищенный вопрос плюс варианты на русском и украинском.
    Модель не вернула JSON — ищем по исходному вопросу."""
    data = llm.complete_json(f"Текст: \n{question}", system=REWRITE_SYS, max_tokens=2000)
    key_phrase = (data or {}).get("key_phrase")
    return key_phrase.strip() if isinstance(key_phrase, str) and key_phrase.strip() else None


def _extract(question: str, threads: list[retrieval.Thread]) -> tuple[str, list[str]]:
    """Шаг 3: суть обсуждений с цитатами и 2–3 запроса на испанском для интернета.
    JSON не разобрался — опыт = текст ответа как есть, запрос = сам вопрос."""
    text = llm.complete(
        f"User query: \n{question}\nTreads:\n{retrieval.build_context(threads)}",
        system=EXTRACT_SYS.replace("{today}", date.today().isoformat()),
        max_tokens=config.ANSWER_MAX_TOKENS,
    )
    data = llm.parse_json(text)
    if not data or not isinstance(data.get("response_from_it_ua"), str):
        return text.strip(), [question]
    queries = [q for q in data.get("info_for_search") or [] if isinstance(q, str) and q.strip()]
    return json.dumps(data, ensure_ascii=False), queries or [question]


def _compose(question: str, experience: str, official: str) -> str:
    """Шаг 5: одно живое сообщение из опыта чата и официальных данных.
    JSON не разобрался — берём текст как есть."""
    text = llm.complete(
        f"User question: \n{question}\nChat experience: \n{experience}\n"
        f"Official information: \n{official}",
        system=COMPOSE_SYS,
        max_tokens=config.ANSWER_MAX_TOKENS,
    )
    message = (llm.parse_json(text) or {}).get("message")
    return message.strip() if isinstance(message, str) and message.strip() else text.strip()


def ask(question: str, user_id: int | None = None,
        groups: list[str] | None = None) -> Answer:
    """Ответ целиком — для /ask."""
    *_, result = ask_steps(question, user_id=user_id, groups=groups)
    return result


def ask_steps(question: str, user_id: int | None = None,
              groups: list[str] | None = None):
    """Те же шаги по одному. Перед выжимкой, интернетом и сборкой отдаёт имя шага
    ("threads", "web", "compose") — бот показывает его пользователю, как n8n.
    Последним отдаёт Answer. Спека — decisions/2026-09-26-bot-progress-messages.md."""
    t0 = time.time()
    marks = [t0]

    key_phrase = _rewrite(question) if config.USE_QUERY_REWRITE else None
    marks.append(time.time())

    found = retrieval.search(key_phrase or question, groups=groups)
    marks.append(time.time())

    yield "threads"
    if found.threads:
        experience, queries = _extract(question, found.threads)
    else:
        experience, queries = NOT_DISCUSSED, [question]
    marks.append(time.time())

    yield "web"
    # в Perplexity уходят только поисковые запросы — ни тредов, ни переписки
    official = perplexity.search(
        WEB_PROMPT.replace("{queries}", json.dumps(queries, ensure_ascii=False)))
    marks.append(time.time())

    yield "compose"
    if not found.threads and not official:
        text = NOT_FOUND
    else:
        text = _compose(question, experience, official or WEB_NOT_FOUND)
    marks.append(time.time())

    steps = [round(b - a, 1) for a, b in zip(marks, marks[1:])]
    logger.info("answer steps rewrite/search/extract/web/compose: %s s, web %s",
                steps, "ok" if official else "none")

    result = Answer(
        answer=text,
        sources=[t.as_source() for t in found.threads],
        facts_used=found.facts,
        key_phrase=key_phrase,
        latency_ms=int((time.time() - t0) * 1000),
        thread_ids=[t.id for t in found.threads],
    )
    _log(question, result, user_id)
    yield result


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
