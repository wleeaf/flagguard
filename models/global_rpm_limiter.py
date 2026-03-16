from __future__ import annotations

import time

from database import get_db
from time_utils import db_now


class GlobalRpmLimiter:
    """Cross-process RPM limiter backed by PostgreSQL.

    This is intended for single-machine multi-process deployments (e.g., 8 webhook workers
    with SO_REUSEPORT) where per-process asyncio semaphores are insufficient to enforce
    a global quota.
    """

    def __init__(self, *, retention_minutes: int = 120):
        self._retention_minutes = max(1, int(retention_minutes))
        self._last_cleanup_bucket: int | None = None

    def try_acquire(self, key: str, limit: int) -> bool:
        """Atomically consume 1 slot from the current minute bucket.

        Returns True if the slot was acquired, False if the bucket is at capacity.
        """
        if limit <= 0:
            return True

        bucket = int(time.time() // 60)
        now_ts = db_now()

        with get_db() as conn:
            row = conn.execute(
                """INSERT INTO ai_global_rpm (key, bucket, count, updated_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(key, bucket) DO UPDATE
                       SET count = ai_global_rpm.count + 1,
                           updated_at = excluded.updated_at
                       WHERE ai_global_rpm.count < ?
                   RETURNING count""",
                (key, bucket, now_ts, limit),
            ).fetchone()

            # Opportunistic cleanup once per bucket per-process (best-effort).
            if self._last_cleanup_bucket != bucket:
                self._last_cleanup_bucket = bucket
                cutoff = bucket - self._retention_minutes
                conn.execute(
                    "DELETE FROM ai_global_rpm WHERE key = ? AND bucket < ?",
                    (key, cutoff),
                )

        return row is not None

