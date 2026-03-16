from __future__ import annotations

import threading
import time
from collections import deque

from config import (
    LOGIC_RATE_LIMIT_WINDOW_SECONDS,
    MAX_REQUESTS_PER_MINUTE,
    RATE_LIMIT_BACKEND,
)
from database import get_db
from time_utils import db_ago, db_now


class RateLimiter:
    """
    Logic-level in-memory sliding-window rate limiter.

    Purpose: Abuse prevention (e.g., 10 requests/min).
    This is distinct from the fast per-user cooldown limiter in handlers.
    """

    _PRUNE_THRESHOLD = 5000

    def __init__(self):
        self._backend = self._resolve_backend()
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, user_id: str) -> bool:
        if self._backend == "database":
            return self._check_database(user_id)
        return self._check_memory(user_id)

    def _check_memory(self, user_id: str) -> bool:
        now = time.monotonic()
        window = float(LOGIC_RATE_LIMIT_WINDOW_SECONDS)

        with self._lock:
            events = self._events.setdefault(user_id, deque())
            self._drop_old(events, now, window)
            if len(events) >= MAX_REQUESTS_PER_MINUTE:
                return False
            events.append(now)
            if len(self._events) > self._PRUNE_THRESHOLD:
                self._prune_all(now, window)
            return True

    def _count_recent(self, user_id: str, seconds: int = 60) -> int:
        if self._backend == "database":
            threshold = db_ago(seconds=seconds)
            with get_db() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) AS cnt
                       FROM user_requests
                       WHERE user_id = ? AND timestamp > ?""",
                    (user_id, threshold),
                ).fetchone()
            return int(row["cnt"] if row else 0)

        now = time.monotonic()
        with self._lock:
            events = self._events.get(user_id)
            if not events:
                return 0
            self._drop_old(events, now, float(seconds))
            return len(events)

    def cleanup(self, seconds: int = 120):
        if self._backend == "database":
            threshold = db_ago(seconds=seconds)
            with get_db() as conn:
                conn.execute(
                    "DELETE FROM user_requests WHERE timestamp < ?",
                    (threshold,),
                )
            return

        now = time.monotonic()
        with self._lock:
            self._prune_all(now, float(seconds))

    @staticmethod
    def _drop_old(events: deque[float], now: float, window_seconds: float) -> None:
        while events and (now - events[0]) > window_seconds:
            events.popleft()

    def _prune_all(self, now: float, window_seconds: float) -> None:
        stale_users = []
        for uid, events in self._events.items():
            self._drop_old(events, now, window_seconds)
            if not events:
                stale_users.append(uid)
        for uid in stale_users:
            self._events.pop(uid, None)

    @staticmethod
    def _resolve_backend() -> str:
        if RATE_LIMIT_BACKEND == "memory":
            return "memory"
        if RATE_LIMIT_BACKEND == "database":
            return "database"
        # auto — always database (PostgreSQL)
        return "database"

    def _check_database(self, user_id: str) -> bool:
        threshold = db_ago(seconds=LOGIC_RATE_LIMIT_WINDOW_SECONDS)
        now_ts = db_now()
        with get_db() as conn:
            # Atomic: INSERT only if the user hasn't exceeded the limit.
            # The sub-select count and the INSERT happen within the same
            # statement, so concurrent transactions cannot both pass.
            cur = conn.execute(
                """INSERT INTO user_requests (user_id, timestamp)
                   SELECT ?, ?
                   WHERE (SELECT COUNT(*) FROM user_requests
                          WHERE user_id = ? AND timestamp > ?) < ?""",
                (user_id, now_ts, user_id, threshold, MAX_REQUESTS_PER_MINUTE),
            )
            return cur.rowcount > 0
