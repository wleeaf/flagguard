import threading
import time

from config import KNOWN_USER_UPSERT_INTERVAL_SECONDS
from database import get_db
from time_utils import db_now

_known_user_cache: dict[str, tuple[float, str, str]] = {}
_known_user_cache_lock = threading.Lock()


def _normalize_username(username: str | None) -> str:
    value = (username or "").strip()
    if not value:
        return ""
    # Telegram usernames never include "@", but some sources may include it.
    if value.startswith("@"):
        value = value[1:].strip()
    if not value or value.casefold() == "anonymous":
        return ""
    return value


class UserRepository:
    """Manages user behavior tracking and known-user registry."""

    def get_behavior(self, user_id: str) -> dict:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM user_behavior WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                now = db_now()
                conn.execute(
                    "INSERT INTO user_behavior (user_id, last_reset) VALUES (?, ?)",
                    (user_id, now),
                )
                return {
                    "user_id": user_id,
                    "jailbreak_attempts": 0,
                    "suspicious_score": 0,
                    "honeypot_caught": 0,
                    "total_requests": 0,
                    "last_reset": now,
                }
            return dict(row)

    _ALLOWED_FIELDS = frozenset({
        "jailbreak_attempts", "suspicious_score", "honeypot_caught", "total_requests",
    })

    def increment_behavior(self, user_id: str, field: str, amount: int = 1):
        if field not in self._ALLOWED_FIELDS:
            raise ValueError(f"Invalid behavior field: {field}")
        self.get_behavior(user_id)
        with get_db() as conn:
            conn.execute(
                f"UPDATE user_behavior SET {field} = {field} + ? WHERE user_id = ?",
                (amount, user_id),
            )

    def reset_behavior(self, user_id: str):
        with get_db() as conn:
            conn.execute(
                """UPDATE user_behavior
                   SET jailbreak_attempts = 0, suspicious_score = 0,
                       honeypot_caught = 0, total_requests = 0,
                       last_reset = ?
                   WHERE user_id = ?""",
                (db_now(), user_id),
            )

    def get_all_behaviors(self) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM user_behavior").fetchall()
            return [dict(r) for r in rows]

    def get_top_jailbreakers(self, limit: int = 10) -> list[dict]:
        """Return the top N users by jailbreak_attempts (sorted in SQL, not Python)."""
        if limit < 1:
            limit = 10
        with get_db() as conn:
            rows = conn.execute(
                """SELECT * FROM user_behavior
                   WHERE jailbreak_attempts > 0
                   ORDER BY jailbreak_attempts DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self, user_id: str) -> dict:
        b = self.get_behavior(user_id)
        return {
            "total_requests": b.get("total_requests", 0),
            "jailbreak_attempts": b.get("jailbreak_attempts", 0),
            "suspicious_score": b.get("suspicious_score", 0),
            "honeypot_caught": b.get("honeypot_caught", 0),
        }

    def get_global_stats(self) -> dict:
        with get_db() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as total_users,
                          COALESCE(SUM(total_requests), 0) as total_requests,
                          COALESCE(SUM(jailbreak_attempts), 0) as total_jailbreaks,
                          COALESCE(SUM(honeypot_caught), 0) as total_honeypots
                   FROM user_behavior"""
            ).fetchone()
            return dict(row)

    def batch_update_behavior(self, user_id: str, total_delta: int = 1,
                              jailbreak_delta: int = 0, score_delta: int = 0) -> dict | None:
        """Update multiple behavior counters in a single DB write. Returns updated row."""
        now = db_now()
        with get_db() as conn:
            row = conn.execute(
                """INSERT INTO user_behavior
                   (user_id, jailbreak_attempts, suspicious_score, honeypot_caught, total_requests, last_reset)
                   VALUES (?, ?, ?, 0, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       total_requests = user_behavior.total_requests + excluded.total_requests,
                       jailbreak_attempts = user_behavior.jailbreak_attempts + excluded.jailbreak_attempts,
                       suspicious_score = user_behavior.suspicious_score + excluded.suspicious_score
                   RETURNING *""",
                (user_id, jailbreak_delta, score_delta, total_delta, now),
            ).fetchone()
            return dict(row) if row else None

    def upsert_known(self, user_id: str, first_name: str, username: str | None):
        # Thread-safety note: two threads may both pass the cache check and
        # write to DB concurrently.  This is intentional — the DB upsert is
        # idempotent (ON CONFLICT … DO UPDATE), so the only cost is a rare
        # duplicate write rather than holding the lock across I/O.
        now_mono = time.monotonic()
        safe_first_name = first_name or ""
        safe_username = _normalize_username(username)
        with _known_user_cache_lock:
            cached = _known_user_cache.get(user_id)
            if cached:
                last_write, prev_first_name, prev_username = cached
                if (
                    now_mono - last_write < KNOWN_USER_UPSERT_INTERVAL_SECONDS
                    and prev_first_name == safe_first_name
                    and prev_username == safe_username
                ):
                    return

        now = db_now()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO known_users (user_id, first_name, username, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = COALESCE(NULLIF(excluded.username, ''), known_users.username),
                       last_seen = excluded.last_seen""",
                (user_id, safe_first_name, safe_username, now, now),
            )

        with _known_user_cache_lock:
            _known_user_cache[user_id] = (now_mono, safe_first_name, safe_username)
            if len(_known_user_cache) > 20000:
                cutoff = now_mono - 600.0
                stale = [uid for uid, (ts, _, _) in _known_user_cache.items() if ts < cutoff]
                for uid in stale:
                    _known_user_cache.pop(uid, None)

    def get_all_known_ids(self) -> list[str]:
        with get_db() as conn:
            rows = conn.execute("SELECT user_id FROM known_users").fetchall()
            return [r["user_id"] for r in rows]

    def count_known_users(self) -> int:
        with get_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM known_users").fetchone()
            return int(row["cnt"] if row else 0)
