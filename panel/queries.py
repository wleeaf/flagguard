import os
from collections import defaultdict
from datetime import datetime

from database import get_db
from panel.pg_backup import backup_dir_path
from time_utils import db_ago, db_now, parse_db_timestamp


def _escape_like(value: str) -> str:
    """Escape special characters for LIKE patterns."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ci_like(column: str) -> str:
    """
    Case-insensitive LIKE that behaves consistently across SQLite/PostgreSQL.

    PostgreSQL LIKE is case-sensitive; SQLite LIKE is case-insensitive by default.
    We normalize with LOWER(...) on both sides.
    """
    return f"LOWER(COALESCE({column}, '')) LIKE LOWER(?) ESCAPE '\\'"




def get_requests_per_hour(hours: int = 24, daily: bool = False) -> list[dict]:
    """Aggregate request counts grouped by hour (or day) for the chart."""
    threshold = db_ago(hours=hours)
    if daily:
        group_expr = "substr(timestamp, 1, 10)"  # YYYY-MM-DD
    else:
        group_expr = "substr(timestamp, 1, 13) || ':00'"  # YYYY-MM-DD HH:00
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT {group_expr} AS hour,
                       COUNT(*) AS count
                FROM conversation_logs
                WHERE timestamp > ?
                GROUP BY hour ORDER BY hour""",
            (threshold,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_activity(limit: int = 20) -> list[dict]:
    """Most recent conversation log entries for the activity feed."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT user_id, first_name, username,
                      safe_substr(user_msg, 1, 160) AS user_msg, timestamp
               FROM conversation_logs
               ORDER BY timestamp DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def log_audit(username: str, action: str, target: str = None, detail: str = None):
    """Write an entry to the panel audit log."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO panel_audit_log (username, action, target, detail, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (username, action, target, detail, db_now()),
        )


# ── User management queries ──────────────────────────────────────────────

_USER_SORT_COLUMNS = frozenset({
    "user_id", "first_name", "username",
    "total_requests", "jailbreak_attempts", "suspicious_score", "last_seen",
})

_USER_BASE_SELECT = """\
    SELECT ku.user_id, ku.first_name, ku.username,
           ku.first_seen, ku.last_seen,
           COALESCE(ub.total_requests, 0)      AS total_requests,
           COALESCE(ub.jailbreak_attempts, 0)  AS jailbreak_attempts,
           COALESCE(ub.suspicious_score, 0)     AS suspicious_score,
           COALESCE(ub.honeypot_caught, 0)      AS honeypot_caught
    FROM known_users ku
    LEFT JOIN user_behavior ub ON ku.user_id = ub.user_id"""


