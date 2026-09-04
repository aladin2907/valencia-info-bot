#!/usr/bin/env python3
"""Telegram-бот — первый клиент API. Всю логику делает API, бот только носит текст.

На боевом токене сейчас висит старый n8n-workflow. Бот проверяет это при старте
и не запускается, пока webhook чужой, — чтобы не сломать работающего бота.

    TELEGRAM_BOT_TOKEN=... API_URL=http://localhost:8080 python -m bot.main
"""
import asyncio
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


async def ask_api(question: str, user_id: int) -> tuple[str, list[dict]]:
    async with httpx.AsyncClient(base_url=config.API_URL, timeout=300.0) as client:
        r = await client.post("/ask", json={"question": question,
                                            "user_id": str(user_id),
                                            "platform": "telegram"})
        if r.status_code == 429:
            return r.json().get("detail", "Слишком часто. Подожди немного."), []
        r.raise_for_status()
        data = r.json()
        return data["answer"], data.get("sources", [])


def format_sources(sources: list[dict]) -> str:
    """Ссылки на обсуждения — чтобы можно было проверить ответ первоисточником."""
    links = [s["link"] for s in sources[:3] if s.get("link")]
    return "\n\n📎 " + " · ".join(f"[обсуждение {i + 1}]({u})" for i, u in enumerate(links)) \
        if links else ""


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
        await bot.send_chat_action(msg.chat.id, "typing")
        try:
            answer, sources = await ask_api(msg.text, msg.from_user.id)
        except Exception as e:
            log.warning("ask failed: %s", e)
            await msg.answer(BUSY)
            return
        await send_answer(msg, answer + format_sources(sources))

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
