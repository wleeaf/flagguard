import urllib.parse

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse

from panel.auth import hash_password, validate_password
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import (
    change_panel_user_password,
    create_panel_user,
    delete_panel_user,
    get_panel_users,
    log_audit,
)

router = APIRouter(prefix="/panel")


@router.get("/panel-users")
async def panel_users_page(
    request: Request,
    user: dict = Depends(require_auth),
    msg: str = "",
    msg_type: str = "success",
):
    users = await run_blocking(get_panel_users)
    return templates.TemplateResponse(
        request,
        "panel_users.html",
        {
            "request": request,
            "user": user,
            "panel_users": users,
            "msg": msg,
            "msg_type": msg_type,
        },
    )


@router.post("/panel-users/create")
async def create_user(
    request: Request,
    user: dict = Depends(require_auth),
    username: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
):
    username = username.strip()
    if not username:
        return _redirect("Username cannot be empty.", "error")

    if password != confirm_password:
        return _redirect("Passwords do not match.", "error")

    err = validate_password(password)
    if err:
        return _redirect(err, "error")

    pw_hash = hash_password(password)
    created = await run_blocking(create_panel_user, username, pw_hash)
    if not created:
        return _redirect(f"User '{username}' already exists.", "error")

    await run_blocking(log_audit, user["username"], "create_panel_user", detail=username)
    return _redirect(f"User '{username}' created successfully.")


@router.post("/panel-users/{target_username}/delete")
async def delete_user(
    request: Request,
    target_username: str,
    user: dict = Depends(require_auth),
):
    if target_username == user["username"]:
        return _redirect("You cannot delete your own account.", "error")

    err = await run_blocking(delete_panel_user, target_username)
    if err:
        return _redirect(err, "error")

    await run_blocking(log_audit, user["username"], "delete_panel_user", detail=target_username)
    return _redirect(f"User '{target_username}' deleted.")


@router.post("/panel-users/{target_username}/change-password")
async def change_password(
    request: Request,
    target_username: str,
    user: dict = Depends(require_auth),
    password: str = Form(""),
    confirm_password: str = Form(""),
):
    if password != confirm_password:
        return _redirect("Passwords do not match.", "error")

    err = validate_password(password)
    if err:
        return _redirect(err, "error")

    pw_hash = hash_password(password)
    updated = await run_blocking(change_panel_user_password, target_username, pw_hash)
    if not updated:
        return _redirect(f"User '{target_username}' not found.", "error")

    await run_blocking(log_audit, user["username"], "change_panel_user_password", detail=target_username)
    return _redirect(f"Password changed for '{target_username}'.")


def _redirect(msg: str, msg_type: str = "success") -> RedirectResponse:
    qs = urllib.parse.urlencode({"msg": msg, "msg_type": msg_type})
    return RedirectResponse(f"/panel/panel-users?{qs}", status_code=303)
