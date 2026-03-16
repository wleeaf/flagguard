import time

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from config import PANEL_COOKIE_SECURE, PANEL_TOKEN_EXPIRE_HOURS
from database import get_db
from panel.dependencies import run_blocking
from panel.auth import verify_password, create_token
from panel.core import templates
from panel.queries import log_audit
from time_utils import db_now

router = APIRouter()

# Brute-force protection: IP → list of failure timestamps (+ explicit lockout timer)
_login_attempts: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 300       # 5 minutes
_LOCKOUT_SECONDS = 900      # 15 minutes


def _get_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_login_rate(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login."""
    now = time.time()

    # Prune expired lockouts and stale attempt records periodically
    if len(_lockouts) > 500:
        expired = [k for k, v in _lockouts.items() if now >= v]
        for k in expired:
            del _lockouts[k]
    if len(_login_attempts) > 500:
        cutoff = now - _WINDOW_SECONDS
        stale = [k for k, v in _login_attempts.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _login_attempts[k]

    locked_until = _lockouts.get(ip, 0.0)
    if now < locked_until:
        return False
    if locked_until:
        _lockouts.pop(ip, None)

    timestamps = _login_attempts.get(ip, [])
    recent = [t for t in timestamps if now - t < _WINDOW_SECONDS]
    _login_attempts[ip] = recent
    return True


def _record_login_attempt(ip: str):
    """Record a failed login attempt."""
    now = time.time()
    timestamps = _login_attempts.setdefault(ip, [])
    timestamps.append(now)
    timestamps[:] = [t for t in timestamps if now - t < _WINDOW_SECONDS]

    if len(timestamps) >= _MAX_ATTEMPTS:
        _lockouts[ip] = now + _LOCKOUT_SECONDS
        _login_attempts[ip] = []

    # Cleanup when dict grows too large
    if len(_login_attempts) > 1000:
        stale = [k for k, v in _login_attempts.items() if not v]
        for k in stale:
            del _login_attempts[k]
    if len(_lockouts) > 1000:
        stale = [k for k, v in _lockouts.items() if now >= v]
        for k in stale:
            del _lockouts[k]


def _get_active_user(username: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM panel_users WHERE username = ? AND is_active = TRUE",
            (username,),
        ).fetchone()


def _touch_last_login(username: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE panel_users SET last_login = ? WHERE username = ?",
            (db_now(), username),
        )


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.post("/auth/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    client_ip = _get_client_ip(request)
    if not _check_login_rate(client_ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many login attempts. Please try again later."},
            status_code=429,
        )

    row = await run_blocking(_get_active_user, username)

    if not row or not verify_password(password, row["password_hash"]):
        _record_login_attempt(client_ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password"},
            status_code=401,
        )

    await run_blocking(_touch_last_login, username)
    await run_blocking(log_audit, username, "login")

    token = create_token(username)
    response = RedirectResponse(url="/panel/", status_code=303)
    response.set_cookie(
        key="panel_token",
        value=token,
        httponly=True,
        samesite="strict",
        secure=PANEL_COOKIE_SECURE,
        max_age=PANEL_TOKEN_EXPIRE_HOURS * 3600,
    )
    return response


@router.post("/auth/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("panel_token")
    return response
