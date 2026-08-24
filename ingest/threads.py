"""Пересборка тредов из архива сообщений.

Тред — цепочка «вопрос + все ответы на него», это единица поиска. Собирается
не за вчера, а за окно (по умолчанию 14 дней): тред живёт несколько дней, и на
вопрос недельной давности вполне могут ответить сегодня.

Текст треда — «Автор: сообщение», склеенные по времени. Формат тот же, что у
корпуса, на котором проводился замер: менять его — значит обесценивать замер.
"""
import hashlib
from datetime import datetime, timedelta, timezone

from app import config

MIN_MESSAGES = 2
MIN_CHARS = 50

# Корни тредов, задетые свежими сообщениями. Идём вверх по цепочке ответов:
# новое сообщение может быть ответом в тред, начатый месяц назад.
SQL_ROOTS = """
WITH RECURSIVE touched AS (
    SELECT group_slug, message_id, reply_to_message_id
      FROM messages
     WHERE group_slug = %s AND sent_at >= %s
    UNION
    SELECT m.group_slug, m.message_id, m.reply_to_message_id
      FROM messages m
      JOIN touched t ON m.group_slug = t.group_slug
                    AND m.message_id = t.reply_to_message_id
)
SELECT DISTINCT t.message_id
  FROM touched t
  LEFT JOIN messages p ON p.group_slug = %s AND p.message_id = t.reply_to_message_id
 WHERE t.reply_to_message_id IS NULL OR p.message_id IS NULL
"""

# Все сообщения тредов с этими корнями — вниз по цепочке ответов.
SQL_THREAD_MESSAGES = """
WITH RECURSIVE down AS (
    SELECT group_slug, message_id, message_id AS root_id, sender_name, text, sent_at, tg_link
      FROM messages
     WHERE group_slug = %s AND message_id = ANY(%s)
    UNION ALL
    SELECT m.group_slug, m.message_id, d.root_id, m.sender_name, m.text, m.sent_at, m.tg_link
      FROM messages m
      JOIN down d ON m.group_slug = d.group_slug
                 AND m.reply_to_message_id = d.message_id
)
SELECT * FROM down ORDER BY root_id, sent_at
"""

UPSERT = """
INSERT INTO threads (group_slug, root_message_id, content, message_count,
                     started_at, last_activity_at, content_hash, tg_link, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (group_slug, root_message_id) DO UPDATE
   SET content          = EXCLUDED.content,
       message_count    = EXCLUDED.message_count,
       last_activity_at = EXCLUDED.last_activity_at,
       content_hash     = EXCLUDED.content_hash,
       tg_link          = COALESCE(EXCLUDED.tg_link, threads.tg_link),
       updated_at       = now()
 WHERE threads.content_hash IS DISTINCT FROM EXCLUDED.content_hash
RETURNING id, content_hash
"""


def build_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if text:
            parts.append(f"{m.get('sender_name') or 'Unknown'}: {text}")
    return " ".join(parts)


def rebuild(conn, group: str, since: datetime | None = None, window_days: int | None = None) -> dict:
    """Пересобирает треды группы, задетые сообщениями за окно. Идемпотентно:
    если текст не изменился, строка не трогается и вектор не пересчитывается."""
    window_days = window_days or config.THREAD_REBUILD_DAYS
    since = since or datetime.now(timezone.utc) - timedelta(days=window_days)

    with conn.cursor() as cur:
        cur.execute(SQL_ROOTS, (group, since, group))
        roots = [r["message_id"] for r in cur.fetchall()]
        if not roots:
            return {"group": group, "roots": 0, "written": 0, "skipped": 0}

        cur.execute(SQL_THREAD_MESSAGES, (group, roots))
        rows = cur.fetchall()

    grouped: dict[int, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["root_id"], []).append(r)

    written = skipped = 0
    with conn.cursor() as cur:
        for root_id, msgs in grouped.items():
            if len(msgs) < MIN_MESSAGES:
                skipped += 1
                continue
            text = build_text(msgs)
            if len(text) < MIN_CHARS:
                skipped += 1
                continue
            dates = [m["sent_at"] for m in msgs]
            cur.execute(UPSERT, (
                group, root_id, text, len(msgs), min(dates), max(dates),
                hashlib.sha256(text.encode()).hexdigest(),
                next((m["tg_link"] for m in msgs if m["message_id"] == root_id), None),
            ))
            if cur.fetchone():
                written += 1
            else:
                skipped += 1
    conn.commit()
    return {"group": group, "roots": len(grouped), "written": written, "skipped": skipped}
