import time
from collections import deque
import urllib.parse
import csv
import io
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import JSONResponse, RedirectResponse, Response

from models.user import UserRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import (
    clear_admin_messages,
    delete_admin_message,
    get_admin_messages,
    get_admin_messages_for_export,
    log_audit,
    log_admin_message,
)
import notifications

router = APIRouter(prefix="/panel")

_user_repo = UserRepository()
_ACTION_LIMITS: dict[str, tuple[int, int]] = {
    "dm": (20, 60),          # 20 DMs per minute per admin
    "broadcast": (3, 300),   # 3 broadcasts per 5 minutes per admin
}
_ACTION_HISTORY: dict[tuple[str, str], deque[float]] = {}
_ALLOWED_MESSAGE_TYPES = {"dm", "broadcast"}


def _check_action_limit(username: str, action: str) -> tuple[bool, int]:
    limit, window = _ACTION_LIMITS[action]
    now = time.time()
    key = (username, action)
    entries = _ACTION_HISTORY.setdefault(key, deque())

    while entries and now - entries[0] > window:
        entries.popleft()

    if len(entries) >= limit:
        retry_after = max(1, int(window - (now - entries[0])))
        return False, retry_after

    entries.append(now)
    return True, 0


def _normalize_msg_type(msg_type: str) -> str:
    normalized = (msg_type or "").strip().lower()
    if normalized not in _ALLOWED_MESSAGE_TYPES:
        return ""
    return normalized


@router.get("/comms")
async def comms_page(
    request: Request,
    user: dict = Depends(require_auth),
    msg: str = "",
    msg_type: str = "success",
):
    known_count = await run_blocking(_user_repo.count_known_users)
    dm_history = await run_blocking(get_admin_messages, msg_type="dm", limit=25)
    broadcast_history = await run_blocking(get_admin_messages, msg_type="broadcast", limit=25)
    return templates.TemplateResponse(
        request,
        "comms.html",
        {
            "request": request,
            "user": user,
            "known_count": known_count,
            "msg": msg,
            "msg_type": msg_type,
            "dm_history": dm_history,
            "broadcast_history": broadcast_history,
        },
    )


@router.post("/comms/dm")
async def send_dm(
    request: Request,
    user: dict = Depends(require_auth),
    chat_id: str = Form(...),
    message: str = Form(...),
):
    chat_id = chat_id.strip()
    message = message.strip()
    if not chat_id or not message:
        return _redirect("Recipient and message are required", "error")
    if not chat_id.isdigit():
        return _redirect("Invalid user ID", "error")

    allowed, retry_after = _check_action_limit(user["username"], "dm")
    if not allowed:
        return _redirect(f"DM rate limit exceeded. Try again in {retry_after}s", "error")

    result = await notifications.send_dm(chat_id, message)

    if result.get("ok"):
        await run_blocking(log_audit, user["username"], "send_dm", target=chat_id)
        await run_blocking(log_admin_message, "dm", message, user["username"], chat_id)
        return _redirect("Message sent successfully")
    else:
        desc = result.get("description", "Unknown error")
        return _redirect(f"Failed: {desc}", "error")


@router.post("/comms/broadcast")
async def send_broadcast(
    request: Request,
    user: dict = Depends(require_auth),
    message: str = Form(...),
):
    message = message.strip()
    if not message:
        return _redirect("Message cannot be empty", "error")

    allowed, retry_after = _check_action_limit(user["username"], "broadcast")
    if not allowed:
        return _redirect(
            f"Broadcast rate limit exceeded. Try again in {retry_after}s",
            "error",
        )

    user_ids = await run_blocking(_user_repo.get_all_known_ids)
    if not user_ids:
        return _redirect("No known users to broadcast to", "error")

    job_id = await notifications.start_background_broadcast(
        user_ids,
        message,
        initiated_by=user["username"],
    )

    await run_blocking(
        log_audit,
        user["username"], "broadcast",
        detail=f"job_id={job_id}, total={len(user_ids)}",
    )
    await run_blocking(log_admin_message, "broadcast", message, user["username"])
    return _redirect(
        f"Broadcast queued for {len(user_ids)} users (job: {job_id})"
    )


@router.get("/comms/export/{msg_type}")
async def export_messages(
    msg_type: str,
    user: dict = Depends(require_auth),
):
    normalized_type = _normalize_msg_type(msg_type)
    if not normalized_type:
        return _redirect("Invalid message type", "error")

    rows = await run_blocking(get_admin_messages_for_export, normalized_type)
    await run_blocking(
        log_audit,
        user["username"],
        f"export_{normalized_type}_logs",
        detail=f"{len(rows)} row(s)",
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "timestamp", "msg_type", "target", "message", "sent_by"])
    for row in rows:
        writer.writerow([
            row.get("id", ""),
            row.get("timestamp", ""),
            row.get("msg_type", ""),
            row.get("target", ""),
            row.get("message", ""),
            row.get("sent_by", ""),
        ])
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{normalized_type}_logs.csv"',
        },
    )


@router.post("/comms/{msg_type}/clear")
async def clear_messages(
    msg_type: str,
    user: dict = Depends(require_auth),
):
    normalized_type = _normalize_msg_type(msg_type)
    if not normalized_type:
        return _redirect("Invalid message type", "error")

    deleted_count = await run_blocking(clear_admin_messages, normalized_type)
    await run_blocking(
        log_audit,
        user["username"],
        f"clear_{normalized_type}_logs",
        detail=f"{deleted_count} row(s)",
    )
    return _redirect(f"Deleted {deleted_count} {normalized_type} log(s)")


@router.post("/comms/{msg_type}/{message_id}/delete")
async def delete_message(
    msg_type: str,
    message_id: int,
    user: dict = Depends(require_auth),
):
    normalized_type = _normalize_msg_type(msg_type)
    if not normalized_type:
        return _redirect("Invalid message type", "error")

    deleted = await run_blocking(delete_admin_message, message_id, normalized_type)
    if not deleted:
        return _redirect("Message log not found", "error")

    await run_blocking(
        log_audit,
        user["username"],
        f"delete_{normalized_type}_log",
        target=str(message_id),
    )
    return _redirect("Log entry deleted")


def _redirect(msg: str, msg_type: str = "success"):
    qs = urllib.parse.urlencode({"msg": msg, "msg_type": msg_type})
    return RedirectResponse(f"/panel/comms?{qs}", status_code=303)


@router.get("/comms/jobs/{job_id}")
async def broadcast_job_status(
    job_id: str,
    user: dict = Depends(require_auth),
):
    job = await run_blocking(notifications.get_broadcast_job, job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "job_not_found"}, status_code=404)
    return JSONResponse({"ok": True, "job": job})
