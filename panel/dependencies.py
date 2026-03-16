import asyncio
import os

from fastapi import Request

from database import get_db
from panel.auth import decode_token


class NotAuthenticatedException(Exception):
    """Raised when a request lacks valid authentication."""


_RUN_BLOCKING_TIMEOUT: float = 30.0


async def run_blocking(func, *args, **kwargs):
    """Run blocking I/O in a worker thread with a timeout."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return func(*args, **kwargs)
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=_RUN_BLOCKING_TIMEOUT,
    )


def _fetch_active_panel_user(username: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM panel_users WHERE username = ? AND is_active = TRUE",
            (username,),
        ).fetchone()
        if row:
            return dict(row)
    return None


async def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("panel_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    username = payload.get("sub")
    if not username:
        return None
    return await run_blocking(_fetch_active_panel_user, username)


async def require_auth(request: Request) -> dict:
    user = await get_current_user(request)
    if not user:
        raise NotAuthenticatedException()
    return user
