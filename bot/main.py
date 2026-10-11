#!/usr/bin/env python3
"""Telegram-бот — первый клиент API. Всю логику делает API, бот только носит текст.

На боевом токене сейчас висит старый n8n-workflow. Бот проверяет это при старте
и не запускается, пока webhook чужой, — чтобы не сломать работающего бота.

    TELEGRAM_BOT_TOKEN=... API_URL=http://localhost:8080 python -m bot.main
"""
import asyncio
import json
import logging

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message

from app import config

log = logging.getLogger("bot")

GREETING = {
    "ru": "Привет! Я отвечаю на вопросы о жизни в Валенсии по обсуждениям в местных чатах: "
          "школы, документы, врачи, аренда, быт.\n\n"
          "Просто напиши вопрос своими словами.",
    "uk": "Привіт! Я відповідаю на питання про життя у Валенсії за обговореннями в місцевих "
          "чатах: школи, документи, лікарі, оренда, побут.\n\n"
          "Просто напиши питання своїми словами.",
    "en": "Hi! I answer questions about life in Valencia based on discussions in local chats: "
          "schools, paperwork, doctors, renting, daily life.\n\n"
          "Just write your question in your own words.",
}
BUSY = {
    "ru": "Сервер сейчас занят, попробуй ещё раз через пару минут.",
    "uk": "Сервер зараз зайнятий, спробуй ще раз за кілька хвилин.",
    "en": "The server is busy right now, please try again in a couple of minutes.",
}
# «processing» из наборов RU и UKR в n8n, дословно; EN — в том же тоне
PROCESSING = {
    "ru": "🤖 Ваш запрос получен. Запускаю интеллектуальный поиск и проверку данных — "
          "ответ будет готов примерно через 3 минуты. Благодарю за доверие.",
    "uk": "🤖 Привіт! Дякуємо, що ти з нами. Ми отримали твоє повідомлення, починаємо роботу "
          "над пошуком інформації, скоро повернемось із відповіддю. Час обробки твого "
          "запиту — 3 хвилини.",
    "en": "🤖 Your request has been received. I'm starting an intelligent search and verifying "
          "the data — the answer will be ready in about 3 minutes. Thank you for your trust.",
}
# этапы на тех же местах, что в n8n; тексты — владельца (в n8n были английские)
STAGES = {
    "ru": {"threads": "Ищем в обсуждениях...",
           "web": "Проверяем в интернете и официальных источниках...",
           "compose": "Формируем ответ..."},
    "uk": {"threads": "Шукаємо в обговореннях...",
           "web": "Перевіряємо в інтернеті та офіційних джерелах...",
           "compose": "Формуємо відповідь..."},
    "en": {"threads": "Searching the discussions...",
           "web": "Checking the internet and official sources...",
           "compose": "Composing the answer..."},
}
# Rate-limit notice, built by the bot in the user's language. {m} = minutes.
# The API returns retry_after_seconds; it no longer dictates the wording.
RATE_LIMIT = {
    "ru": "Следующий вопрос можно задать через {m} мин.",
    "uk": "Наступне питання можна поставити через {m} хв.",
    "en": "You can ask the next question in {m} min.",
}


