"""Security-focused tests for the panel: brute-force, headers, cookies, JWT."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from httpx import AsyncClient, ASGITransport

from database import init_database, get_db
from panel.auth import hash_password, create_token
from panel.app import app
from panel.routes import auth_routes


@pytest.fixture(autouse=True)
def _setup_db(pg_get_db):
    init_database()

    with pg_get_db() as conn:
        conn.execute(
            "INSERT INTO panel_users (username, password_hash, is_active) VALUES (?, ?, TRUE)",
            ("admin", hash_password("correctpass")),
        )

    # Reset brute-force state between tests
    auth_routes._login_attempts.clear()
    auth_routes._lockouts.clear()

    yield


@pytest.mark.anyio
async def test_brute_force_lockout():
    """After 5 failed logins, the 6th should return 429."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(5):
            resp = await client.post(
                "/auth/login",
                data={"username": "admin", "password": "wrongpass"},
            )
            assert resp.status_code == 401

        # 6th attempt should be rate-limited
        resp = await client.post(
            "/auth/login",
            data={"username": "admin", "password": "wrongpass"},
        )
        assert resp.status_code == 429


@pytest.mark.anyio
async def test_security_headers_present():
    """Security headers should be set on every response."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/login")
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert "strict-origin" in resp.headers.get("Referrer-Policy", "")


@pytest.mark.anyio
async def test_cookie_flags_on_login():
    """Successful login should set httponly and samesite cookie flags."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/auth/login",
            data={"username": "admin", "password": "correctpass"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        cookie_header = resp.headers.get("set-cookie", "")
        assert "httponly" in cookie_header.lower()
        assert "samesite=strict" in cookie_header.lower()
        assert "secure" in cookie_header.lower()


@pytest.mark.anyio
async def test_lockout_lasts_full_window(monkeypatch):
    """Lockout should remain active for the full configured lockout duration."""
    base = 1_000_000.0
    monkeypatch.setattr(auth_routes.time, "time", lambda: base)
    auth_routes._login_attempts.clear()
    auth_routes._lockouts.clear()

    for _ in range(5):
        auth_routes._record_login_attempt("127.0.0.1")

    # After 5 minutes (window), still locked because lockout is 15 minutes.
    monkeypatch.setattr(auth_routes.time, "time", lambda: base + 301.0)
    assert auth_routes._check_login_rate("127.0.0.1") is False

    # After lockout expires, requests should be allowed again.
    monkeypatch.setattr(auth_routes.time, "time", lambda: base + 901.0)
    assert auth_routes._check_login_rate("127.0.0.1") is True


@pytest.mark.anyio
async def test_expired_jwt_redirects_to_login():
    """An expired JWT should redirect to login."""
    import jwt
    from datetime import datetime, timezone, timedelta

    expired_payload = {
        "sub": "admin",
        "exp": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    expired_token = jwt.encode(expired_payload, "test-secret", algorithm="HS256")

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"panel_token": expired_token},
    ) as client:
        resp = await client.get("/panel/", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]


@pytest.mark.anyio
async def test_invalid_jwt_redirects_to_login():
    """A garbage JWT should redirect to login."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"panel_token": "not.a.valid.jwt"},
    ) as client:
        resp = await client.get("/panel/", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]


@pytest.mark.anyio
async def test_inactive_user_redirects_to_login(pg_get_db):
    """An inactive user's valid JWT should still redirect to login."""
    # Deactivate the admin user
    with pg_get_db() as conn:
        conn.execute("UPDATE panel_users SET is_active = FALSE WHERE username = 'admin'")

    token = create_token("admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"panel_token": token},
    ) as client:
        resp = await client.get("/panel/", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]


@pytest.mark.anyio
async def test_csrf_required_for_panel_post():
    """State-changing panel endpoints should reject missing CSRF tokens."""
    token = create_token("admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"panel_token": token, "panel_csrf": "known-token"},
    ) as client:
        resp = await client.post("/panel/controls/toggle/maintenance")
        assert resp.status_code == 403
