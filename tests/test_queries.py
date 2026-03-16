"""Tests for panel query helpers: LIKE escaping, CSV export limit, URL encoding."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from database import init_database, get_db
from models.conversation import ConversationRepository
from panel.queries import (
    _escape_like,
    get_all_logs_for_export,
    get_logs_paginated,
    get_reports_paginated,
    get_users_paginated,
)


@pytest.fixture(autouse=True)
def _setup_db(pg_get_db):
    init_database()

    # Seed some conversation logs
    with pg_get_db() as conn:
        for i in range(20):
            conn.execute(
                "INSERT INTO conversation_logs (user_id, first_name, username, user_msg, ai_msg, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("user1", "Test", "test", f"msg_{i}", f"reply_{i}", f"2026-01-01 00:{i:02d}:00"),
            )
        conn.execute(
            "INSERT INTO known_users (user_id, first_name, username, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            ("UserABC", "Alice", "Alice_Wonder", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
        )
        conn.execute(
            "INSERT INTO reports (user_id, first_name, username, message, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("UserABC", "Alice", "Alice_Wonder", "NeedS ReView", "open", "2026-01-01 00:00:00"),
        )

    yield


def test_escape_like_escapes_percent():
    assert _escape_like("100%") == "100\\%"


def test_escape_like_escapes_underscore():
    assert _escape_like("user_name") == "user\\_name"


def test_escape_like_escapes_backslash():
    assert _escape_like("path\\file") == "path\\\\file"


def test_escape_like_combined():
    assert _escape_like("50%_off\\deal") == "50\\%\\_off\\\\deal"


def test_csv_export_respects_row_limit():
    rows = get_all_logs_for_export(limit=5)
    assert len(rows) == 5


def test_csv_export_default_returns_all():
    rows = get_all_logs_for_export()
    assert len(rows) == 20


def test_user_search_case_insensitive():
    data = get_users_paginated(search="alice")
    assert any(u["user_id"] == "UserABC" for u in data["users"])


def test_logs_search_case_insensitive():
    data = get_logs_paginated(search="MSG_1", per_page=25, include_total=True)
    assert len(data["logs"]) > 0


def test_reports_search_case_insensitive():
    data = get_reports_paginated(search="review")
    assert any(r["user_id"] == "UserABC" for r in data["reports"])


def test_conversation_history_returns_latest_window():
    repo = ConversationRepository()
    user_id = "history-user"
    for i in range(40):
        repo.add_turn(user_id, "user", f"turn-{i}")
    history = repo.get_history(user_id)
    assert len(history) == repo.MAX_HISTORY * 2
    assert history[0]["content"] == "turn-20"
    assert history[-1]["content"] == "turn-39"


def test_url_encoding_in_comms_redirect():
    """The _redirect helper should use proper URL encoding."""
    import urllib.parse
    from panel.routes.comms import _redirect
    from fastapi.responses import RedirectResponse

    response = _redirect("Hello world & stuff", "error")
    location = response.headers["location"]
    # The message should be properly URL-encoded, not use manual + signs
    assert "Hello+world" in location or "Hello%20world" in location
    assert "msg_type=error" in location
