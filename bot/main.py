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

GREETING = (
    "Привет! Я отвечаю на вопросы о жизни в Валенсии по обсуждениям в местных чатах: "
    "школы, документы, врачи, аренда, быт.\n\n"
    "Просто напиши вопрос своими словами."
)
BUSY = "Сервер сейчас занят, попробуй ещё раз через пару минут."
# «processing» из наборов RU и UKR в n8n, дословно
PROCESSING = {
    "ru": "🤖 Ваш запрос получен. Запускаю интеллектуальный поиск и проверку данных — "
          "ответ будет готов примерно через 3 минуты. Благодарю за доверие.",
    "uk": "🤖 Привіт! Дякуємо, що ти з нами. Ми отримали твоє повідомлення, починаємо роботу "
          "над пошуком інформації, скоро повернемось із відповіддю. Час обробки твого "
          "запиту — 3 хвилини.",
}
# этапы на тех же местах, что в n8n; тексты — владельца (в n8n были английские)
STAGES = {
    "ru": {"threads": "Ищем в обсуждениях...",
           "web": "Проверяем в интернете и официальных источниках...",
           "compose": "Формируем ответ..."},
    "uk": {"threads": "Шукаємо в обговореннях...",
           "web": "Перевіряємо в інтернеті та офіційних джерелах...",
           "compose": "Формуємо відповідь..."},
}


async def ask_api(question: str, user_id: int | None, say) -> str:
    """Только текст ответа, как было в n8n. Ссылки на официальные сайты модель
    вставляет прямо в текст; треды из `sources` остаются для других клиентов.

    API отдаёт ответ строками по ходу работы. `say` получает событие:
    "received" — лимит пройден и работа пошла, дальше имена этапов.
    Без user_id у вопроса нет ни истории разговора, ни лимита."""
    body = {"question": question, "platform": "telegram"}
    if user_id is not None:
        body["user_id"] = str(user_id)
    async with httpx.AsyncClient(base_url=config.API_URL, timeout=300.0) as client:
        async with client.stream("POST", "/ask/stream", json=body) as r:
            if r.status_code == 429:
                await r.aread()
                return r.json().get("detail", "Слишком часто. Подожди немного.")
            r.raise_for_status()
            await say("received")
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if "stage" in event:
                    await say(event["stage"])
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
        await msg.answer(GREETING)

    @dp.message(F.text & ~F.text.startswith("/"))
    async def question(msg: Message):
        lang = "uk" if msg.from_user.language_code == "uk" else "ru"

        async def say(event: str):
            text = PROCESSING[lang] if event == "received" else STAGES[lang].get(event)
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
            answer = await ask_api(msg.text, user_id, say)
        except Exception as e:
            log.warning("ask failed: %s", e)
            await msg.answer(BUSY)
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
