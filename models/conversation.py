import logging
import random
import threading

from database import get_db
from time_utils import db_now

_prune_counter = 0
_prune_lock = threading.Lock()


class ConversationRepository:
    """Manages permanent conversation logs and rolling AI context history."""

    MAX_HISTORY = 10
    _PRUNE_EVERY_N = 10  # only check pruning every Nth add_turn call

    def log(self, user_id: str, first_name: str, username: str,
            user_msg: str, ai_msg: str):
        self._db_log(user_id, first_name, username, user_msg, ai_msg)

    def get_logs(self, user_id: str, limit: int = 100) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT * FROM conversation_logs
                   WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def _db_log(self, user_id, first_name, username, user_msg, ai_msg):
        try:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO conversation_logs
                       (user_id, first_name, username, user_msg, ai_msg, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, first_name, username, user_msg, ai_msg, db_now()),
                )
        except Exception as e:
            logging.error("DB log error: %s", e)

    def add_turn(self, user_id: str, role: str, content: str):
        global _prune_counter
        with get_db() as conn:
            conn.execute(
                """INSERT INTO conversation_history (user_id, role, content, timestamp)
                   VALUES (?, ?, ?, ?)""",
                (user_id, role, content, db_now()),
            )

        # Only check pruning every Nth call to avoid a COUNT query on every insert.
        # A 5% random chance per call ensures all users get pruned eventually,
        # not just whichever user happens to be the Nth caller.
        should_prune = False
        with _prune_lock:
            _prune_counter += 1
            if _prune_counter >= self._PRUNE_EVERY_N:
                _prune_counter = 0
                should_prune = True
        if not should_prune and random.random() < 0.05:
            should_prune = True

        if should_prune:
            max_rows = self.MAX_HISTORY * 2
            with get_db() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM conversation_history WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["cnt"]
                if count > max_rows + 5:
                    conn.execute(
                        """DELETE FROM conversation_history
                           WHERE user_id = ? AND id <= (
                               SELECT id FROM conversation_history
                               WHERE user_id = ?
                               ORDER BY id DESC
                               LIMIT 1 OFFSET ?
                           )""",
                        (user_id, user_id, max_rows),
                    )

    def get_history(self, user_id: str) -> list[dict]:
        max_rows = self.MAX_HISTORY * 2
        with get_db() as conn:
            rows = conn.execute(
                """SELECT role, content FROM conversation_history
                   WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, max_rows),
            ).fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_history(self, user_id: str):
        with get_db() as conn:
            conn.execute(
                "DELETE FROM conversation_history WHERE user_id = ?", (user_id,)
            )

    def clear_all_history(self) -> int:
        """Delete all AI context history for every user. Returns rows deleted."""
        with get_db() as conn:
            cur = conn.execute("DELETE FROM conversation_history")
            return cur.rowcount or 0
