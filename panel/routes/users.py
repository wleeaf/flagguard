import csv
import io
import urllib.parse
from datetime import timedelta

from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import RedirectResponse, Response

from models.user import UserRepository
from models.conversation import ConversationRepository
from models.admin import AdminRepository
from models.moderation import ModerationRepository
from models.ctf_flags import CTFFlagRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from services import is_admin as _is_admin
from panel.queries import (
    clear_user_logs,
    full_reset_user,
    get_users_paginated,
    get_user_conversation_entry,
    get_user_detail,
    get_user_conversations,
    get_user_interactions,
    get_user_interactions_for_export,
    log_admin_message,
    log_audit,
)
from time_utils import now_local
import notifications

router = APIRouter(prefix="/panel")

_user_repo = UserRepository()
_conv_repo = ConversationRepository()
_moderation_repo = ModerationRepository()
_admin_repo = AdminRepository()
_flag_repo = CTFFlagRepository()


# ── List ──────────────────────────────────────────────────────────────────

@router.get("/users")
async def user_list(
    request: Request,
    user: dict = Depends(require_auth),
    search: str = "",
    sort: str = "last_seen",
    order: str = "desc",
    page: int = 1,
):
    data = await run_blocking(
        get_users_paginated,
        page=max(1, page), search=search.strip(), sort_by=sort, order=order,
    )
    ctx = {
        "request": request,
        "user": user,
        "search": search,
        "sort": sort,
        "order": order,
        **data,
    }

    # hx-boost navigation sets HX-Boosted — treat it as a full page load.
    # Only search/sort/pagination triggers (hx-get) should get the partial.
    is_partial = request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")
    if is_partial:
        return templates.TemplateResponse(request, "users/_table.html", ctx)
    return templates.TemplateResponse(request, "users/list.html", ctx)


# ── Detail ────────────────────────────────────────────────────────────────

@router.get("/users/{user_id}")
async def user_detail_page(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
    msg: str = "",
    msg_type: str = "success",
):
    detail = await run_blocking(get_user_detail, user_id)
    if not detail:
        raise HTTPException(status_code=404, detail="User not found")

    conversations = await run_blocking(get_user_conversations, user_id)
    is_target_admin = _is_admin(int(user_id)) if user_id.isdigit() else False
    flag_progress = await run_blocking(_flag_repo.get_user_progress, user_id)

    return templates.TemplateResponse(
        request,
        "users/detail.html",
        {
            "request": request,
            "user": user,
            "detail": detail,
            "conversations": conversations,
            "is_target_admin": is_target_admin,
            "flag_progress": flag_progress,
            "msg": msg,
            "msg_type": msg_type,
        },
    )


# ── Chat view ────────────────────────────────────────────────────────

