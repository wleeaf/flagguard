import threading
import time as _time

from database import get_db
from time_utils import db_now

# Module-level caches shared across all ModerationRepository instances.
_ban_cache: dict[str, tuple[bool, float]] = {}
_timeout_cache: dict[str, tuple[str | None, float]] = {}
_CACHE_TTL: float = 5.0
_cache_lock = threading.Lock()


class ModerationRepository:
    """Ban and timeout management with in-memory TTL cache."""

    # ── Bans ──────────────────────────────────────────────────────────

    def ban_user(self, user_id: str, banned_by: str = "") -> bool:
        now = db_now()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO banned_users (user_id, banned_by, banned_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       banned_by = excluded.banned_by,
                       banned_at = excluded.banned_at""",
                (user_id, banned_by, now),
            )
        with _cache_lock:
            _ban_cache[user_id] = (True, _time.time())
        return True

    def unban_user(self, user_id: str) -> bool:
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM banned_users WHERE user_id = ?", (user_id,)
            )
            deleted = (cur.rowcount or 0) > 0
        with _cache_lock:
            _ban_cache[user_id] = (False, _time.time())
        return deleted

    def is_banned(self, user_id: str) -> bool:
        now = _time.time()
        with _cache_lock:
            entry = _ban_cache.get(user_id)
            if entry and (now - entry[1]) <= _CACHE_TTL:
                return entry[0]
        with get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)
            ).fetchone()
        result = row is not None
        with _cache_lock:
            _ban_cache[user_id] = (result, _time.time())
        return result

    def list_banned(self) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM banned_users ORDER BY banned_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Timeouts ──────────────────────────────────────────────────────

    def timeout_user(self, user_id: str, until: str, timed_out_by: str = "") -> bool:
        now = db_now()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO timed_out_users (user_id, timeout_until, timed_out_by, timed_out_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       timeout_until = excluded.timeout_until,
                       timed_out_by = excluded.timed_out_by,
                       timed_out_at = excluded.timed_out_at""",
                (user_id, until, timed_out_by, now),
            )
        with _cache_lock:
            _timeout_cache[user_id] = (until, _time.time())
        return True

    def remove_timeout(self, user_id: str) -> bool:
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM timed_out_users WHERE user_id = ?", (user_id,)
            )
            deleted = (cur.rowcount or 0) > 0
        with _cache_lock:
            _timeout_cache[user_id] = (None, _time.time())
        return deleted

    def get_active_timeout(self, user_id: str) -> dict | None:
        now_str = db_now()
        now = _time.time()

        with _cache_lock:
            entry = _timeout_cache.get(user_id)
            if entry and (now - entry[1]) <= _CACHE_TTL:
                until = entry[0]
                if until is None or until <= now_str:
                    return None
                return {"user_id": user_id, "timeout_until": until}

        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM timed_out_users WHERE user_id = ? AND timeout_until > ?",
                (user_id, now_str),
            ).fetchone()

        if row:
            result = dict(row)
            with _cache_lock:
                _timeout_cache[user_id] = (result["timeout_until"], _time.time())
            return result

        with _cache_lock:
            _timeout_cache[user_id] = (None, _time.time())
        return None

    def list_timeouts(self) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM timed_out_users ORDER BY timed_out_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def cleanup_expired_timeouts(self) -> int:
        now_str = db_now()
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM timed_out_users WHERE timeout_until <= ?",
                (now_str,),
            )
            return int(cur.rowcount or 0)
