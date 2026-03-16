import atexit
import logging
import threading
import time
from contextlib import contextmanager

from config import (
    DATABASE_POOL_MAX_SIZE,
    DATABASE_POOL_MIN_SIZE,
    DATABASE_URL,
)
from metrics import inc_counter, observe_histogram, set_gauge

_pg_pool = None
_pg_pool_lock = threading.Lock()
_SLOW_QUERY_THRESHOLD = 1.0  # seconds


def _convert_qmark_sql(sql: str) -> str:
    """Convert qmark (?) placeholders to PostgreSQL %s placeholders.

    This is a deliberate design decision: the entire codebase uses SQLite-style
    ``?`` placeholders for portability. All queries flow through :class:`DBConnection`
    which calls this function transparently, so callers never need to worry about
    the underlying driver's paramstyle.  Do **not** bypass ``DBConnection`` with
    raw ``conn._raw.execute()`` — the query will fail because PostgreSQL expects
    ``%s``, not ``?``.
    """
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]

        if ch == "'" and not in_double:
            out.append(ch)
            if in_single and i + 1 < len(sql) and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
            i += 1
            continue

        if ch == "?" and not in_single and not in_double:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    chunk: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(script):
        ch = script[i]
        if ch == "'" and not in_double:
            chunk.append(ch)
            if in_single and i + 1 < len(script) and script[i + 1] == "'":
                chunk.append("'")
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            chunk.append(ch)
            i += 1
            continue
        if ch == ";" and not in_single and not in_double:
            stmt = "".join(chunk).strip()
            if stmt:
                statements.append(stmt)
            chunk = []
            i += 1
            continue
        chunk.append(ch)
        i += 1
    tail = "".join(chunk).strip()
    if tail:
        statements.append(tail)
    return statements


