import urllib.parse

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse

from config import ADMIN_IDS
from models.admin import AdminRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import log_audit

router = APIRouter(prefix="/panel")

_admin_repo = AdminRepository()


@router.get("/admins")
async def admins_page(
    request: Request,
    user: dict = Depends(require_auth),
    msg: str = "",
    msg_type: str = "success",
):
    db_admins = await run_blocking(_admin_repo.list_admins)
    return templates.TemplateResponse(
        request,
        "admins.html",
        {
            "request": request,
            "user": user,
            "env_admin_ids": ADMIN_IDS,
            "db_admins": db_admins,
            "msg": msg,
            "msg_type": msg_type,
        },
    )


@router.post("/admins/add")
async def add_admin(
    request: Request,
    user: dict = Depends(require_auth),
    telegram_id: str = Form(""),
    label: str = Form(""),
):
    tid = telegram_id.strip()
    if not tid or not tid.isdigit():
        return _redirect("Telegram ID must be a numeric value.", "error")

    inserted = await run_blocking(_admin_repo.add_admin, tid, label.strip())
    if not inserted:
        return _redirect(f"Admin {tid} already exists.", "error")

    await run_blocking(log_audit, user["username"], "add_telegram_admin", detail=tid)
    return _redirect(f"Admin {tid} added successfully.")


@router.post("/admins/{telegram_id}/remove")
async def remove_admin(
    request: Request,
    telegram_id: str,
    user: dict = Depends(require_auth),
):
    removed = await run_blocking(_admin_repo.remove_admin, telegram_id)
    if not removed:
        return _redirect(f"Admin {telegram_id} not found.", "error")

    await run_blocking(log_audit, user["username"], "remove_telegram_admin", detail=telegram_id)
    return _redirect(f"Admin {telegram_id} removed.")


@router.post("/admins/{telegram_id}/toggle-silent")
async def toggle_silent(
    request: Request,
    telegram_id: str,
    user: dict = Depends(require_auth),
):
    # Ensure the admin exists in DB (env-only admins may not have a row yet)
    await run_blocking(_admin_repo.add_admin, telegram_id, "")
    new_val = await run_blocking(_admin_repo.toggle_silent, telegram_id)
    state = "muted" if new_val else "unmuted"
    await run_blocking(
        log_audit,
        user["username"],
        "toggle_admin_silent",
        detail=f"{telegram_id} → {state}",
    )
    return _redirect(f"Admin {telegram_id} {state}.")


def _redirect(msg: str, msg_type: str = "success") -> RedirectResponse:
    qs = urllib.parse.urlencode({"msg": msg, "msg_type": msg_type})
    return RedirectResponse(f"/panel/admins?{qs}", status_code=303)
