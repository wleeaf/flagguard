import asyncio
import base64
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse
from metrics import render_prometheus

from config import (
    METRICS_EXPORT_DIR,
    METRICS_EXPORT_INTERVAL_SECONDS,
    METRICS_EXPORT_MAX_AGE_SECONDS,
    PANEL_COOKIE_SECURE,
    PANEL_TOKEN_EXPIRE_HOURS,
    validate_panel_config,
)
from database import init_database
from panel.dependencies import NotAuthenticatedException
from panel.routes.auth_routes import router as auth_router
from panel.routes.dashboard import router as dashboard_router
from panel.routes.users import router as users_router
from panel.routes.logs import router as logs_router
from panel.routes.security import router as security_router
from panel.routes.controls import router as controls_router
from panel.routes.competition import router as competition_router
from panel.routes.leaderboard import router as leaderboard_router
from panel.routes.comms import router as comms_router
from panel.routes.audit import router as audit_router
from panel.routes.reports import router as reports_router
from panel.routes.admins import router as admins_router
from panel.routes.panel_users import router as panel_users_router
from panel.routes.moderation import router as moderation_router
from panel.routes.events import router as events_router
from panel.routes.prompts import router as prompts_router
from panel.routes.flags import router as flags_router

PANEL_DIR = Path(__file__).parent


_CSRF_EXEMPT_POST_PATHS = {"/auth/login"}


def _is_protected_csrf_path(path: str) -> bool:
    return path.startswith("/panel/") or path == "/auth/logout"


def _receive_with_body(body: bytes):
    sent = False

    async def _receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return _receive


async def _csrf_from_form_body(request: Request, body: bytes) -> str | None:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        payload = body.decode("utf-8", errors="ignore")
        values = parse_qs(payload, keep_blank_values=True).get("csrf_token")
        if values:
            return values[0]
        return None

    if "multipart/form-data" in content_type:
        probe_request = Request(request.scope, receive=_receive_with_body(body))
        form = await probe_request.form()
        value = form.get("csrf_token")
        return str(value) if value is not None else None

    return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_panel_config()
    init_database()
    import notifications
    await notifications.start_broadcast_worker("panel")
    from metrics import start_metrics_exporter
    await start_metrics_exporter(
        role="panel",
        export_dir=METRICS_EXPORT_DIR,
        interval_seconds=METRICS_EXPORT_INTERVAL_SECONDS,
    )

    yield

    await notifications.close_client()
    from metrics import stop_metrics_exporter
    await stop_metrics_exporter()


from config import BOT_NAME as _BOT_NAME
app = FastAPI(title=f"{_BOT_NAME} Panel", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=PANEL_DIR / "static"), name="static")


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    csrf_cookie = request.cookies.get("panel_csrf")
    is_unsafe = request.method in {"POST", "PUT", "PATCH", "DELETE"}
    is_protected = _is_protected_csrf_path(request.url.path)

    if (
        is_unsafe
        and is_protected
        and request.url.path not in _CSRF_EXEMPT_POST_PATHS
    ):
        submitted = request.headers.get("X-CSRF-Token")
        if not submitted and request.method == "POST":
            body = await request.body()
            submitted = await _csrf_from_form_body(request, body)
            request = Request(request.scope, receive=_receive_with_body(body))

        if (
            not csrf_cookie
            or not submitted
            or not secrets.compare_digest(str(csrf_cookie), str(submitted))
        ):
            return PlainTextResponse("CSRF validation failed", status_code=403)

    response = await call_next(request)
    if not csrf_cookie:
        response.set_cookie(
            key="panel_csrf",
            value=secrets.token_urlsafe(32),
            httponly=False,
            samesite="strict",
            secure=PANEL_COOKIE_SECURE,
            max_age=PANEL_TOKEN_EXPIRE_HOURS * 3600,
        )
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    # Generate a per-request nonce for inline scripts
    nonce = base64.b64encode(secrets.token_bytes(16)).decode()
    request.state.csp_nonce = nonce

    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://cdn.tailwindcss.com https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'"
    )
    return response


@app.exception_handler(NotAuthenticatedException)
async def _redirect_to_login(_request: Request, _exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=303)


@app.get("/health")
async def health():
    try:
        from database import get_db

        def _ping():
            with get_db() as conn:
                conn.execute("SELECT 1")

        await asyncio.to_thread(_ping)
        return {"status": "ok"}
    except Exception as e:
        import logging
        logging.error("Health check DB ping failed: %s", e)
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "degraded", "db": "connection_error"}, status_code=503)


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(
        render_prometheus(
            export_dir=METRICS_EXPORT_DIR,
            role="panel",
            max_age_seconds=METRICS_EXPORT_MAX_AGE_SECONDS,
        ),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/")
async def root():
    return RedirectResponse(url="/panel/", status_code=303)


app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(users_router)
app.include_router(logs_router)
app.include_router(security_router)
app.include_router(controls_router)
app.include_router(competition_router)
app.include_router(leaderboard_router)
app.include_router(comms_router)
app.include_router(audit_router)
app.include_router(reports_router)
app.include_router(admins_router)
app.include_router(panel_users_router)
app.include_router(moderation_router)
app.include_router(events_router)
app.include_router(prompts_router)
app.include_router(flags_router)