class DBConnection:
    """Compatibility wrapper that converts qmark SQL to PostgreSQL format."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        sql = _convert_qmark_sql(sql)
        return self._raw.execute(sql, params)

    def executemany(self, sql: str, param_seq):
        sql = _convert_qmark_sql(sql)
        with self._raw.cursor() as cur:
            cur.executemany(sql, param_seq)
            return cur

    def executescript(self, script: str):
        cur = None
        for stmt in _split_sql_script(script):
            cur = self._raw.execute(stmt)
        return cur

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def close(self):
        return self._raw.close()

    def __getattr__(self, item):
        return getattr(self._raw, item)


def _get_postgres_pool():
    global _pg_pool
    if _pg_pool is not None and not getattr(_pg_pool, "closed", False):
        return _pg_pool

    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except Exception as e:
        raise RuntimeError(
            "PostgreSQL dependencies are missing. "
            "Install requirements.txt first."
        ) from e

    with _pg_pool_lock:
        if _pg_pool is not None and not getattr(_pg_pool, "closed", False):
            return _pg_pool

        _pg_pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=DATABASE_POOL_MIN_SIZE,
            max_size=DATABASE_POOL_MAX_SIZE,
            kwargs={"row_factory": dict_row},
            timeout=30,
        )
        try:
            _pg_pool.wait()
        except Exception:
            try:
                _pg_pool.close()
            except Exception:
                pass
            _pg_pool = None
            raise
        return _pg_pool


def _get_connection():
    global _pg_pool
    last_err = None
    for _ in range(2):
        pool = _get_postgres_pool()
        try:
            raw = pool.getconn()
            return DBConnection(raw)
        except Exception as e:
            last_err = e
            with _pg_pool_lock:
                if _pg_pool is pool:
                    try:
                        _pg_pool.close()
                    except Exception:
                        pass
                    _pg_pool = None
    raise ConnectionError(
        "Could not acquire a database connection after retrying. "
        "The connection pool may be exhausted — check DATABASE_POOL_MAX_SIZE, "
        "active worker count, and PostgreSQL max_connections."
    ) from last_err


def _put_connection(conn: DBConnection):
    try:
        pool = _get_postgres_pool()
        pool.putconn(conn._raw)
    except Exception:
        try:
            conn._raw.close()
        except Exception:
            pass


def close_connection():
    global _pg_pool
    with _pg_pool_lock:
        pool = _pg_pool
        _pg_pool = None
    if pool is not None:
        try:
            pool.close()
        except Exception:
            pass


atexit.register(close_connection)


def _report_pool_gauges() -> None:
    """Emit pool utilization metrics (best-effort, no-op if pool unavailable)."""
    pool = _pg_pool
    if pool is None or getattr(pool, "closed", False):
        return
    try:
        stats = pool.get_stats()
        set_gauge("db_pool_size", float(stats.get("pool_size", 0)))
        set_gauge("db_pool_available", float(stats.get("pool_available", 0)))
    except Exception:
        pass


@contextmanager
def get_db():
    """Context manager for DB transactions with contention-aware commit retries."""
    conn = _get_connection()
    start = time.monotonic()
    inc_counter("db_transactions_started_total")
    _report_pool_gauges()
    try:
        yield conn
        last_err = None
        for attempt in range(3):
            try:
                conn.commit()
                last_err = None
                inc_counter("db_transactions_committed_total")
                break
            except Exception as e:
                last_err = e
                err_text = str(e).lower()
                is_locked = (
                    "locked" in err_text
                    or "deadlock" in err_text
                    or "could not serialize" in err_text
                )
                if is_locked and attempt < 2:
                    inc_counter("db_locked_commit_retries_total")
                    time.sleep(0.05 * (2 ** attempt))
                else:
                    raise
        if last_err:
            raise last_err
    except BaseException:
        conn.rollback()
        inc_counter("db_transactions_rolled_back_total")
        raise
    finally:
        elapsed = time.monotonic() - start
        observe_histogram("db_transaction_seconds", elapsed)
        if elapsed > _SLOW_QUERY_THRESHOLD:
            logging.warning("Slow DB transaction: %.2fs", elapsed)
        _put_connection(conn)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_behavior (
    user_id             TEXT PRIMARY KEY,
    jailbreak_attempts  INTEGER DEFAULT 0,
    suspicious_score    INTEGER DEFAULT 0,
    honeypot_caught     INTEGER DEFAULT 0,
    total_requests      INTEGER DEFAULT 0,
    last_reset          TEXT
);

CREATE TABLE IF NOT EXISTS user_requests (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id     TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_requests_user_ts
    ON user_requests(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_user_requests_ts
    ON user_requests(timestamp);

CREATE TABLE IF NOT EXISTS ai_global_rpm (
    key        TEXT NOT NULL,
    bucket     BIGINT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (key, bucket)
);
CREATE INDEX IF NOT EXISTS idx_ai_global_rpm_updated_at
    ON ai_global_rpm(updated_at);

CREATE TABLE IF NOT EXISTS competition_winners (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id     TEXT NOT NULL UNIQUE,
    won_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_comp_winners_won_at
    ON competition_winners(won_at);

CREATE TABLE IF NOT EXISTS competition_winner_log (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id     TEXT NOT NULL,
    first_name  TEXT,
    username    TEXT,
    won_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comp_winner_log_user
    ON competition_winner_log(user_id);
CREATE INDEX IF NOT EXISTS idx_comp_winner_log_won_at
    ON competition_winner_log(won_at);

CREATE TABLE IF NOT EXISTS flag_finders (
    user_id     TEXT PRIMARY KEY,
    first_name  TEXT,
    username    TEXT,
    found_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flag_finders_found_at
    ON flag_finders(found_at);

CREATE TABLE IF NOT EXISTS competition_rounds (
    id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    question      TEXT,
    reward_message TEXT,
    max_winners   INTEGER NOT NULL DEFAULT 0,
    initiated_by  TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    end_reason    TEXT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_comp_rounds_status_started
    ON competition_rounds(status, started_at);
CREATE INDEX IF NOT EXISTS idx_comp_rounds_started
    ON competition_rounds(started_at);

CREATE TABLE IF NOT EXISTS competition_round_winners (
    id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    round_id      BIGINT NOT NULL REFERENCES competition_rounds(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    first_name    TEXT,
    username      TEXT,
    won_at        TEXT NOT NULL,
    winner_rank   INTEGER,
    UNIQUE(round_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_comp_round_winners_round_rank
    ON competition_round_winners(round_id, winner_rank, won_at);
CREATE INDEX IF NOT EXISTS idx_comp_round_winners_user
    ON competition_round_winners(user_id);

CREATE TABLE IF NOT EXISTS competition_state (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS conversation_logs (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id     TEXT NOT NULL,
    first_name  TEXT,
    username    TEXT,
    user_msg    TEXT,
    ai_msg      TEXT,
    timestamp   TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_logs_user
    ON conversation_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_conv_logs_ts
    ON conversation_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_conv_logs_user_ts
    ON conversation_logs(user_id, timestamp);

CREATE TABLE IF NOT EXISTS known_users (
    user_id     TEXT PRIMARY KEY,
    first_name  TEXT,
    username    TEXT,
    first_seen  TEXT,
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS bot_state (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_history_user
    ON conversation_history(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_conv_history_user_id
    ON conversation_history(user_id, id);

CREATE TABLE IF NOT EXISTS panel_users (
    id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    telegram_id   TEXT,
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TEXT,
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS panel_audit_log (
    id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username   TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    timestamp  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts
    ON panel_audit_log(timestamp);

CREATE TABLE IF NOT EXISTS reports (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id     TEXT NOT NULL,
    first_name  TEXT,
    username    TEXT,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    resolved_by TEXT,
    created_at  TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_user   ON reports(user_id);

CREATE TABLE IF NOT EXISTS broadcast_jobs (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    initiated_by TEXT,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    total       INTEGER NOT NULL DEFAULT 0,
    success     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT,
    started_at  TEXT,
    finished_at TEXT,
    worker_id   TEXT,
    lease_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_status_created
    ON broadcast_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS broadcast_recipients (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    job_id      BIGINT NOT NULL REFERENCES broadcast_jobs(id) ON DELETE CASCADE,
    chat_id     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    sent_at     TEXT,
    updated_at  TEXT,
    UNIQUE(job_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_job_status
    ON broadcast_recipients(job_id, status);

CREATE TABLE IF NOT EXISTS message_log (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id     TEXT NOT NULL,
    first_name  TEXT,
    username    TEXT,
    message_text TEXT,
    timestamp   TEXT
);
CREATE INDEX IF NOT EXISTS idx_message_log_user
    ON message_log(user_id);
CREATE INDEX IF NOT EXISTS idx_message_log_ts
    ON message_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_message_log_user_ts
    ON message_log(user_id, timestamp);

CREATE TABLE IF NOT EXISTS telegram_admins (
    telegram_id TEXT UNIQUE NOT NULL,
    label       TEXT,
    added_at    TEXT,
    silent      BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS banned_users (
    user_id    TEXT PRIMARY KEY,
    banned_by  TEXT,
    banned_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timed_out_users (
    user_id       TEXT PRIMARY KEY,
    timeout_until TEXT NOT NULL,
    timed_out_by  TEXT,
    timed_out_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timed_out_users_until
    ON timed_out_users(timeout_until);

CREATE TABLE IF NOT EXISTS admin_messages (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    msg_type    TEXT NOT NULL,
    target      TEXT,
    message     TEXT NOT NULL,
    sent_by     TEXT,
    timestamp   TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_messages_type
    ON admin_messages(msg_type);
CREATE INDEX IF NOT EXISTS idx_admin_messages_ts
    ON admin_messages(timestamp);

CREATE TABLE IF NOT EXISTS bot_events (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    event_type  TEXT NOT NULL,
    user_id     TEXT,
    first_name  TEXT,
    username    TEXT,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_events_type_created
    ON bot_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_bot_events_created
    ON bot_events(created_at);

CREATE TABLE IF NOT EXISTS ctf_flags (
    id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    position        INTEGER NOT NULL UNIQUE,
    flag_value      TEXT NOT NULL,
    success_message TEXT NOT NULL DEFAULT '',
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ctf_flags_position ON ctf_flags(position);

CREATE TABLE IF NOT EXISTS user_flag_progress (
    user_id   TEXT NOT NULL,
    flag_id   BIGINT NOT NULL REFERENCES ctf_flags(id) ON DELETE CASCADE,
    found_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, flag_id)
);
CREATE INDEX IF NOT EXISTS idx_user_flag_progress_user ON user_flag_progress(user_id);
"""


