import urllib.parse
from datetime import timedelta

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse

from models.moderation import ModerationRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import get_banned_users, get_timed_out_users, log_audit
from services import is_admin as _is_admin
from time_utils import now_local

router = APIRouter(prefix="/panel")

_moderation_repo = ModerationRepository()

_DURATION_MAP = {
    "300": "5 dakika",
    "900": "15 dakika",
    "3600": "1 saat",
    "21600": "6 saat",
    "86400": "24 saat",
    "604800": "7 gün",
}


def _redirect_moderation(msg: str, msg_type: str = "success") -> RedirectResponse:
    qs = urllib.parse.urlencode({"msg": msg, "msg_type": msg_type})
    return RedirectResponse(f"/panel/moderation?{qs}", status_code=303)


@router.get("/moderation")
async def moderation_page(
    request: Request,
    user: dict = Depends(require_auth),
    msg: str = "",
    msg_type: str = "success",
):
    banned = await run_blocking(get_banned_users)
    timed_out = await run_blocking(get_timed_out_users)
    now_str = now_local().strftime("%Y-%m-%d %H:%M:%S")

    return templates.TemplateResponse(
        request,
        "moderation.html",
        {
            "request": request,
            "user": user,
            "banned_users": banned,
            "timed_out_users": timed_out,
            "duration_map": _DURATION_MAP,
            "now_str": now_str,
            "msg": msg,
            "msg_type": msg_type,
        },
    )


@router.post("/moderation/ban")
async def ban_user(
    request: Request,
    user: dict = Depends(require_auth),
    user_id: str = Form(...),
):
    uid = user_id.strip()
    if not uid.isdigit():
        return _redirect_moderation("Geçersiz User ID", "error")
    if _is_admin(int(uid)):
        return _redirect_moderation("Yöneticiler banlanamaz.", "error")

    await run_blocking(_moderation_repo.ban_user, uid, user["username"])
    await run_blocking(log_audit, user["username"], "ban_user", target=uid)
    return _redirect_moderation(f"Kullanıcı {uid} banlandı.")


@router.post("/moderation/{user_id}/unban")
async def unban_user(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    await run_blocking(_moderation_repo.unban_user, user_id)
    await run_blocking(log_audit, user["username"], "unban_user", target=user_id)
    return _redirect_moderation(f"Kullanıcı {user_id} banı kaldırıldı.")


@router.post("/moderation/timeout")
async def timeout_user(
    request: Request,
    user: dict = Depends(require_auth),
    user_id: str = Form(...),
    duration: str = Form(...),
):
    uid = user_id.strip()
    if not uid.isdigit():
        return _redirect_moderation("Geçersiz User ID", "error")
    if _is_admin(int(uid)):
        return _redirect_moderation("Yöneticilere timeout uygulanamaz.", "error")

    try:
        seconds = int(duration)
    except ValueError:
        return _redirect_moderation("Geçersiz süre", "error")

    if seconds <= 0:
        return _redirect_moderation("Süre pozitif olmalıdır", "error")

    until_dt = now_local() + timedelta(seconds=seconds)
    until_str = until_dt.strftime("%Y-%m-%d %H:%M:%S")

    await run_blocking(_moderation_repo.timeout_user, uid, until_str, user["username"])
    label = _DURATION_MAP.get(duration, f"{seconds}s")
    await run_blocking(
        log_audit, user["username"], "timeout_user", target=uid, detail=label
    )
    return _redirect_moderation(f"Kullanıcı {uid} {label} süreliğine kısıtlandı.")


@router.post("/moderation/{user_id}/remove-timeout")
async def remove_timeout(
    request: Request,
    user_id: str,
    user: dict = Depends(require_auth),
):
    await run_blocking(_moderation_repo.remove_timeout, user_id)
    await run_blocking(log_audit, user["username"], "remove_timeout", target=user_id)
    return _redirect_moderation(f"Kullanıcı {user_id} kısıtlaması kaldırıldı.")


@router.post("/moderation/cleanup-expired")
async def cleanup_expired(
    request: Request,
    user: dict = Depends(require_auth),
):
    count = await run_blocking(_moderation_repo.cleanup_expired_timeouts)
    await run_blocking(
        log_audit, user["username"], "cleanup_expired_timeouts", detail=f"{count} rows"
    )
    return _redirect_moderation(f"{count} süresi dolmuş kısıtlama temizlendi.")
