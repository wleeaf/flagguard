import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import (
    MESSAGE_LOG_ASYNC,
    MESSAGE_LOG_BATCH_SIZE,
    MESSAGE_LOG_FLUSH_INTERVAL_SECONDS,
    MESSAGE_LOG_QUEUE_SIZE,
)
from services import bot_state as _bot_state, is_admin as _is_admin, moderation_repo as _moderation_repo
from database import get_db
from metrics import inc_counter, observe_histogram, set_gauge
from models.user import _normalize_username
from time_utils import db_now

from time_utils import parse_db_timestamp, now_local

MAINTENANCE_REPLY = "⚠️ <b>BAKIM MODU</b>\n\nSistem bakımda. Lütfen daha sonra tekrar deneyin."


def _format_remaining(until_str: str) -> str:
    """Compute human-readable remaining time in Turkish."""
    until_dt = parse_db_timestamp(until_str)
    if not until_dt:
        return "biraz"
    delta = until_dt - now_local()
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 60:
        return f"{total_seconds} saniye"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    parts = []
    if hours:
        parts.append(f"{hours} saat")
    if minutes:
        parts.append(f"{minutes} dakika")
    return " ".join(parts) if parts else f"{total_seconds} saniye"

_message_log_queue: asyncio.Queue[tuple[str, str, str, str, str] | None] | None = None
_message_log_task: asyncio.Task | None = None
_message_log_stop_event: asyncio.Event | None = None
_message_log_loop: asyncio.AbstractEventLoop | None = None


def _upsert_known_users_from_message_rows(
    conn,
    rows: list[tuple[str, str, str, str, str]],
) -> None:
    """Keep known_users updated without adding extra DB work to the async hot path."""
    if not rows:
        return

    # Track min/max timestamps per user in this batch and prefer the most recent
    # *non-empty* username so we can recover from occasional missing fields.
    by_uid: dict[str, dict[str, str]] = {}
    for uid, first_name, username, _text, ts in rows:
        uid = str(uid)
        first_name = (first_name or "").strip()
        username = _normalize_username(username)

        existing = by_uid.get(uid)
        if existing is None:
            by_uid[uid] = {
                "first_name": first_name,
                "first_name_ts": ts if first_name else "",
                "username": username,
                "username_ts": ts if username else "",
                "first_ts": ts,
                "last_ts": ts,
            }
            continue

        if ts < existing["first_ts"]:
            existing["first_ts"] = ts
        if ts > existing["last_ts"]:
            existing["last_ts"] = ts

        if first_name and (not existing["first_name_ts"] or ts >= existing["first_name_ts"]):
            existing["first_name"] = first_name
            existing["first_name_ts"] = ts

        if username and (not existing["username_ts"] or ts >= existing["username_ts"]):
            existing["username"] = username
            existing["username_ts"] = ts

    params: list[tuple[str, str, str, str, str]] = []
    for uid, d in by_uid.items():
        params.append((uid, d["first_name"], d["username"], d["first_ts"], d["last_ts"]))

    conn.executemany(
        """INSERT INTO known_users (user_id, first_name, username, first_seen, last_seen)
           VALUES (?, ?, NULLIF(?, ''), ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               first_name = COALESCE(NULLIF(excluded.first_name, ''), known_users.first_name),
               username = COALESCE(NULLIF(excluded.username, ''), known_users.username),
               last_seen = CASE
                   WHEN excluded.last_seen > COALESCE(known_users.last_seen, '')
                       THEN excluded.last_seen
                   ELSE known_users.last_seen
               END""",
        params,
    )


def _write_message_log(
    uid: str,
    first_name: str,
    username: str,
    text: str,
):
    ts = db_now()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO message_log
               (user_id, first_name, username, message_text, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (uid, first_name, username, text, ts),
        )
        _upsert_known_users_from_message_rows(
            conn,
            [(uid, first_name, username, text, ts)],
        )


def _write_message_log_batch(rows: list[tuple[str, str, str, str, str]]) -> None:
    if not rows:
        return
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO message_log
               (user_id, first_name, username, message_text, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )
        _upsert_known_users_from_message_rows(conn, rows)


def _get_message_log_queue() -> asyncio.Queue[tuple[str, str, str, str, str] | None]:
    global _message_log_queue
    if _message_log_queue is None:
        _message_log_queue = asyncio.Queue(maxsize=MESSAGE_LOG_QUEUE_SIZE)
        set_gauge("message_log_queue_depth", 0.0)
    return _message_log_queue


def _enqueue_message_log(uid: str, first_name: str, username: str, text: str) -> None:
    if not MESSAGE_LOG_ASYNC:
        return
    row = (uid, first_name, username, text, db_now())
    q = _get_message_log_queue()
    try:
        q.put_nowait(row)
        inc_counter("message_log_enqueued_total")
        set_gauge("message_log_queue_depth", float(q.qsize()))
    except asyncio.QueueFull:
        inc_counter("message_log_dropped_total")


