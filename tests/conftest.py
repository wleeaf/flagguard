import os

os.environ.setdefault("TELEGRAM_TOKEN", "fake:token")
os.environ.setdefault("GEMINI_API_KEY", "fake-key")
os.environ.setdefault("PANEL_SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://aishield:testpass@localhost:5432/aishield_test",
))

import pytest
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


TEST_DATABASE_URL = os.environ["DATABASE_URL"]

# All application tables in dependency-safe truncation order
_ALL_TABLES = [
    "broadcast_recipients",
    "broadcast_jobs",
    "reports",
    "panel_audit_log",
    "panel_users",
    "conversation_history",
    "bot_state",
    "known_users",
    "conversation_logs",
    "competition_state",
    "competition_winners",
    "competition_winner_log",
    "competition_round_winners",
    "competition_rounds",
    "flag_finders",
    "ai_global_rpm",
    "user_requests",
    "user_behavior",
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True, scope="session")
def _create_test_schema():
    """Create all tables once per test session."""
    conn = psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row)
    from database import _SCHEMA, _split_sql_script
    for stmt in _split_sql_script(_SCHEMA):
        conn.execute(stmt)
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _isolate_test():
    """Truncate all tables between tests for isolation."""
    yield
    conn = psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row)
    tables = ", ".join(_ALL_TABLES)
    conn.execute(f"TRUNCATE {tables} CASCADE")
    conn.commit()
    conn.close()


@pytest.fixture
def pg_get_db(monkeypatch):
    """Fixture that patches get_db everywhere to use the test PostgreSQL database."""
    from database import DBConnection

    @contextmanager
    def patched_get_db():
        conn = psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row)
        wrapped = DBConnection(conn)
        try:
            yield wrapped
            conn.commit()
        finally:
            conn.close()

    # Patch all known modules that import get_db
    targets = [
        "database.get_db",
        "panel.queries.get_db",
        "panel.dependencies.get_db",
        "panel.routes.auth_routes.get_db",
        "models.report.get_db",
        "models.user.get_db",
        "models.conversation.get_db",
        "models.competition.get_db",
        "models.bot_state.get_db",
    ]
    for target in targets:
        monkeypatch.setattr(target, patched_get_db)

    return patched_get_db