def rate_limit_text(lang: str, retry_after_seconds: int) -> str:
    minutes = max(1, -(-int(retry_after_seconds) // 60))  # ceil, at least 1
    return RATE_LIMIT[lang].format(m=minutes)


SUPPORTED_LANGS = ("ru", "uk", "en")
DEFAULT_LANG = "en"  # anything that isn't ru/uk falls back to English


# Ukrainian-specific Cyrillic letters: їієґ (and uppercase). Their presence
# distinguishes Ukrainian from Russian without a language-detection dependency.
_UK_LETTERS = set("їієґЇІЄҐ")


def _lang_from_code(language_code: str | None) -> str:
    """Fallback: map Telegram's language_code to a supported language.
    ru -> ru, uk -> uk, en -> en; everything else -> English."""
    code = (language_code or "").split("-")[0].lower()
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG


def detect_lang(text: str | None, language_code: str | None = None) -> str:
    """Pick the service-message language from the MESSAGE TEXT so it matches the
    language the answer comes back in. Cyrillic with Ukrainian-only letters ->
    uk, other Cyrillic -> ru, Latin/other -> en. When the text carries no
    letters (emoji, digits), fall back to Telegram's language_code."""
    text = text or ""
    has_cyrillic = any("\u0400" <= ch <= "\u04ff" for ch in text)
    if has_cyrillic:
        return "uk" if any(ch in _UK_LETTERS for ch in text) else "ru"
    if any(ch.isalpha() for ch in text):
        return "en"
    return _lang_from_code(language_code)


def stage_text(lang: str, event: dict) -> str | None:
    """Текст служебного сообщения. В первом этапе — сколько обсуждений в базе,
    со склонением: «по 54 901 обсуждению», «по 54 902 обсуждениям»."""
    stage = event.get("stage")
    if stage == "received":
        return PROCESSING[lang]
    total = event.get("total")
    if stage == "threads" and isinstance(total, int) and total > 0:
        n = f"{total:,}".replace(",", "\u00a0")
        one = total % 10 == 1 and total % 100 != 11
        if lang == "uk":
            return f"Шукаємо серед {n} {'обговорення' if one else 'обговорень'} в групах Telegram..."
        if lang == "en":
            return f"Searching {n} {'discussion' if total == 1 else 'discussions'} in Telegram groups..."
        return f"Ищем по {n} {'обсуждению' if one else 'обсуждениям'} в группах Telegram..."
    return STAGES[lang].get(stage)


async def ask_api(question: str, user_id: int | None, say, lang: str = DEFAULT_LANG) -> str:
    """Только текст ответа, как было в n8n. Ссылки на официальные сайты модель
    вставляет прямо в текст; треды из `sources` остаются для других клиентов.

    API отдаёт ответ строками по ходу работы. `say` получает событие:
    {"stage": "received"} — лимит пройден и работа пошла, дальше этапы API.
    Без user_id у вопроса нет ни истории разговора, ни лимита."""
    body = {"question": question, "platform": "telegram"}
    if user_id is not None:
        body["user_id"] = str(user_id)
    async with httpx.AsyncClient(base_url=config.API_URL, timeout=300.0) as client:
        async with client.stream("POST", "/ask/stream", json=body) as r:
            if r.status_code == 429:
                await r.aread()
                detail = r.json().get("detail")
                # New contract: detail is {error, retry_after_seconds, message}.
                # Build the text ourselves in the user's language; fall back to
                # the API's message, then a generic line, for older responses.
                if isinstance(detail, dict) and "retry_after_seconds" in detail:
                    return rate_limit_text(lang, detail["retry_after_seconds"])
                if isinstance(detail, dict):
                    return detail.get("message") or RATE_LIMIT[lang].format(m=1)
                return detail or RATE_LIMIT[lang].format(m=1)
            r.raise_for_status()
            await say({"stage": "received"})
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if "stage" in event:
                    await say(event)
                elif "answer" in event:
                    return event["answer"]
                else:
                    raise RuntimeError(event.get("error") or line)
    raise RuntimeError("API оборвал ответ на полпути")


async def send_answer(msg: Message, text: str) -> None:
    """В ответах живой текст из чата: подчёркивания в ссылках, звёздочки, скобки.
    Telegram на таком спотыкается — если разметка не разобралась, шлём как есть,
    чем терять готовый ответ."""
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]:
        try:
            await msg.answer(chunk, parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramBadRequest:
            await msg.answer(chunk, disable_web_page_preview=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан")

    bot = Bot(config.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(msg: Message):
        # /start has no question text to detect from -> use Telegram's locale.
        await msg.answer(GREETING[_lang_from_code(msg.from_user.language_code)])

    @dp.message(F.text & ~F.text.startswith("/"))
    async def question(msg: Message):
        # Detect from the question text so the service messages match the
        # language the answer will come back in; language_code is the fallback.
        lang = detect_lang(msg.text, msg.from_user.language_code)

        async def say(event: dict):
            text = stage_text(lang, event)
            if not text:
                return  # незнакомый этап пропускаем
            try:
                await msg.answer(text)
            except Exception as e:  # из-за сообщения об этапе ответ не теряем
                log.warning("stage message failed: %s", e)

        # От имени группы или канала (анонимный админ, пост от канала) Telegram
        # подставляет одного общего «пользователя» на всех таких людей во всех
        # группах — у такого сообщения своей истории нет, иначе разговоры смешаются.
        user_id = None if msg.sender_chat else msg.from_user.id

        await bot.send_chat_action(msg.chat.id, "typing")
        try:
            answer = await ask_api(msg.text, user_id, say, lang)
        except Exception as e:
            log.warning("ask failed: %s", e)
            await msg.answer(BUSY[lang])
            return
        await send_answer(msg, answer)

    hook = await bot.get_webhook_info()
    if hook.url:
        if not config.ALLOW_WEBHOOK_TAKEOVER:
            await bot.session.close()
            raise SystemExit(
                f"На этом токене уже висит webhook: {hook.url}\n"
                "Значит, бот сейчас работает через n8n. Не запускаюсь, чтобы его не сломать.\n"
                "Осознанное переключение: выключить workflow в n8n, затем "
                "ALLOW_WEBHOOK_TAKEOVER=1."
            )
        log.warning("снимаю чужой webhook %s — переключение разрешено флагом", hook.url)
        await bot.delete_webhook(drop_pending_updates=True)

    log.info("бот запущен, API: %s", config.API_URL)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