async def _message_log_worker() -> None:
    q = _get_message_log_queue()
    stop_event = _message_log_stop_event
    if stop_event is None:
        return

    batch_size = max(1, int(MESSAGE_LOG_BATCH_SIZE))
    flush_interval = max(0.01, float(MESSAGE_LOG_FLUSH_INTERVAL_SECONDS))

    while True:
        if stop_event.is_set() and q.empty():
            return

        try:
            item = await asyncio.wait_for(q.get(), timeout=flush_interval)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            continue

        if item is None:
            q.task_done()
            stop_event.set()
            continue

        batch: list[tuple[str, str, str, str, str]] = [item]
        while len(batch) < batch_size:
            try:
                nxt = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if nxt is None:
                q.task_done()
                stop_event.set()
                continue
            batch.append(nxt)

        started = time.monotonic()
        try:
            await asyncio.to_thread(_write_message_log_batch, batch)
            inc_counter("message_log_written_total", float(len(batch)))
        except Exception:
            inc_counter("message_log_write_failures_total")
            logging.exception("Message log batch write failed (size=%d)", len(batch))
        finally:
            observe_histogram("message_log_flush_seconds", time.monotonic() - started)
            for _ in range(len(batch)):
                q.task_done()
            set_gauge("message_log_queue_depth", float(q.qsize()))


async def start_message_log_worker() -> None:
    """Start the background message-log writer (idempotent)."""
    if not MESSAGE_LOG_ASYNC:
        return

    global _message_log_task, _message_log_stop_event, _message_log_loop
    loop = asyncio.get_running_loop()
    if _message_log_task and not _message_log_task.done() and _message_log_loop is loop:
        return

    _message_log_loop = loop
    _message_log_stop_event = asyncio.Event()
    _message_log_task = asyncio.create_task(_message_log_worker())


async def stop_message_log_worker() -> None:
    """Flush and stop the background message-log writer."""
    global _message_log_task, _message_log_stop_event, _message_log_loop
    if not MESSAGE_LOG_ASYNC:
        return
    if not _message_log_task:
        return

    q = _get_message_log_queue()
    if _message_log_stop_event:
        _message_log_stop_event.set()
    try:
        q.put_nowait(None)
    except asyncio.QueueFull:
        pass

    try:
        await asyncio.wait_for(q.join(), timeout=10.0)
    except asyncio.TimeoutError:
        logging.warning(
            "Message log flush timed out; dropping remaining queued writes (depth=%d)",
            q.qsize(),
        )

    try:
        await asyncio.wait_for(_message_log_task, timeout=5.0)
    except asyncio.TimeoutError:
        _message_log_task.cancel()
        try:
            await asyncio.wait_for(_message_log_task, timeout=1.0)
        except Exception:
            pass
    _message_log_task = None
    _message_log_stop_event = None
    _message_log_loop = None


class MessageLogMiddleware(BaseMiddleware):
    """Logs every incoming user message to message_log table."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user:
            try:
                uid = str(event.from_user.id)
                first_name = event.from_user.first_name or ""
                username = event.from_user.username or ""
                text = event.text or event.caption or ""
                if MESSAGE_LOG_ASYNC:
                    _enqueue_message_log(uid, first_name, username, text)
                else:
                    await asyncio.to_thread(_write_message_log, uid, first_name, username, text)
            except Exception as e:
                logging.error("Message log middleware error: %s", e)
        return await handler(event, data)


class ModerationMiddleware(BaseMiddleware):
    """Blocks banned and timed-out non-admin users."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        user_id = event.from_user.id if event.from_user else 0
        if _is_admin(user_id):
            return await handler(event, data)

        uid = str(user_id)
        if _moderation_repo.is_banned(uid):
            await event.reply("Bu botu kullanmaktan banlandınız.")
            return None

        timeout = _moderation_repo.get_active_timeout(uid)
        if timeout:
            remaining = _format_remaining(timeout["timeout_until"])
            await event.reply(
                f"Çok hızlı olduğun için bir süreliğine dinlen. "
                f"{remaining} içinde yeniden dene."
            )
            return None

        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    """Blocks all non-admin users when maintenance mode is active."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        user_id = event.from_user.id if event.from_user else 0

        if _is_admin(user_id):
            return await handler(event, data)

        # Fast path: the property reads from an in-memory TTL cache, so it's
        # safe (and much cheaper) to call from the event loop without to_thread.
        maintenance_mode = _bot_state.maintenance_mode
        if not maintenance_mode:
            return await handler(event, data)

        await event.reply(MAINTENANCE_REPLY, parse_mode="HTML")
        return None
