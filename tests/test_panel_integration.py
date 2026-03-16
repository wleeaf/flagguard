"""Integration tests for all panel pages (Phases 1-7)."""

import os
import sys

# Ensure project root is on path before imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from httpx import AsyncClient, ASGITransport

from database import init_database, get_db
from panel.auth import hash_password, create_token
from panel.app import app


@pytest.fixture(autouse=True)
def _setup_db(pg_get_db):
    """Use the test PostgreSQL database for every test."""
    init_database()

    # Seed an admin user
    with pg_get_db() as conn:
        conn.execute(
            "INSERT INTO panel_users (username, password_hash) VALUES (?, ?)",
            ("admin", hash_password("pass123")),
        )
        # Seed some test data
        conn.execute(
            "INSERT INTO known_users (user_id, first_name, username) VALUES (?, ?, ?)",
            ("12345", "TestUser", "testuser"),
        )
        conn.execute(
            "INSERT INTO user_behavior (user_id, total_requests, jailbreak_attempts, suspicious_score) "
            "VALUES (?, ?, ?, ?)",
            ("12345", 50, 3, 25),
        )
        conn.execute(
            "INSERT INTO conversation_logs (user_id, first_name, username, user_msg, ai_msg) "
            "VALUES (?, ?, ?, ?, ?)",
            ("12345", "TestUser", "testuser", "hello", "hi there"),
        )
        conn.execute(
            "INSERT INTO reports (user_id, first_name, username, message) VALUES (?, ?, ?, ?)",
            ("12345", "TestUser", "testuser", "Needs review"),
        )

    yield


def _auth_cookies() -> dict[str, str]:
    token = create_token("admin")
    return {
        "panel_token": token,
        "panel_csrf": "test-csrf-token",
    }


@pytest.fixture
def auth_cookies():
    return _auth_cookies()


@pytest.mark.anyio
async def test_login_page():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert "Sign in" in resp.text or "Login" in resp.text


@pytest.mark.anyio
async def test_unauthenticated_redirect():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/panel/", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]


@pytest.mark.anyio
async def test_dashboard(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text


@pytest.mark.anyio
async def test_users_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/users")
        assert resp.status_code == 200
        assert "Users" in resp.text
        assert "TestUser" in resp.text


@pytest.mark.anyio
async def test_users_htmx_partial(auth_cookies):
    """HTMX partial request should return just the table, not the full page."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get(
            "/panel/users?search=test",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Partial should NOT have the full base layout
        assert "<nav" not in resp.text
        assert "TestUser" in resp.text


@pytest.mark.anyio
async def test_users_boosted_full_page(auth_cookies):
    """HX-Boosted navigation should return the full page."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get(
            "/panel/users",
            headers={"HX-Request": "true", "HX-Boosted": "true"},
        )
        assert resp.status_code == 200
        assert "<nav" in resp.text


@pytest.mark.anyio
async def test_user_detail(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/users/12345")
        assert resp.status_code == 200
        assert "TestUser" in resp.text


@pytest.mark.anyio
async def test_logs_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/logs")
        assert resp.status_code == 200
        assert "Logs" in resp.text


@pytest.mark.anyio
async def test_security_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/security")
        assert resp.status_code == 200
        assert "Security" in resp.text


@pytest.mark.anyio
async def test_controls_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/controls")
        assert resp.status_code == 200
        assert "Maintenance" in resp.text
        assert "Backup" in resp.text


@pytest.mark.anyio
async def test_controls_backup_returns_pg_message(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.post(
            "/panel/controls/backup",
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 200
        assert "PostgreSQL" in resp.text


@pytest.mark.anyio
async def test_competition_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/competition")
        assert resp.status_code == 200
        assert "Competition" in resp.text


@pytest.mark.anyio
async def test_leaderboard_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/leaderboard")
        assert resp.status_code == 200
        assert "Leaderboard" in resp.text


@pytest.mark.anyio
async def test_comms_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/comms")
        assert resp.status_code == 200
        assert "Direct Message" in resp.text
        assert "Broadcast" in resp.text


@pytest.mark.anyio
async def test_audit_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/audit")
        assert resp.status_code == 200
        assert "Audit" in resp.text


@pytest.mark.anyio
async def test_audit_htmx_partial(auth_cookies):
    """HTMX partial request to audit should return just the table."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get(
            "/panel/audit?page=1",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "<nav" not in resp.text


@pytest.mark.anyio
async def test_controls_toggle(auth_cookies):
    """Toggle endpoint should return HTML fragment."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.post(
            "/panel/controls/toggle/maintenance",
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 200
        assert "toggle-maintenance" in resp.text


@pytest.mark.anyio
async def test_controls_toggle_winner_broadcast(auth_cookies):
    """Winner broadcast toggle endpoint should return HTML fragment."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.post(
            "/panel/controls/toggle/winner_broadcast",
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 200
        assert "toggle-winner_broadcast" in resp.text


@pytest.mark.anyio
async def test_controls_set_ai_difficulty(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.post(
            "/panel/controls/difficulty",
            data={"active": "EASY"},
            headers={"X-CSRF-Token": "test-csrf-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/panel/controls" in resp.headers["location"]

    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key = 'ai_difficulty_level'"
        ).fetchone()
        assert row is not None
        assert row["value"] == "EASY"


@pytest.mark.anyio
async def test_competition_setup(auth_cookies):
    """Setting up a competition should redirect back."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.post(
            "/panel/competition/setup",
            data={"answer": "testanswer", "question": "a test question"},
            headers={"X-CSRF-Token": "test-csrf-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/panel/competition" in resp.headers["location"]


@pytest.mark.anyio
async def test_form_csrf_token_does_not_break_form_fields(auth_cookies):
    """Posting csrf_token in form body should preserve other Form(...) fields."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.post(
            "/panel/comms/broadcast",
            data={"message": "hello users", "csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/panel/comms" in resp.headers["location"]


@pytest.mark.anyio
async def test_competition_end(auth_cookies):
    """Ending a competition should redirect back."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.post(
            "/panel/competition/end",
            headers={"X-CSRF-Token": "test-csrf-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/panel/competition" in resp.headers["location"]


@pytest.mark.anyio
async def test_logs_export(auth_cookies):
    """CSV export should return a downloadable file."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/logs/export")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")


@pytest.mark.anyio
async def test_reports_page(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resp = await client.get("/panel/reports")
        assert resp.status_code == 200
        assert "Reports" in resp.text
        assert "Needs review" in resp.text
        assert "Resolve" in resp.text


@pytest.mark.anyio
async def test_reports_reopen_flow(auth_cookies):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=auth_cookies
    ) as client:
        resolve_resp = await client.post(
            "/panel/reports/1/resolve",
            data={"search": "", "status": "", "page": "1"},
            headers={"HX-Request": "true", "X-CSRF-Token": "test-csrf-token"},
        )
        assert resolve_resp.status_code == 200
        assert "Reopen" in resolve_resp.text
        assert "Resolved" in resolve_resp.text

        reopen_resp = await client.post(
            "/panel/reports/1/reopen",
            data={"search": "", "status": "", "page": "1"},
            headers={"HX-Request": "true", "X-CSRF-Token": "test-csrf-token"},
        )
        assert reopen_resp.status_code == 200
        assert "Resolve" in reopen_resp.text
        assert "Open" in reopen_resp.text