@router.get("/users/{user_id}/chat")
async def user_chat_view(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    detail = await run_blocking(get_user_detail, user_id)
    if not detail:
        raise HTTPException(status_code=404, detail="User not found")

    interactions = await run_blocking(get_user_interactions, user_id)

    return templates.TemplateResponse(
        request,
        "users/_chat.html",
        {
            "request": request,
            "detail": detail,
            "interactions": interactions,
        },
    )


@router.get("/users/{user_id}/conversation/{conversation_id}")
async def user_conversation_chat_detail(
    request: Request,
    user_id: str,
    conversation_id: int,
    user: dict = Depends(require_auth),
):
    detail = await run_blocking(get_user_detail, user_id)
    if not detail:
        raise HTTPException(status_code=404, detail="User not found")

    entry = await run_blocking(get_user_conversation_entry, user_id, conversation_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Conversation entry not found")

    return templates.TemplateResponse(
        request,
        "users/_conversation_chat.html",
        {
            "request": request,
            "detail": detail,
            "entry": entry,
        },
    )


# ── Actions ───────────────────────────────────────────────────────────────

@router.post("/users/{user_id}/reset")
async def reset_behavior(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    await run_blocking(_user_repo.reset_behavior, user_id)
    await run_blocking(log_audit, user["username"], "reset_behavior", target=user_id)
    return RedirectResponse(
        f"/panel/users/{user_id}?msg=Behavior+reset+successfully", status_code=303
    )


@router.post("/users/{user_id}/clear-history")
async def clear_history(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    await run_blocking(_conv_repo.clear_history, user_id)
    await run_blocking(log_audit, user["username"], "clear_history", target=user_id)
    return RedirectResponse(
        f"/panel/users/{user_id}?msg=Conversation+history+cleared", status_code=303
    )


@router.post("/users/{user_id}/send-dm")
async def send_dm_from_profile(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
    message: str = Form(...),
):
    detail = await run_blocking(get_user_detail, user_id)
    if not detail:
        raise HTTPException(status_code=404, detail="User not found")
    if not user_id.isdigit():
        return _redirect_user_detail(user_id, "Invalid user ID", "error")

    text = message.strip()
    if not text:
        return _redirect_user_detail(user_id, "Message cannot be empty", "error")

    result = await notifications.send_dm(user_id, text)
    if not result.get("ok"):
        desc = result.get("description", "Unknown error")
        return _redirect_user_detail(user_id, f"Failed to send DM: {desc}", "error")

    await run_blocking(log_audit, user["username"], "send_dm", target=user_id, detail="from_user_profile")
    await run_blocking(log_admin_message, "dm", text, user["username"], user_id)
    return _redirect_user_detail(user_id, "DM sent successfully", "success")


@router.post("/users/{user_id}/clear-logs")
async def clear_logs(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    if not user_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid user ID")

    deleted = await run_blocking(clear_user_logs, user_id)
    await run_blocking(
        log_audit,
        user["username"],
        "clear_logs",
        target=user_id,
        detail=f"conversation_logs={deleted['conversation_logs']}, message_log={deleted['message_log']}",
    )
    return _redirect_user_detail(
        user_id,
        f"Logs cleared (conversation={deleted['conversation_logs']}, messages={deleted['message_log']})",
        "success",
    )


@router.post("/users/{user_id}/full-reset")
async def full_reset(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    if not user_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid user ID")

    counts = await run_blocking(full_reset_user, user_id)
    total = sum(counts.values())
    await run_blocking(
        log_audit,
        user["username"],
        "full_reset_user",
        target=user_id,
        detail=f"deleted {total} rows across {sum(1 for v in counts.values() if v)} tables",
    )
    return _redirect_user_detail(
        user_id,
        f"Full reset complete ({total} rows deleted across all tables)",
        "success",
    )


@router.get("/users/{user_id}/export-logs")
async def export_user_logs(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    detail = await run_blocking(get_user_detail, user_id)
    if not detail:
        raise HTTPException(status_code=404, detail="User not found")

    rows = await run_blocking(get_user_interactions_for_export, user_id)
    await run_blocking(
        log_audit,
        user["username"],
        "export_user_logs",
        target=user_id,
        detail=f"{len(rows)} rows",
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "entry_type", "user_msg", "ai_msg"])
    for row in rows:
        writer.writerow(
            [
                row.get("timestamp", ""),
                row.get("type", ""),
                row.get("user_msg", ""),
                row.get("ai_msg", ""),
            ]
        )
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="user_{user_id}_interactions.csv"',
        },
    )


@router.post("/users/{user_id}/ban")
async def ban_from_detail(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    if not user_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid user ID")
    if _is_admin(int(user_id)):
        return _redirect_user_detail(user_id, "Yöneticiler banlanamaz.", "error")
    await run_blocking(_moderation_repo.ban_user, user_id, user["username"])
    await run_blocking(log_audit, user["username"], "ban_user", target=user_id, detail="from_user_detail")
    return _redirect_user_detail(user_id, "Kullanıcı banlandı.", "success")


@router.post("/users/{user_id}/timeout")
async def timeout_from_detail(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
    duration: str = Form(...),
):
    if not user_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid user ID")
    if _is_admin(int(user_id)):
        return _redirect_user_detail(user_id, "Yöneticilere timeout uygulanamaz.", "error")
    try:
        seconds = int(duration)
    except ValueError:
        return _redirect_user_detail(user_id, "Geçersiz süre", "error")
    if seconds <= 0:
        return _redirect_user_detail(user_id, "Süre pozitif olmalıdır", "error")

    until_dt = now_local() + timedelta(seconds=seconds)
    until_str = until_dt.strftime("%Y-%m-%d %H:%M:%S")
    await run_blocking(_moderation_repo.timeout_user, user_id, until_str, user["username"])
    await run_blocking(log_audit, user["username"], "timeout_user", target=user_id, detail=f"{seconds}s from_user_detail")
    return _redirect_user_detail(user_id, "Kullanıcı kısıtlandı.", "success")


@router.post("/users/{user_id}/make-admin")
async def make_admin_from_detail(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    if not user_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid user ID")
    detail = await run_blocking(get_user_detail, user_id)
    label = (detail or {}).get("first_name") or ""
    inserted = await run_blocking(_admin_repo.add_admin, user_id, label)
    if not inserted:
        return _redirect_user_detail(user_id, "Kullanıcı zaten yönetici.", "error")
    await run_blocking(log_audit, user["username"], "make_admin", target=user_id, detail="from_user_detail")
    return _redirect_user_detail(user_id, "Kullanıcı yönetici yapıldı.", "success")


@router.post("/users/{user_id}/remove-admin")
async def remove_admin_from_detail(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    if not user_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid user ID")
    removed = await run_blocking(_admin_repo.remove_admin, user_id)
    if not removed:
        return _redirect_user_detail(user_id, "Kullanıcı yönetici değil.", "error")
    await run_blocking(log_audit, user["username"], "remove_admin", target=user_id, detail="from_user_detail")
    return _redirect_user_detail(user_id, "Yöneticilik kaldırıldı.", "success")


def _redirect_user_detail(user_id: str, msg: str, msg_type: str = "success") -> RedirectResponse:
    qs = urllib.parse.urlencode({"msg": msg, "msg_type": msg_type})
    return RedirectResponse(f"/panel/users/{user_id}?{qs}", status_code=303)
