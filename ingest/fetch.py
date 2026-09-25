"""Докачка новых сообщений из Telegram в архив (таблица messages).

Инкрементально: с последнего сохранённого message_id по каждой группе. Первый
запуск можно ограничить датой (--since), чтобы не тянуть всю историю разом.

История назад (--backfill-days) идёт от самого старого сохранённого сообщения
в прошлое, поэтому прерванный прогон продолжается с того места, где встал.

Нужен пользовательский аккаунт (Telethon), а не бот: боты не читают историю
групп. api_id/api_hash берутся на my.telegram.org.
"""
import asyncio
import os
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Message

from app import config

SESSION = os.getenv("TG_SESSION_PATH", "sessions/valencia_ingest")
# В ECS диск контейнера read-only, а узел эфемерный: файл сессии там хранить
# негде. Поэтому сессию можно передать строкой (Secrets Manager -> env).
# Строка делается из файла: StringSession.save(SQLiteSession(path)).
SESSION_STRING = os.getenv("TG_SESSION_STRING", "").strip()
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
    """Докуда уже дошли. Если сырой архив пуст, а треды залиты из файлов —
    берём последний корень треда: иначе докачка начнёт историю группы заново."""
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(message_id), 0) AS m FROM messages WHERE group_slug = %s",
                    (group,))
        last = cur.fetchone()["m"]
        if last:
            return last
        cur.execute("SELECT coalesce(max(root_message_id), 0) AS m FROM threads WHERE group_slug = %s",
                    (group,))
        return cur.fetchone()["m"]


def _first_id(conn, group: str) -> int | None:
    """Самое старое сохранённое сообщение — отметка для докачки назад.
    Ночная докачка вперёд смотрит на максимум, поэтому её эта отметка не сдвигает."""
    with conn.cursor() as cur:
        cur.execute("SELECT min(message_id) AS m FROM messages WHERE group_slug = %s", (group,))
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
    entity = await client.get_entity(config.TG_CHATS.get(group, group))
    prefix = _link_prefix(entity)
    saved = 0
    # Точку старта должен выбирать Telegram, а не мы. Без offset_date первый
    # запуск идёт от самого первого сообщения группы и выбрасывает старое уже
    # у себя — то есть выкачивает всю историю впустую и рискует нарваться на
    # ограничение частоты. При наличии отметки командует min_id: у Telethon в
    # обратном порядке offset_id важнее даты.
    window = {"reverse": True, "limit": limit}
    if last:
        window["min_id"] = last
    elif since:
        window["offset_date"] = since
    async for msg in client.iter_messages(entity, **window):
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


async def backfill_group(client, conn, group: str, until: datetime,
                         limit: int | None = None) -> dict:
    """История назад: от самого старого сохранённого сообщения к более старым,
    до даты until или начала группы. Если сообщений группы в базе нет — от
    последнего сообщения группы."""
    first = _first_id(conn, group)
    entity = await client.get_entity(config.TG_CHATS.get(group, group))
    prefix = _link_prefix(entity)
    saved = 0
    oldest = None
    # Без reverse Telethon идёт от новых к старым, offset_id — исключительно:
    # сообщения строго старше отметки.
    window = {"limit": limit}
    if first:
        window["offset_id"] = first
    async for msg in client.iter_messages(entity, **window):
        sent = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
        if sent < until:
            break
        if not isinstance(msg, Message) or not (msg.message or "").strip():
            continue
        with conn.cursor() as cur:
            cur.execute(UPSERT, (
                group, msg.id, msg.sender_id, await _sender_name(client, msg),
                msg.message, msg.reply_to_msg_id, sent, msg.edit_date, f"{prefix}{msg.id}",
            ))
        saved += 1
        oldest = sent
        if saved % 500 == 0:
            conn.commit()
            print(f"  {group}: назад {saved}, дошли до {sent:%Y-%m-%d}", flush=True)
    conn.commit()
    return {"group": group, "backfill_from_id": first, "saved": saved,
            "oldest": oldest.isoformat() if oldest else None}


async def fetch_all(conn, groups: list[str] | None = None,
                    since: datetime | None = None, limit: int | None = None,
                    backfill_until: datetime | None = None) -> list[dict]:
    if not API_ID or not API_HASH:
        raise RuntimeError("нет TG_API_ID / TG_API_HASH — докачка невозможна")
    groups = groups or config.GROUPS
    if SESSION_STRING:
        session = StringSession(SESSION_STRING)
    else:
        os.makedirs(os.path.dirname(SESSION) or ".", exist_ok=True)
        session = SESSION
    out = []
    async with TelegramClient(session, int(API_ID), API_HASH) as client:
        for g in groups:
            try:
                out.append(await fetch_group(client, conn, g, since=since, limit=limit))
            except Exception as e:
                out.append({"group": g, "error": str(e)})
            if backfill_until:
                try:
                    out.append(await backfill_group(client, conn, g, backfill_until, limit=limit))
                except Exception as e:
                    out.append({"group": g, "backfill": True, "error": str(e)})
    return out


def run(conn, groups: list[str] | None = None, since: datetime | None = None,
        limit: int | None = None, backfill_until: datetime | None = None) -> list[dict]:
    return asyncio.run(fetch_all(conn, groups=groups, since=since, limit=limit,
                                 backfill_until=backfill_until))
