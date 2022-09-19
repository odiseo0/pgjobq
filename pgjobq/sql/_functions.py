from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, List, Mapping, Optional, TypedDict, Union
from uuid import UUID

import asyncpg  # type: ignore

PoolOrConnection = Union[asyncpg.Pool, asyncpg.Connection]
Record = Mapping[str, Any]


PUBLISH_MESSAGES = """\
WITH queue_info AS (
    SELECT
        id AS queue_id,
        retention_period,
        max_delivery_attempts
    FROM pgjobq.queues
    WHERE name = $1
), published_notification AS (
    SELECT pg_notify('pgjobq.new_job', $1)
)
INSERT INTO pgjobq.messages(
    queue_id,
    id,
    expires_at,
    delivery_attempts_remaining,
    available_at,
    body
)
SELECT
    queue_id,
    $2,
    now() + retention_period,
    max_delivery_attempts,
    -- set next ack to now
    -- somewhat meaningless but avoids nulls
    now() + COALESCE($4, '0 seconds'::interval),
    $3
FROM queue_info
LEFT JOIN published_notification ON 1 = 1
RETURNING 1;  -- NULL if the queue doesn't exist
"""


async def publish_messages(
    conn: PoolOrConnection,
    *,
    queue_name: str,
    message_id: UUID,
    message_body: bytes,
    delay: Optional[timedelta],
) -> None:
    res: Optional[int] = await conn.fetchval(  # type: ignore
        PUBLISH_MESSAGES,
        queue_name,
        message_id,
        message_body,
        delay,
    )
    if res is None:
        raise LookupError(f"Queue not found: there is no queue named {queue_name}")


_POLL_FOR_MESSAGES = """\
WITH queue_info AS (
    SELECT
        id,
        ack_deadline
    FROM pgjobq.queues
    WHERE name = $1
), selected_messages AS (
    SELECT
        id
    FROM pgjobq.messages
    WHERE (
        delivery_attempts_remaining != 0
        AND
        expires_at > now()
        AND
        available_at < now()
        AND
        queue_id = (SELECT id FROM queue_info)
    )
    {order_by}
    FOR UPDATE SKIP LOCKED
    LIMIT $2
)
UPDATE pgjobq.messages
SET
    available_at = now() + (SELECT ack_deadline FROM queue_info),
    delivery_attempts_remaining = delivery_attempts_remaining - 1
FROM selected_messages
WHERE pgjobq.messages.id = selected_messages.id
RETURNING pgjobq.messages.id AS id, available_at AS next_ack_deadline, body
"""

POLL_FOR_MESSAGES = _POLL_FOR_MESSAGES.format(order_by="")
POLL_FOR_MESSAGES_FIFO = _POLL_FOR_MESSAGES.format(order_by="ORDER BY id")


class Message(TypedDict):
    id: UUID
    body: bytes
    next_ack_deadline: datetime


async def poll_for_messages(
    conn: PoolOrConnection,
    *,
    queue_name: str,
    batch_size: int,
    fifo: bool,
) -> List[Message]:
    query = POLL_FOR_MESSAGES_FIFO if fifo else POLL_FOR_MESSAGES
    return await conn.fetch(  # type: ignore
        query,
        queue_name,
        batch_size,
    )


ACK_MESSAGE = """\
WITH msg AS (
    SELECT pg_notify('pgjobq.job_completed', $1 || ',' || CAST($2::uuid AS text))
)
DELETE FROM pgjobq.messages
WHERE queue_id = (SELECT id FROM pgjobq.queues WHERE name = $1) AND id = $2::uuid AND 1 = (SELECT 1 FROM msg);
"""


async def ack_message(
    conn: PoolOrConnection,
    queue_name: str,
    job_id: UUID,
) -> None:
    await conn.execute(ACK_MESSAGE, queue_name, job_id)  # type: ignore


NACK_MESSAGE = """\
WITH msg AS (
    SELECT pg_notify('pgjobq.new_job', $1)
)
UPDATE pgjobq.messages
SET available_at = now()
WHERE queue_id = (SELECT id FROM pgjobq.queues WHERE name = $1) AND id = $2 AND 1 = (SELECT 1 FROM msg);
"""


async def nack_message(
    conn: PoolOrConnection,
    queue_name: str,
    job_id: UUID,
) -> None:
    await conn.execute(NACK_MESSAGE, queue_name, job_id)  # type: ignore


EXTEND_ACK_DEADLINE = """\
WITH message_for_update AS (
    SELECT
        id,
        queue_id
    FROM pgjobq.messages
    WHERE queue_id = (SELECT id FROM pgjobq.queues WHERE name = $1) AND id = $2
    FOR UPDATE SKIP LOCKED
)
UPDATE pgjobq.messages
SET available_at = (
    now() + (
        SELECT ack_deadline
        FROM pgjobq.queues
        WHERE pgjobq.queues.id = (
            SELECT queue_id FROM message_for_update
        )
    )
)
WHERE pgjobq.messages.id = (
    SELECT id FROM message_for_update
)
RETURNING available_at AS next_ack_deadline;
"""


async def extend_ack_deadline(
    conn: PoolOrConnection,
    queue_name: str,
    job_id: UUID,
) -> datetime:
    return await conn.fetchval(EXTEND_ACK_DEADLINE, queue_name, job_id)  # type: ignore