def get_users_paginated(
    page: int = 1,
    per_page: int = 25,
    search: str = "",
    sort_by: str = "last_seen",
    order: str = "desc",
) -> dict:
    if sort_by not in _USER_SORT_COLUMNS:
        sort_by = "last_seen"
    if order not in ("asc", "desc"):
        order = "desc"
    offset = (page - 1) * per_page

    with get_db() as conn:
        if search:
            pattern = f"%{_escape_like(search)}%"
            where = (
                f"WHERE {_ci_like('ku.first_name')} "
                f"OR {_ci_like('ku.username')} "
                f"OR {_ci_like('ku.user_id')}"
            )
            params = (pattern, pattern, pattern)

            total = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM known_users ku {where}", params
            ).fetchone()["cnt"]

            rows = conn.execute(
                f"{_USER_BASE_SELECT} {where} ORDER BY {sort_by} {order} LIMIT ? OFFSET ?",
                (*params, per_page, offset),
            ).fetchall()
        else:
            total = conn.execute(
                "SELECT COUNT(*) AS cnt FROM known_users"
            ).fetchone()["cnt"]

            rows = conn.execute(
                f"{_USER_BASE_SELECT} ORDER BY {sort_by} {order} LIMIT ? OFFSET ?",
                (per_page, offset),
            ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "users": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


def get_user_detail(user_id: str) -> dict | None:
    with get_db() as conn:
        ku = conn.execute(
            "SELECT * FROM known_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not ku:
            return None

        ub = conn.execute(
            "SELECT * FROM user_behavior WHERE user_id = ?", (user_id,)
        ).fetchone()

        result = dict(ku)
        if ub:
            result.update(dict(ub))
        else:
            result.update({
                "jailbreak_attempts": 0, "suspicious_score": 0,
                "honeypot_caught": 0, "total_requests": 0,
            })
        return result


def get_user_conversations(user_id: str, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, user_msg, ai_msg, timestamp
               FROM conversation_logs
               WHERE user_id = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_conversation_entry(user_id: str, conversation_id: int) -> dict | None:
    """Fetch one conversation log row for user detail lazy expansion."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, user_msg, ai_msg, timestamp
               FROM conversation_logs
               WHERE id = ? AND user_id = ?""",
            (conversation_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def get_user_all_messages(user_id: str, limit: int = 100) -> list[dict]:
    """Get all messages from message_log for a user (every message they ever sent)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, message_text, timestamp
               FROM message_log
               WHERE user_id = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_interactions(user_id: str, limit: int | None = 100) -> list[dict]:
    """Get combined user messages and AI responses in chronological order for chat view."""
    if limit is not None and limit < 1:
        limit = 100

    duplicate_window_seconds = 600

    def _normalize_text(value: str) -> str:
        return " ".join((value or "").strip().split()).casefold()

    with get_db() as conn:
        conv_sql = (
            """SELECT user_msg, ai_msg, timestamp
               FROM conversation_logs
               WHERE user_id = ?
               ORDER BY timestamp DESC"""
        )
        conv_params: tuple = (user_id,)
        if limit is not None:
            conv_sql += "\n               LIMIT ?"
            conv_params = (user_id, limit)

        # Get conversation logs (user + AI exchanges)
        conv_rows = conn.execute(conv_sql, conv_params).fetchall()

        msg_sql = (
            """SELECT message_text, timestamp
               FROM message_log
               WHERE user_id = ?
               ORDER BY timestamp DESC"""
        )
        msg_params: tuple = (user_id,)
        if limit is not None:
            msg_sql += "\n               LIMIT ?"
            msg_params = (user_id, limit * 3)

        # Get all raw messages from message_log
        msg_rows = conn.execute(msg_sql, msg_params).fetchall()

        # Build a unified timeline
        entries = []
        conv_slots: dict[str, list[dict]] = defaultdict(list)

        # Add conversation log entries (these include bot responses).
        for r in conv_rows:
            d = dict(r)
            user_msg = d.get("user_msg", "")
            timestamp = d.get("timestamp", "")
            entries.append({
                "type": "conversation",
                "user_msg": user_msg,
                "ai_msg": d.get("ai_msg", ""),
                "timestamp": timestamp,
            })
            normalized = _normalize_text(user_msg)
            parsed_ts = parse_db_timestamp(timestamp)
            if normalized and parsed_ts:
                conv_slots[normalized].append({"timestamp": parsed_ts, "used": False})

        for slots in conv_slots.values():
            slots.sort(key=lambda x: x["timestamp"])

        # Add message_log entries that do not map to an existing conversation row.
        msg_dicts = [dict(r) for r in msg_rows]
        msg_dicts.sort(key=lambda x: x.get("timestamp", ""))
        for d in msg_dicts:
            text = d.get("message_text", "")
            ts = d.get("timestamp", "")
            if not text:
                continue

            suppress = False
            normalized = _normalize_text(text)
            parsed_msg_ts = parse_db_timestamp(ts)
            slots = conv_slots.get(normalized, [])
            if parsed_msg_ts and slots:
                for slot in slots:
                    if slot["used"]:
                        continue
                    # Conversation rows are usually written after message_log.
                    delta = (slot["timestamp"] - parsed_msg_ts).total_seconds()
                    if -3 <= delta <= duplicate_window_seconds:
                        slot["used"] = True
                        suppress = True
                        break

            if suppress:
                continue

            entries.append({
                "type": "message",
                "user_msg": text,
                "ai_msg": "",
                "timestamp": ts,
            })

        # Sort by timestamp ascending (oldest first for chat view)
        entries.sort(key=lambda x: x.get("timestamp", ""))
        if limit is None:
            return entries
        return entries[-limit:]


def get_user_interactions_for_export(user_id: str) -> list[dict]:
    """Get full merged interaction history for CSV export."""
    return get_user_interactions(user_id, limit=None)


def clear_user_logs(user_id: str) -> dict:
    """Delete persisted conversation and raw message logs for a user."""
    with get_db() as conn:
        conv_deleted = conn.execute(
            "DELETE FROM conversation_logs WHERE user_id = ?",
            (user_id,),
        ).rowcount or 0
        msg_deleted = conn.execute(
            "DELETE FROM message_log WHERE user_id = ?",
            (user_id,),
        ).rowcount or 0
    return {
        "conversation_logs": int(max(conv_deleted, 0)),
        "message_log": int(max(msg_deleted, 0)),
    }


def full_reset_user(user_id: str) -> dict[str, int]:
    """Delete ALL data associated with a user (except known_users identity and audit log)."""
    # Tables with user_id column to clear, in safe order.
    _tables = (
        "conversation_history",
        "conversation_logs",
        "message_log",
        "user_requests",
        "user_behavior",
        "banned_users",
        "timed_out_users",
        "flag_finders",
        "user_flag_progress",
        "competition_winners",
        "competition_round_winners",
        "competition_winner_log",
        "bot_events",
        "reports",
    )
    counts: dict[str, int] = {}
    with get_db() as conn:
        for table in _tables:
            deleted = conn.execute(
                f"DELETE FROM {table} WHERE user_id = ?", (user_id,)
            ).rowcount or 0
            counts[table] = int(max(deleted, 0))
        # admin_messages uses 'target' column for the recipient user_id.
        deleted = conn.execute(
            "DELETE FROM admin_messages WHERE target = ?", (user_id,)
        ).rowcount or 0
        counts["admin_messages"] = int(max(deleted, 0))
    return counts


# ── Conversation log queries ─────────────────────────────────────────────

def get_log_users() -> list[dict]:
    """Get distinct users from conversation logs for the filter dropdown."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT user_id,
                      MAX(first_name) AS first_name,
                      MAX(username) AS username
               FROM conversation_logs
               GROUP BY user_id
               ORDER BY COALESCE(NULLIF(MAX(first_name), ''), user_id)"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_logs_paginated(
    page: int = 1,
    per_page: int = 25,
    user_id: str = "",
    username: str = "",
    search: str = "",
    include_total: bool = False,
) -> dict:
    if per_page < 1:
        per_page = 25
    offset = (page - 1) * per_page
    conditions: list[str] = []
    params: list = []

    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    if username:
        conditions.append("username = ?")
        params.append(username)
    if search:
        conditions.append(f"({_ci_like('user_msg')} OR {_ci_like('ai_msg')})")
        pattern = f"%{_escape_like(search)}%"
        params.extend([pattern, pattern])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        total = None
        if include_total:
            total = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM conversation_logs {where}", params
            ).fetchone()["cnt"]

        rows = conn.execute(
            f"""SELECT id, user_id, first_name, username,
                       safe_substr(user_msg, 1, 300) AS user_msg,
                       safe_substr(ai_msg, 1, 300)   AS ai_msg,
                       timestamp
                FROM conversation_logs {where}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?""",
            (*params, per_page + 1, offset),
        ).fetchall()

    has_next = len(rows) > per_page
    rows = rows[:per_page]
    if include_total:
        total_pages = max(1, ((total or 0) + per_page - 1) // per_page)
    else:
        total = offset + len(rows) + (1 if has_next else 0)
        total_pages = page + (1 if has_next else 0)

    return {
        "logs": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_next": has_next,
    }


def get_log_entry(log_id: int) -> dict | None:
    """Fetch a single conversation log entry with full (untruncated) messages."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, user_id, first_name, username,
                      user_msg, ai_msg, timestamp
               FROM conversation_logs
               WHERE id = ?""",
            (log_id,),
        ).fetchone()
        if row:
            return dict(row)
        return None


def get_all_logs_for_export(user_id: str = "", limit: int = 10_000) -> list[dict]:
    with get_db() as conn:
        if user_id:
            rows = conn.execute(
                """SELECT user_id, first_name, username,
                          user_msg, ai_msg, timestamp
                   FROM conversation_logs WHERE user_id = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT user_id, first_name, username,
                          user_msg, ai_msg, timestamp
                   FROM conversation_logs ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ── Security queries ─────────────────────────────────────────────────────

def get_top_jailbreakers(limit: int = 10) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ku.user_id, ku.first_name, ku.username,
                      ub.jailbreak_attempts, ub.honeypot_caught,
                      ub.suspicious_score, ub.total_requests
               FROM user_behavior ub
               JOIN known_users ku ON ku.user_id = ub.user_id
               WHERE ub.jailbreak_attempts > 0
               ORDER BY ub.jailbreak_attempts DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_suspicion_distribution() -> dict:
    with get_db() as conn:
        row = conn.execute(
            """SELECT
                   SUM(CASE WHEN suspicious_score < 20 THEN 1 ELSE 0 END)  AS low,
                   SUM(CASE WHEN suspicious_score >= 20
                             AND suspicious_score < 40 THEN 1 ELSE 0 END)  AS medium,
                   SUM(CASE WHEN suspicious_score >= 40
                             AND suspicious_score < 60 THEN 1 ELSE 0 END)  AS high,
                   SUM(CASE WHEN suspicious_score >= 60 THEN 1 ELSE 0 END) AS critical
               FROM user_behavior"""
        ).fetchone()
        return {
            "low": int(row["low"] or 0),
            "medium": int(row["medium"] or 0),
            "high": int(row["high"] or 0),
            "critical": int(row["critical"] or 0),
        }


def get_security_stats() -> dict:
    with get_db() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(jailbreak_attempts), 0) AS total_jailbreaks,
                      COALESCE(SUM(honeypot_caught), 0)     AS total_honeypots,
                      COALESCE(SUM(total_requests), 0)      AS total_requests,
                      COUNT(*)                               AS total_users,
                      COALESCE(MAX(suspicious_score), 0)     AS max_score,
                      COALESCE(AVG(suspicious_score), 0)     AS avg_score
               FROM user_behavior"""
        ).fetchone()
        result = dict(row)
        result["total_jailbreaks"] = int(result["total_jailbreaks"])
        result["total_honeypots"] = int(result["total_honeypots"])
        result["total_requests"] = int(result["total_requests"])
        result["total_users"] = int(result["total_users"])
        result["max_score"] = int(result["max_score"])
        result["avg_score"] = float(round(result["avg_score"], 1))
        total_req = result["total_requests"] or 1
        result["jailbreak_rate"] = round(
            result["total_jailbreaks"] / total_req * 100, 1
        )
        result["honeypot_rate"] = round(
            result["total_honeypots"] / total_req * 100, 1
        )
        return result


# ── Audit log queries ────────────────────────────────────────────────────

def get_audit_log_paginated(page: int = 1, per_page: int = 50) -> dict:
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM panel_audit_log"
        ).fetchone()["cnt"]

        rows = conn.execute(
            """SELECT id, username, action, target, detail, timestamp
               FROM panel_audit_log
               ORDER BY timestamp DESC
               LIMIT ? OFFSET ?""",
            (per_page, offset),
        ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "entries": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


# ── Admin messages (DM/broadcast history) ────────────────────────────

def log_admin_message(msg_type: str, message: str, sent_by: str, target: str = None):
    """Log an admin DM or broadcast to admin_messages table."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO admin_messages (msg_type, target, message, sent_by, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (msg_type, target, message, sent_by, db_now()),
        )


def get_admin_messages(msg_type: str = "", limit: int | None = 50) -> list[dict]:
    """Get admin messages, optionally filtered by type."""
    if limit is not None and limit < 1:
        limit = 50

    with get_db() as conn:
        base_sql = """SELECT * FROM admin_messages"""
        params: tuple = ()

        if msg_type:
            base_sql += "\n                   WHERE msg_type = ?"
            params = (msg_type,)

        base_sql += "\n                   ORDER BY timestamp DESC"
        if limit is not None:
            base_sql += "\n                   LIMIT ?"
            params = (*params, limit)

        rows = conn.execute(base_sql, params).fetchall()
        return [dict(r) for r in rows]


def get_admin_message_entry(message_id: int, msg_type: str = "") -> dict | None:
    """Get a specific admin message row."""
    with get_db() as conn:
        if msg_type:
            row = conn.execute(
                """SELECT * FROM admin_messages
                   WHERE id = ? AND msg_type = ?""",
                (message_id, msg_type),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM admin_messages
                   WHERE id = ?""",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None


def delete_admin_message(message_id: int, msg_type: str = "") -> bool:
    """Delete one admin message row."""
    with get_db() as conn:
        if msg_type:
            cur = conn.execute(
                """DELETE FROM admin_messages
                   WHERE id = ? AND msg_type = ?""",
                (message_id, msg_type),
            )
        else:
            cur = conn.execute(
                "DELETE FROM admin_messages WHERE id = ?",
                (message_id,),
            )
        return (cur.rowcount or 0) > 0


def clear_admin_messages(msg_type: str = "") -> int:
    """Delete all admin messages, optionally by message type."""
    with get_db() as conn:
        if msg_type:
            cur = conn.execute(
                "DELETE FROM admin_messages WHERE msg_type = ?",
                (msg_type,),
            )
        else:
            cur = conn.execute("DELETE FROM admin_messages")
        return int(cur.rowcount or 0)


def get_admin_messages_for_export(msg_type: str) -> list[dict]:
    """Get full admin message history for CSV export."""
    return get_admin_messages(msg_type=msg_type, limit=None)


# ── Backup helpers ───────────────────────────────────────────────────────

def get_backup_list() -> list[dict]:
    backup_dir = backup_dir_path()
    if not backup_dir.is_dir():
        return []
    files = []
    for name in os.listdir(str(backup_dir)):
        if not name.endswith((".dump", ".sql", ".backup")):
            continue
        path = os.path.join(str(backup_dir), name)
        if os.path.isfile(path):
            stat = os.stat(path)
            files.append({
                "name": name,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": stat.st_mtime,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return files


# ── Database maintenance (danger zone) ───────────────────────────────────

_DB_RESET_TABLES: tuple[str, ...] = (
    # Child tables first (not required with CASCADE but keeps intent clear).
    "broadcast_recipients",
    "broadcast_jobs",
    "admin_messages",
    "bot_events",
    "reports",
    "panel_audit_log",
    "conversation_history",
    "conversation_logs",
    "message_log",
    "user_flag_progress",
    "flag_finders",
    "ctf_flags",
    "competition_round_winners",
    "competition_rounds",
    "competition_winner_log",
    "competition_winners",
    "competition_state",
    "ai_global_rpm",
    "user_requests",
    "user_behavior",
    "bot_state",
    "known_users",
    # Panel auth users (optional).
    "panel_users",
)


def reset_database(*, keep_panel_users: bool) -> None:
    """Destructively truncate application tables (PostgreSQL only)."""
    _keep = set()
    if keep_panel_users:
        _keep.add("panel_users")
        _keep.add("telegram_admins")
    tables = [t for t in _DB_RESET_TABLES if t not in _keep]
    if not tables:
        return
    with get_db() as conn:
        # Fail fast if the DB is busy/locked so the panel request doesn't hang.
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL statement_timeout = '60s'")
        try:
            conn.execute(
                f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"
            )
            return
        except Exception as exc:
            msg = str(exc).lower()
            if "permission denied for sequence" in msg:
                # Still wipe data even if we can't reset identities.
                conn.execute(
                    f"TRUNCATE {', '.join(tables)} CASCADE"
                )
                return

            is_privilege = (
                "permission denied" in msg
                or "must be owner" in msg
                or "not owner" in msg
            )
            if not is_privilege:
                raise

        # Fallback: DELETE rows in dependency order (slower, but needs only DELETE privileges).
        conn.execute("SET LOCAL statement_timeout = '0'")
        for table in tables:
            conn.execute(f"DELETE FROM {table}")


def backfill_known_users_from_message_log() -> None:
    """Backfill known_users from historical logs (helps recover usernames/last_seen)."""
    with get_db() as conn:
        conn.execute(
            """
            WITH events AS (
                SELECT
                    user_id,
                    NULLIF(TRIM(first_name), '') AS first_name,
                    NULLIF(TRIM(LEADING '@' FROM TRIM(username)), '') AS username,
                    timestamp
                FROM message_log
                UNION ALL
                SELECT
                    user_id,
                    NULLIF(TRIM(first_name), '') AS first_name,
                    NULLIF(TRIM(LEADING '@' FROM TRIM(username)), '') AS username,
                    timestamp
                FROM conversation_logs
            ),
            bounds AS (
                SELECT
                    user_id,
                    MIN(timestamp) AS first_seen,
                    MAX(timestamp) AS last_seen
                FROM events
                GROUP BY user_id
            ),
            best_name AS (
                SELECT DISTINCT ON (user_id)
                    user_id,
                    first_name
                FROM events
                WHERE first_name IS NOT NULL
                ORDER BY user_id, timestamp DESC
            ),
            best_username AS (
                SELECT DISTINCT ON (user_id)
                    user_id,
                    username
                FROM events
                WHERE username IS NOT NULL
                ORDER BY user_id, timestamp DESC
            )
            INSERT INTO known_users (user_id, first_name, username, first_seen, last_seen)
            SELECT
                b.user_id,
                n.first_name,
                u.username,
                b.first_seen,
                b.last_seen
            FROM bounds b
            LEFT JOIN best_name n ON n.user_id = b.user_id
            LEFT JOIN best_username u ON u.user_id = b.user_id
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = COALESCE(excluded.first_name, known_users.first_name),
                username = COALESCE(excluded.username, known_users.username),
                first_seen = CASE
                    WHEN known_users.first_seen IS NULL OR known_users.first_seen = '' THEN excluded.first_seen
                    WHEN excluded.first_seen < known_users.first_seen THEN excluded.first_seen
                    ELSE known_users.first_seen
                END,
                last_seen = CASE
                    WHEN excluded.last_seen > COALESCE(known_users.last_seen, '')
                        THEN excluded.last_seen
                    ELSE known_users.last_seen
                END
            """
        )


# ── Report queries ──────────────────────────────────────────────────────

def get_reports_paginated(
    page: int = 1,
    per_page: int = 25,
    status: str = "",
    search: str = "",
) -> dict:
    offset = (page - 1) * per_page
    conditions: list[str] = []
    params: list = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if search:
        pattern = f"%{_escape_like(search)}%"
        conditions.append(
            f"({_ci_like('message')} OR {_ci_like('first_name')} "
            f"OR {_ci_like('username')} OR {_ci_like('user_id')})"
        )
        params.extend([pattern, pattern, pattern, pattern])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM reports {where}", params
        ).fetchone()["cnt"]

        rows = conn.execute(
            f"""SELECT * FROM reports {where}
                ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,
                         created_at DESC
                LIMIT ? OFFSET ?""",
            (*params, per_page, offset),
        ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "reports": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


def get_banned_users() -> list[dict]:
    """Get banned users joined with known_users for name/username."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT b.user_id, b.banned_by, b.banned_at,
                      ku.first_name, ku.username
               FROM banned_users b
               LEFT JOIN known_users ku ON ku.user_id = b.user_id
               ORDER BY b.banned_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_timed_out_users() -> list[dict]:
    """Get timed out users joined with known_users for name/username."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT t.user_id, t.timeout_until, t.timed_out_by, t.timed_out_at,
                      ku.first_name, ku.username
               FROM timed_out_users t
               LEFT JOIN known_users ku ON ku.user_id = t.user_id
               ORDER BY t.timed_out_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


# ── Event queries ──────────────────────────────────────────────────────

def get_events_paginated(
    page: int = 1,
    per_page: int = 25,
    event_type: str = "",
    search: str = "",
) -> dict:
    offset = (page - 1) * per_page
    conditions: list[str] = []
    params: list = []

    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if search:
        pattern = f"%{_escape_like(search)}%"
        conditions.append(
            f"({_ci_like('message')} OR {_ci_like('first_name')} "
            f"OR {_ci_like('username')} OR {_ci_like('user_id')})"
        )
        params.extend([pattern, pattern, pattern, pattern])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM bot_events {where}", params
        ).fetchone()["cnt"]

        rows = conn.execute(
            f"""SELECT * FROM bot_events {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?""",
            (*params, per_page, offset),
        ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "events": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


def get_event_stats() -> dict:
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN event_type = 'flag_leak_blocked' THEN 1 ELSE 0 END) AS flag_leak_blocked,
                      SUM(CASE WHEN event_type = 'error' THEN 1 ELSE 0 END) AS error,
                      SUM(CASE WHEN event_type = 'user_report' THEN 1 ELSE 0 END) AS user_report
               FROM bot_events"""
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "flag_leak_blocked": int(row["flag_leak_blocked"] or 0),
            "error": int(row["error"] or 0),
            "user_report": int(row["user_report"] or 0),
        }


def get_report_stats() -> dict:
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open,
                      SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved
               FROM reports"""
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "open": int(row["open"] or 0),
            "resolved": int(row["resolved"] or 0),
        }


# ── Panel user management ────────────────────────────────────────────────

def get_panel_users() -> list[dict]:
    """Get all panel users."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, username, is_active, created_at, last_login
               FROM panel_users
               ORDER BY username"""
        ).fetchall()
        return [dict(r) for r in rows]


def create_panel_user(username: str, password_hash: str) -> bool:
    """Create a panel user. Returns False if username already exists."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM panel_users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO panel_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, db_now()),
        )
        return True


def delete_panel_user(username: str) -> str | None:
    """Delete a panel user. Returns error string or None on success."""
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM panel_users"
        ).fetchone()["cnt"]
        if total <= 1:
            return "Cannot delete the last remaining panel user."
        deleted = conn.execute(
            "DELETE FROM panel_users WHERE username = ?", (username,)
        ).rowcount or 0
        if not deleted:
            return "User not found."
    return None


def change_panel_user_password(username: str, password_hash: str) -> bool:
    """Update password for a panel user. Returns False if user not found."""
    with get_db() as conn:
        updated = conn.execute(
            "UPDATE panel_users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        ).rowcount or 0
        return updated > 0