_AUTOVACUUM_TUNING = """
DO $$
BEGIN
    -- High-churn tables: aggressive autovacuum to prevent bloat.
    EXECUTE 'ALTER TABLE user_requests SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.02)';
    EXECUTE 'ALTER TABLE ai_global_rpm SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.02)';
    EXECUTE 'ALTER TABLE message_log SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.05)';
    EXECUTE 'ALTER TABLE broadcast_recipients SET (autovacuum_vacuum_scale_factor = 0.05)';
EXCEPTION WHEN OTHERS THEN
    -- Best-effort: some hosted PostgreSQL providers restrict ALTER TABLE SET.
    NULL;
END $$;
"""


def init_database():
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript(_SCHEMA)
        # Remove deprecated mode flags that are no longer supported.
        conn.execute("DELETE FROM bot_state WHERE key = ?", ("spy_mode",))

    # Migration: add silent column to telegram_admins if missing.
    # IMPORTANT: This must run in its own transaction because a failed ALTER TABLE
    # (column already exists) puts the PostgreSQL transaction into INERROR state.
    # psycopg 3 silently rolls back INERROR transactions on commit(), which would
    # undo any new table creations from the schema above.
    try:
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE telegram_admins ADD COLUMN silent BOOLEAN DEFAULT FALSE"
            )
    except Exception:
        pass  # Column already exists

    # Migration: add tag column to ctf_flags if missing.
    try:
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE ctf_flags ADD COLUMN tag TEXT NOT NULL DEFAULT ''"
            )
    except Exception:
        pass  # Column already exists

    # Migration: add notify_on_find column to ctf_flags if missing.
    try:
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE ctf_flags ADD COLUMN notify_on_find BOOLEAN NOT NULL DEFAULT FALSE"
            )
    except Exception:
        pass  # Column already exists

    # Migration: add notify_message column to ctf_flags if missing.
    try:
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE ctf_flags ADD COLUMN notify_message TEXT NOT NULL DEFAULT ''"
            )
    except Exception:
        pass  # Column already exists

    # Seed ctf_flags with CHALLENGE_FLAG if the table is empty.
    try:
        from config import CHALLENGE_FLAG
        from time_utils import db_now

        with get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM ctf_flags"
            ).fetchone()["cnt"]
            if count == 0 and CHALLENGE_FLAG:
                conn.execute(
                    "INSERT INTO ctf_flags (position, flag_value, success_message, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (1, CHALLENGE_FLAG, "", db_now()),
                )
            # Backfill: copy existing flag_finders into user_flag_progress for position 1.
            conn.execute(
                """INSERT INTO user_flag_progress (user_id, flag_id, found_at)
                   SELECT ff.user_id, cf.id, ff.found_at
                   FROM flag_finders ff
                   CROSS JOIN ctf_flags cf
                   WHERE cf.position = 1
                     AND NOT EXISTS (
                         SELECT 1 FROM user_flag_progress ufp
                         WHERE ufp.user_id = ff.user_id AND ufp.flag_id = cf.id
                     )"""
            )
    except Exception as e:
        logging.warning("CTF flags seed/backfill skipped: %s", e)

    # Helper function: encoding-safe substr that won't crash on invalid UTF-8.
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE OR REPLACE FUNCTION safe_substr(val text, start int, len int)
                RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
                BEGIN
                    RETURN substr(val, start, len);
                EXCEPTION WHEN OTHERS THEN
                    RETURN '[encoding error]';
                END;
                $$
            """)
    except Exception:
        pass

    # Apply autovacuum tuning for high-churn tables (best-effort).
    try:
        with get_db() as conn:
            conn.execute(_AUTOVACUUM_TUNING)
    except Exception:
        pass
    print("Database initialized (PostgreSQL).")
