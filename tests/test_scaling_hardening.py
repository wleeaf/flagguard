"""Regression tests for scalability hardening changes."""

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "fake:token")
os.environ.setdefault("PANEL_SECRET_KEY", "test-secret")

from database import init_database
from models.user import UserRepository
import notifications


@pytest.fixture(autouse=True)
def _setup_db(pg_get_db):
    init_database()
    yield


def test_upsert_known_throttles_unchanged_user_writes():
    repo = UserRepository()
    with patch("models.user.time.monotonic", side_effect=[0.0, 10.0, 70.0]):
        with patch(
            "models.user.db_now",
            side_effect=[
                "2026-01-01 00:00:00",
                "2026-01-01 00:01:10",
            ],
        ) as now_mock:
            repo.upsert_known("u1", "Alice", "alice")
            repo.upsert_known("u1", "Alice", "alice")
            repo.upsert_known("u1", "Alice", "alice")

    assert now_mock.call_count == 2
    import database
    with database.get_db() as conn:
        row = conn.execute("SELECT * FROM known_users WHERE user_id = ?", ("u1",)).fetchone()
        assert row is not None
        assert row["first_name"] == "Alice"
        assert row["username"] == "alice"
        assert row["last_seen"] == "2026-01-01 00:01:10"


def test_upsert_known_preserves_username_when_incoming_is_empty_or_anonymous():
    repo = UserRepository()

    repo.upsert_known("u1", "Alice", "alice")
    repo.upsert_known("u1", "Alice", "")
    repo.upsert_known("u1", "Alice", "anonymous")

    import database
    with database.get_db() as conn:
        row = conn.execute("SELECT * FROM known_users WHERE user_id = ?", ("u1",)).fetchone()
        assert row is not None
        assert row["username"] == "alice"


@pytest.mark.anyio
async def test_background_broadcast_job_lifecycle(monkeypatch):
    async def fake_send_message(chat_id, text, parse_mode=None):
        await asyncio.sleep(0)
        return {"ok": True}

    monkeypatch.setattr(notifications, "send_message", fake_send_message)

    await notifications.start_broadcast_worker("test")
    try:
        job_id = await notifications.start_background_broadcast(
            ["1", "2", "3"],
            "hello",
            initiated_by="test",
        )
        assert job_id

        for _ in range(100):
            job = notifications.get_broadcast_job(job_id)
            assert job is not None
            if job["status"] == "done":
                break
            await asyncio.sleep(0.05)

        job = notifications.get_broadcast_job(job_id)
        assert job is not None
        assert job["status"] == "done"
        assert job["success"] == 3
        assert job["failed"] == 0
    finally:
        await notifications.stop_broadcast_worker()
