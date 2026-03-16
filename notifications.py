"""Centralized Telegram notification and broadcast queue helpers."""

import asyncio
import html
import logging
import os
import re
import time
from datetime import timedelta
from uuid import uuid4

import httpx

from ai.difficulty import DIFFICULTY_SYMBOLS
from config import (
    ADMIN_IDS,
    TELEGRAM_TOKEN,
    TELEGRAM_SEND_CONCURRENCY,
    TELEGRAM_MIN_SEND_INTERVAL_SECONDS,
    TELEGRAM_SEND_MAX_RETRIES,
)
import database
from metrics import inc_counter, observe_histogram
from models.bot_state import BotStateRepository
from time_utils import db_now, now_local

_bot_state = BotStateRepository()


def log_bot_event(
    event_type: str,
    message: str,
    user_id: str | None = None,
    first_name: str | None = None,
    username: str | None = None,
) -> None:
    """Persist a bot event to the bot_events table."""
    with database.get_db() as conn:
        conn.execute(
            """INSERT INTO bot_events (event_type, user_id, first_name, username, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_type, user_id, first_name, username, message, db_now()),
        )


def _get_api_base() -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _get_all_admin_ids() -> list[int]:
    """Merge env ADMIN_IDS with DB telegram_admins (deduplicated)."""
    all_ids = set(ADMIN_IDS)
    try:
        from models.admin import AdminRepository
        db_ids = AdminRepository().get_all_admin_ids()
        all_ids.update(db_ids)
    except Exception:
        pass
    return list(all_ids)


def _get_notifiable_admin_ids() -> list[int]:
    """Merge env ADMIN_IDS with DB non-silent admins (deduplicated)."""
    all_ids = set(ADMIN_IDS)
    try:
        from models.admin import AdminRepository
        repo = AdminRepository()
        db_ids = repo.get_non_silent_admin_ids()
        all_ids.update(db_ids)
        # Remove env IDs that are muted in DB
        silent_ids = repo.get_silent_admin_ids()
        all_ids -= set(silent_ids)
    except Exception:
        pass
    return list(all_ids)

# Persistent module-level HTTP client (connection pooling, keep-alive)
_client: httpx.AsyncClient | None = None

# Shared outbound send controls (all Telegram sends flow through this gate)
_send_loop: asyncio.AbstractEventLoop | None = None
_send_semaphore: asyncio.Semaphore | None = None
_send_schedule_lock: asyncio.Lock | None = None
_next_send_at = 0.0

# Broadcast worker lifecycle
_worker_task: asyncio.Task | None = None
_worker_stop_event: asyncio.Event | None = None
_BROADCAST_SEND_CONCURRENCY = 20
_BROADCAST_DB_BATCH_SIZE = 100


def _get_db():
    return database.get_db()


def _get_known_user_ids() -> list[str]:
    with _get_db() as conn:
        rows = conn.execute("SELECT user_id FROM known_users").fetchall()
        return [str(r["user_id"]) for r in rows]


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        max_conn = max(50, TELEGRAM_SEND_CONCURRENCY * 2)
        keepalive = max(20, TELEGRAM_SEND_CONCURRENCY)
        _client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(
                max_connections=max_conn,
                max_keepalive_connections=keepalive,
            ),
        )
    return _client


def _future_ts(seconds: int) -> str:
    return (now_local() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


async def close_client():
    """Close worker/client resources. Call during shutdown."""
    await stop_broadcast_worker()
    global _client, _send_loop, _send_semaphore, _send_schedule_lock, _next_send_at
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
    _send_loop = None
    _send_semaphore = None
    _send_schedule_lock = None
    _next_send_at = 0.0


def _get_send_controls() -> tuple[asyncio.Semaphore, asyncio.Lock]:
    global _send_loop, _send_semaphore, _send_schedule_lock
    loop = asyncio.get_running_loop()
    if (
        _send_loop is not loop
        or _send_semaphore is None
        or _send_schedule_lock is None
    ):
        _send_loop = loop
        _send_semaphore = asyncio.Semaphore(TELEGRAM_SEND_CONCURRENCY)
        _send_schedule_lock = asyncio.Lock()
    return _send_semaphore, _send_schedule_lock


def _retry_delay_seconds(attempt: int) -> float:
    # 0 -> 0.5s, 1 -> 1.0s, 2 -> 2.0s ... capped
    return min(8.0, 0.5 * (2 ** attempt))


async def _wait_global_send_slot() -> None:
    """Serialize sends to a global minimum spacing to reduce burst 429s."""
    global _next_send_at
    if TELEGRAM_MIN_SEND_INTERVAL_SECONDS <= 0:
        return

    _, lock = _get_send_controls()
    async with lock:
        now = time.monotonic()
        scheduled = max(now, _next_send_at)
        _next_send_at = scheduled + TELEGRAM_MIN_SEND_INTERVAL_SECONDS
        delay = scheduled - now

    if delay > 0:
        await asyncio.sleep(delay)


async def send_message(chat_id: int | str, text: str, parse_mode: str | None = None) -> dict:
    """Send a single Telegram message via the HTTP Bot API."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {"ok": False, "description": "Telegram API disabled in tests"}
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("fake"):
        return {"ok": False, "description": "Telegram API disabled"}

    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    client = _get_client()
    attempts = TELEGRAM_SEND_MAX_RETRIES + 1
    semaphore, _ = _get_send_controls()

    async with semaphore:
        for attempt in range(attempts):
            await _wait_global_send_slot()
            started = time.monotonic()
            try:
                resp = await client.post(f"{_get_api_base()}/sendMessage", json=payload)
                observe_histogram("telegram_send_seconds", time.monotonic() - started)
            except Exception:
                inc_counter("telegram_send_errors_total")
                if attempt < attempts - 1:
                    inc_counter("telegram_send_retries_total")
                    await asyncio.sleep(_retry_delay_seconds(attempt))
                    continue
                raise

            try:
                result = resp.json()
            except Exception:
                inc_counter("telegram_send_errors_total")
                result = {
                    "ok": False,
                    "description": f"Invalid Telegram response (HTTP {resp.status_code})",
                }

            if result.get("ok"):
                inc_counter("telegram_send_success_total")
                return result

            error_code = result.get("error_code")
            if error_code == 429 and attempt < attempts - 1:
                retry_after = result.get("parameters", {}).get("retry_after", 1)
                try:
                    retry_after = float(retry_after)
                except (TypeError, ValueError):
                    retry_after = 1.0
                retry_after = max(retry_after, TELEGRAM_MIN_SEND_INTERVAL_SECONDS)
                inc_counter("telegram_send_rate_limited_total")
                inc_counter("telegram_send_retries_total")
                await asyncio.sleep(retry_after)
                continue

            try:
                is_server_error = int(error_code or 0) >= 500
            except (TypeError, ValueError):
                is_server_error = False

            if is_server_error and attempt < attempts - 1:
                inc_counter("telegram_send_retries_total")
                await asyncio.sleep(_retry_delay_seconds(attempt))
                continue

            inc_counter("telegram_send_failed_total")
            return result

    inc_counter("telegram_send_failed_total")
    return {"ok": False, "description": "Telegram send failed"}


async def send_dm(chat_id: int | str, message: str) -> dict:
    """Send a formatted admin DM to a user."""
    return await send_message(
        chat_id,
        f"<b>Adminlerden Sana Özel Mesaj</b>\n\n{message or ''}",
        parse_mode="HTML",
    )


async def broadcast(user_ids: list[int | str], message: str) -> dict:
    """Direct broadcast helper with bounded concurrency."""
    inc_counter("broadcast_requests_total")
    started = time.monotonic()
    success = 0
    failed = 0
    text = f"<b>Duyuru</b>\n\n{message or ''}"
    sem = asyncio.Semaphore(_BROADCAST_SEND_CONCURRENCY)

    async def _send_one(uid):
        nonlocal success, failed
        async with sem:
            try:
                result = await send_message(uid, text, parse_mode="HTML")
                if result.get("ok"):
                    success += 1
                else:
                    failed += 1
            except Exception:
                logging.warning("Broadcast failed for %s", uid)
                failed += 1

    await asyncio.gather(*(_send_one(uid) for uid in user_ids))
    result = {"success": success, "failed": failed, "total": len(user_ids)}
    observe_histogram("broadcast_duration_seconds", time.monotonic() - started)
    return result


def _enqueue_broadcast_job(user_ids: list[int | str], message: str, initiated_by: str) -> str:
    created_at = db_now()
    with _get_db() as conn:
        cur = conn.execute(
            """INSERT INTO broadcast_jobs
               (initiated_by, message, status, total, success, failed, created_at)
               VALUES (?, ?, 'queued', ?, 0, 0, ?)
               RETURNING id""",
            (initiated_by, message, len(user_ids), created_at),
        )
        row = cur.fetchone()
        if row is not None:
            job_id = int(row["id"])
        elif getattr(cur, "lastrowid", None) is not None:
            job_id = int(cur.lastrowid)
        else:
            row = conn.execute(
                """SELECT id FROM broadcast_jobs
                   WHERE initiated_by = ? AND created_at = ?
                   ORDER BY id DESC LIMIT 1""",
                (initiated_by, created_at),
            ).fetchone()
            job_id = int(row["id"])

        if user_ids:
            rows = [(job_id, str(uid), created_at) for uid in user_ids]
            conn.executemany(
                """INSERT INTO broadcast_recipients (job_id, chat_id, status, updated_at)
                   VALUES (?, ?, 'pending', ?)
                   ON CONFLICT(job_id, chat_id) DO NOTHING""",
                rows,
            )
        else:
            conn.execute(
                """UPDATE broadcast_jobs
                   SET status = 'done', finished_at = ?, total = 0, success = 0, failed = 0
                   WHERE id = ?""",
                (db_now(), job_id),
            )
    inc_counter("broadcast_jobs_enqueued_total")
    return str(job_id)


async def start_background_broadcast(
    user_ids: list[int | str],
    message: str,
    *,
    initiated_by: str = "system",
) -> str:
    """Persist a broadcast job and return its durable job id."""
    return await asyncio.to_thread(_enqueue_broadcast_job, user_ids, message, initiated_by)


def get_broadcast_job(job_id: str | int) -> dict | None:
    with _get_db() as conn:
        row = conn.execute(
            """SELECT id, initiated_by, status, total, success, failed, error,
                      created_at, started_at, finished_at, worker_id
               FROM broadcast_jobs WHERE id = ?""",
            (str(job_id),),
        ).fetchone()
        return dict(row) if row else None


def _claim_next_job(worker_id: str, lease_seconds: int = 300) -> dict | None:
    now_ts = db_now()
    lease_until = _future_ts(lease_seconds)
    with _get_db() as conn:
        row = conn.execute(
            """SELECT id, message
               FROM broadcast_jobs
               WHERE status = 'queued'
                  OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?)
               ORDER BY created_at ASC
               LIMIT 1""",
            (now_ts,),
        ).fetchone()
        if not row:
            return None

        job_id = row["id"]
        cur = conn.execute(
            """UPDATE broadcast_jobs
               SET status = 'running',
                   started_at = COALESCE(started_at, ?),
                   worker_id = ?,
                   lease_until = ?,
                   error = NULL
               WHERE id = ?
                 AND (status = 'queued'
                      OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?))""",
            (now_ts, worker_id, lease_until, job_id, now_ts),
        )
        if cur.rowcount <= 0:
            return None

        claimed = conn.execute(
            "SELECT id, message FROM broadcast_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not claimed:
            return None
        return {"id": int(claimed["id"]), "message": claimed["message"]}


def _fetch_pending_recipients(job_id: int) -> list[dict]:
    with _get_db() as conn:
        rows = conn.execute(
            """SELECT id, chat_id
               FROM broadcast_recipients
               WHERE job_id = ? AND status = 'pending'
               ORDER BY id""",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _mark_recipients_batch(
    job_id: int,
    results: list[tuple[int, bool, str]],
):
    if not results:
        return

    now_ts = db_now()
    sent_rows: list[tuple] = []
    failed_rows: list[tuple] = []
    for recipient_id, ok, error in results:
        if ok:
            sent_rows.append((now_ts, now_ts, recipient_id, job_id))
        else:
            failed_rows.append((error[:500], now_ts, recipient_id, job_id))

    with _get_db() as conn:
        if sent_rows:
            conn.executemany(
                """UPDATE broadcast_recipients
                   SET status = 'sent',
                       attempts = attempts + 1,
                       last_error = NULL,
                       sent_at = ?,
                       updated_at = ?
                   WHERE id = ? AND job_id = ?""",
                sent_rows,
            )
        if failed_rows:
            conn.executemany(
                """UPDATE broadcast_recipients
                   SET status = 'failed',
                       attempts = attempts + 1,
                       last_error = ?,
                       updated_at = ?
                   WHERE id = ? AND job_id = ?""",
                failed_rows,
            )


def _finalize_job(job_id: int, worker_id: str, error: str | None = None):
    with _get_db() as conn:
        stats = conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
               FROM broadcast_recipients
               WHERE job_id = ?""",
            (job_id,),
        ).fetchone()

        total = int((stats["total"] or 0) if stats else 0)
        success = int((stats["success"] or 0) if stats else 0)
        failed = int((stats["failed"] or 0) if stats else 0)
        pending = int((stats["pending"] or 0) if stats else 0)
        status = "running" if pending > 0 and not error else "done"
        if error:
            status = "failed"

        conn.execute(
            """UPDATE broadcast_jobs
               SET status = ?,
                   total = ?,
                   success = ?,
                   failed = ?,
                   error = ?,
                   finished_at = CASE WHEN ? IN ('done', 'failed') THEN ? ELSE finished_at END,
                   lease_until = CASE WHEN ? IN ('done', 'failed') THEN NULL ELSE lease_until END,
                   worker_id = ?
               WHERE id = ?""",
            (
                status,
                total,
                success,
                failed,
                (error[:500] if error else None),
                status,
                db_now(),
                status,
                worker_id,
                job_id,
            ),
        )


async def _process_job(job_id: int, message: str, worker_id: str):
    # Trust boundary: callers (competition handlers, toggle_mode, announce_*)
    # are responsible for HTML-escaping user-supplied content before persisting
    # the broadcast job. The message stored in the DB is treated as pre-escaped HTML.
    recipients = await asyncio.to_thread(_fetch_pending_recipients, job_id)
    if not recipients:
        await asyncio.to_thread(_finalize_job, job_id, worker_id, None)
        return

    text = f"<b>Duyuru</b>\n\n{message}"
    sem = asyncio.Semaphore(_BROADCAST_SEND_CONCURRENCY)

    async def _send(rec: dict):
        rid = int(rec["id"])
        chat_id = rec["chat_id"]
        ok = False
        err = ""
        async with sem:
            try:
                result = await send_message(chat_id, text, parse_mode="HTML")
                if result.get("ok"):
                    ok = True
                else:
                    err = result.get("description", "Send failed")
            except Exception as e:
                err = str(e)
            return rid, ok, err

    for idx in range(0, len(recipients), _BROADCAST_DB_BATCH_SIZE):
        chunk = recipients[idx : idx + _BROADCAST_DB_BATCH_SIZE]
        batch_results = await asyncio.gather(*(_send(r) for r in chunk))
        await asyncio.to_thread(_mark_recipients_batch, job_id, list(batch_results))

    await asyncio.to_thread(_finalize_job, job_id, worker_id, None)
    inc_counter("broadcast_jobs_done_total")


async def _broadcast_worker_loop(role: str):
    worker_id = f"{role}:{os.getpid()}:{uuid4().hex[:8]}"
    logging.info("Broadcast worker started: %s", worker_id)
    while _worker_stop_event and not _worker_stop_event.is_set():
        try:
            job = await asyncio.to_thread(_claim_next_job, worker_id)
            if not job:
                await asyncio.sleep(1.0)
                continue
            await _process_job(job["id"], job["message"], worker_id)
        except Exception as e:
            inc_counter("broadcast_jobs_failed_total")
            logging.exception("Broadcast worker loop error (%s): %s", worker_id, e)
            await asyncio.sleep(1.0)
    logging.info("Broadcast worker stopped: %s", worker_id)


async def start_broadcast_worker(role: str):
    global _worker_task, _worker_stop_event
    if _worker_task and not _worker_task.done():
        return
    _worker_stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_broadcast_worker_loop(role))


async def stop_broadcast_worker():
    global _worker_task, _worker_stop_event
    if _worker_stop_event:
        _worker_stop_event.set()
    if _worker_task:
        await asyncio.gather(_worker_task, return_exceptions=True)
    _worker_task = None
    _worker_stop_event = None


_MODE_META = {
    "maintenance": ("Bakım Modu", "🔴", "🟢", "Sistem bakım modunda. Erişim geçici olarak durduruldu.", "Sistem aktif. Tüm kullanıcılar erişebilir."),
    "silent": ("Sessiz Mod", "🔕", "🔔", "Bildirimler kapatıldı.", "Bildirimler aktif."),
}

_WINNER_NOTIFY_LABELS: dict[str, str] = {
    "everyone": "herkese",
    "admins": "yöneticilere",
    "none": "kimseye bildirim yok",
}

_WINNER_NOTIFY_CYCLE = ["everyone", "admins", "none"]


async def set_winner_notify_target(target: str, changed_by: str) -> str:
    """Persist a new winner notification target and notify admins.

    Returns the new target value.
    """
    if target not in _WINNER_NOTIFY_LABELS:
        raise ValueError(f"Invalid winner notify target: {target!r}")

    await asyncio.to_thread(setattr, _bot_state, "winner_notify_target", target)

    label = _WINNER_NOTIFY_LABELS[target]
    text = f"<b>📣 Kazanan Duyurusu: {html.escape(label)}</b>"

    admin_ids = await asyncio.to_thread(_get_all_admin_ids)

    async def _notify(admin_id: int):
        try:
            await send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            print(f"Admin notification error ({admin_id}): {e}")

    if admin_ids:
        await asyncio.gather(*(_notify(admin_id) for admin_id in admin_ids))

    return target


async def toggle_mode(mode: str, changed_by: str) -> dict:
    """Toggle a bot mode, notify admins, and return the result."""
    label, on_icon, off_icon, on_desc, off_desc = _MODE_META[mode]

    current = await asyncio.to_thread(lambda: getattr(_bot_state, f"{mode}_mode"))
    new_value = not current
    await asyncio.to_thread(setattr, _bot_state, f"{mode}_mode", new_value)

    icon = on_icon if new_value else off_icon
    desc = on_desc if new_value else off_desc
    state = "aktif" if new_value else "kapalı"
    status_text = f"{icon} {label}: {state}"

    text = f"<b>{html.escape(status_text)}</b>\n\n{desc}"

    # Maintenance mode changes are broadcast to all known users
    if mode == "maintenance":
        user_ids = await asyncio.to_thread(_get_known_user_ids)
        if user_ids:
            await start_background_broadcast(
                user_ids,
                text,
                initiated_by=f"maintenance_toggle_{html.escape(changed_by)}",
            )
    else:
        admin_ids = await asyncio.to_thread(_get_all_admin_ids)

        async def _notify(admin_id: int):
            try:
                await send_message(admin_id, text)
            except Exception as e:
                print(f"Admin notification error ({admin_id}): {e}")

        if admin_ids:
            await asyncio.gather(*(_notify(admin_id) for admin_id in admin_ids))

    return {"new_value": new_value, "status_text": status_text}


async def notify_admins(message: str, silent: bool = False) -> None:
    """Notify admins about important events (no-op in global silent mode).

    When global silent is OFF, per-admin silent flags are respected:
    admins with silent=True in the DB are excluded.
    """
    if silent:
        return
    silent_mode = await asyncio.to_thread(lambda: _bot_state.silent_mode)
    if silent_mode:
        return

    admin_ids = await asyncio.to_thread(_get_notifiable_admin_ids)

    async def _notify(admin_id: int):
        try:
            await send_message(admin_id, message, parse_mode="HTML")
        except Exception as e:
            print(f"Admin notification error ({admin_id}): {e}")

    if admin_ids:
        await asyncio.gather(*(_notify(admin_id) for admin_id in admin_ids))


_DIFFICULTY_NOTIFY_COPY_TR: dict[str, tuple[str, str]] = {
    # Copy is intentionally written along a "hard -> easy" mental model:
    # stricter (harder) at the top, more permissive (easier) at the bottom.
    "IMPOSSIBLE": (
        "İMKANSIZ",
        "En sıkı savunma: neredeyse sıfır tolerans.",
    ),
    "HARD": (
        "ZOR",
        "Sıkı savunma: ipuçları sınırlı, açık yakalaman gerek.",
    ),
    "MEDIUM": (
        "ORTA",
        "Dengeli mod: yardım var ama kalkan hâlâ aktif.",
    ),
    "EASY": (
        "KOLAY",
        "En rahat mod: daha fazla ipucu, daha fazla tolerans.",
    ),
}

_DIFFICULTY_RANK: dict[str, int] = {
    "EASY": 0,
    "MEDIUM": 1,
    "HARD": 2,
    "IMPOSSIBLE": 3,
}


async def announce_difficulty_changed(level: str, *, previous: str | None = None) -> dict:
    """Broadcast AI difficulty changes to all known users."""
    normalized = str(level or "").strip().upper()
    prev = str(previous or "").strip().upper() if previous else ""

    icon_new = DIFFICULTY_SYMBOLS.get(normalized, "")
    title_new, blurb = _DIFFICULTY_NOTIFY_COPY_TR.get(normalized, (normalized, ""))

    heading = "Zorluk Güncellendi"
    if prev and prev in _DIFFICULTY_RANK and normalized in _DIFFICULTY_RANK and prev != normalized:
        delta = _DIFFICULTY_RANK[normalized] - _DIFFICULTY_RANK[prev]
        if delta > 0:
            heading = "Zorluk Artırıldı"
        elif delta < 0:
            heading = "Zorluk Azaltıldı"

    message = f"<b>{heading}</b>\n\n"
    if prev and prev != normalized:
        icon_prev = DIFFICULTY_SYMBOLS.get(prev, "")
        title_prev, _ = _DIFFICULTY_NOTIFY_COPY_TR.get(prev, (prev, ""))
        message += (
            f"{html.escape(icon_prev)} {html.escape(title_prev)} → "
            f"{html.escape(icon_new)} {html.escape(title_new)}"
        )
    else:
        message += f"{html.escape(icon_new)} {html.escape(title_new)}"

    if blurb:
        message += f"\n\n<i>{html.escape(blurb)}</i>"

    user_ids = await asyncio.to_thread(_get_known_user_ids)
    job_id = await start_background_broadcast(
        user_ids,
        message,
        initiated_by=f"difficulty_changed_{normalized.lower() or 'unknown'}",
    )
    return {"audience": "all", "job_id": job_id, "total": len(user_ids)}


def _get_flag_found_count(flag_id: int) -> int:
    """Count how many users have found a specific flag."""
    with _get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM user_flag_progress WHERE flag_id = ?",
            (flag_id,),
        ).fetchone()
        return row["cnt"]


def _get_known_user_count() -> int:
    with _get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM known_users").fetchone()
        return row["cnt"]


async def announce_flag_found(
    user_id: str | int,
    first_name: str,
    username: str,
    *,
    flag_id: int | None = None,
    flag_position: int | None = None,
    flag_tag: str = "",
    notify_message: str = "",
    initiated_by: str = "competition_winner_announcement",
) -> dict:
    """Announce flag winner according to winner_broadcast mode.

    Returns metadata describing delivery audience.
    """
    from models.user import _normalize_username
    safe_username = _normalize_username(username)
    safe_first_name = (first_name or "").strip()
    if safe_username:
        who = f"@{html.escape(safe_username)}"
    elif safe_first_name:
        who = html.escape(safe_first_name)
    else:
        who = f"ID {html.escape(str(user_id))}"

    flag_label = ""
    if flag_position is not None:
        flag_label = f"Flag #{flag_position}"
        if flag_tag:
            flag_label = f"Flag #{flag_position} — {html.escape(flag_tag)}"

    if notify_message:
        found_count = 0
        total_users = 0
        if flag_id is not None:
            found_count = await asyncio.to_thread(_get_flag_found_count, flag_id)
        total_users = await asyncio.to_thread(_get_known_user_count)

        template_vars = {
            "who": who,
            "username": html.escape(safe_username) if safe_username else "",
            "first_name": html.escape(safe_first_name),
            "flag_position": str(flag_position) if flag_position is not None else "",
            "flag_tag": html.escape(flag_tag) if flag_tag else "",
            "flag_label": flag_label,
            "found_count": str(found_count),
            "total_users": str(total_users),
        }

        def _replacer(m):
            key = m.group(1)
            if key in template_vars:
                return str(template_vars[key])
            return m.group(0)

        message = re.sub(r"\{(\w+)\}", _replacer, notify_message)
    else:
        label_parens = f" ({flag_label})" if flag_label else ""
        message = (
            f"<b>Flag Bulundu{label_parens}</b>\n\n"
            f"{who} flag'i başarıyla buldu."
        )

    target = await asyncio.to_thread(lambda: _bot_state.winner_notify_target)

    if target == "none":
        return {"audience": "none", "total": 0}

    if target == "everyone":
        user_ids = await asyncio.to_thread(_get_known_user_ids)
        job_id = await start_background_broadcast(
            user_ids,
            message,
            initiated_by=initiated_by,
        )
        return {"audience": "all", "job_id": job_id, "total": len(user_ids)}

    # target == "admins"
    admin_ids = await asyncio.to_thread(_get_all_admin_ids)

    async def _notify(admin_id: int):
        try:
            await send_message(admin_id, message, parse_mode="HTML")
        except Exception as e:
            logging.warning("Admin winner notification failed (%s): %s", admin_id, e)

    if admin_ids:
        await asyncio.gather(*(_notify(admin_id) for admin_id in admin_ids))
    return {"audience": "admins", "total": len(admin_ids)}
