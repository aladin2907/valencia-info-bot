"""Докачка новых сообщений из Telegram в архив (таблица messages).

Инкрементально: с последнего сохранённого message_id по каждой группе. Первый
запуск можно ограничить датой (--since), чтобы не тянуть всю историю разом.

Нужен пользовательский аккаунт (Telethon), а не бот: боты не читают историю
групп. api_id/api_hash берутся на my.telegram.org.
"""
import asyncio
import os
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl.types import Message

from app import config

SESSION = os.getenv("TG_SESSION_PATH", "sessions/valencia_ingest")
API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")

UPSERT = """
INSERT INTO messages (group_slug, message_id, sender_id, sender_name, text,
                      reply_to_message_id, sent_at, edited_at, tg_link)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (group_slug, message_id) DO UPDATE
   SET text = EXCLUDED.text,
       edited_at = EXCLUDED.edited_at,
       sender_name = COALESCE(EXCLUDED.sender_name, messages.sender_name)
"""


def _last_id(conn, group: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(message_id), 0) AS m FROM messages WHERE group_slug = %s",
                    (group,))
        return cur.fetchone()["m"]


async def _sender_name(client, msg: Message) -> str | None:
    try:
        sender = await msg.get_sender()
    except Exception:
        return None
    if sender is None:
        return None
    name = " ".join(x for x in [getattr(sender, "first_name", None),
                                getattr(sender, "last_name", None)] if x)
    return name or getattr(sender, "username", None) or getattr(sender, "title", None)


def _link_prefix(entity) -> str:
    """У публичной группы ссылка вида t.me/<username>/<id>, у приватной —
    t.me/c/<внутренний id>/<id>. Второй вариант открывается только у участников."""
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/"
    return f"https://t.me/c/{entity.id}/"


async def fetch_group(client, conn, group: str, since: datetime | None = None,
                      limit: int | None = None) -> dict:
    last = _last_id(conn, group)
    entity = await client.get_entity(group)
    prefix = _link_prefix(entity)
    saved = 0
    async for msg in client.iter_messages(entity, min_id=last, reverse=True, limit=limit):
        if not isinstance(msg, Message) or not (msg.message or "").strip():
            continue
        sent = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
        if since and sent < since:
            continue
        link = f"{prefix}{msg.id}"
        with conn.cursor() as cur:
            cur.execute(UPSERT, (
                group, msg.id, msg.sender_id, await _sender_name(client, msg),
                msg.message, msg.reply_to_msg_id, sent, msg.edit_date, link,
            ))
        saved += 1
        if saved % 500 == 0:
            conn.commit()
            print(f"  {group}: {saved}", flush=True)
    conn.commit()
    return {"group": group, "from_id": last, "saved": saved}


async def fetch_all(conn, groups: list[str] | None = None,
                    since: datetime | None = None, limit: int | None = None) -> list[dict]:
    if not API_ID or not API_HASH:
        raise RuntimeError("нет TG_API_ID / TG_API_HASH — докачка невозможна")
    groups = groups or config.GROUPS
    os.makedirs(os.path.dirname(SESSION) or ".", exist_ok=True)
    out = []
    async with TelegramClient(SESSION, int(API_ID), API_HASH) as client:
        for g in groups:
            try:
                out.append(await fetch_group(client, conn, g, since=since, limit=limit))
            except Exception as e:
                out.append({"group": g, "error": str(e)})
    return out


def run(conn, groups: list[str] | None = None, since: datetime | None = None,
        limit: int | None = None) -> list[dict]:
    return asyncio.run(fetch_all(conn, groups=groups, since=since, limit=limit))
