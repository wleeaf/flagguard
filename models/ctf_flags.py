import logging

from database import get_db
from time_utils import db_now


class CTFFlagRepository:
    """Manages CTF flag definitions and per-user progress tracking."""

    # ── Flag CRUD ────────────────────────────────────────────────────────

    def get_all_flags(self) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM ctf_flags ORDER BY position"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_flag_count(self) -> int:
        with get_db() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS cnt FROM ctf_flags"
            ).fetchone()["cnt"]

    def create_flag(
        self,
        flag_value: str,
        success_message: str,
        position: int | None = None,
        tag: str = "",
        notify_on_find: bool = False,
        notify_message: str = "",
    ) -> int:
        with get_db() as conn:
            if position is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM ctf_flags"
                ).fetchone()
                position = row["next_pos"]
            row = conn.execute(
                "INSERT INTO ctf_flags (position, flag_value, success_message, tag, notify_on_find, notify_message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (position, flag_value, success_message, tag, notify_on_find, notify_message, db_now()),
            ).fetchone()
            return row["id"]

    def update_flag(self, flag_id: int, flag_value: str, success_message: str, tag: str = "", notify_on_find: bool = False, notify_message: str = "") -> bool:
        with get_db() as conn:
            cur = conn.execute(
                "UPDATE ctf_flags SET flag_value = ?, success_message = ?, tag = ?, notify_on_find = ?, notify_message = ? WHERE id = ?",
                (flag_value, success_message, tag, notify_on_find, notify_message, flag_id),
            )
            return (cur.rowcount or 0) > 0

    def reorder_flags(self, ordered_ids: list[int]) -> None:
        with get_db() as conn:
            for pos, flag_id in enumerate(ordered_ids, 1):
                conn.execute(
                    "UPDATE ctf_flags SET position = ? WHERE id = ?",
                    (-(pos), flag_id),
                )
            for pos, flag_id in enumerate(ordered_ids, 1):
                conn.execute(
                    "UPDATE ctf_flags SET position = ? WHERE id = ?",
                    (pos, flag_id),
                )

    def delete_flag(self, flag_id: int) -> bool:
        with get_db() as conn:
            row = conn.execute(
                "SELECT position FROM ctf_flags WHERE id = ?", (flag_id,)
            ).fetchone()
            if not row:
                return False
            deleted_pos = row["position"]
            conn.execute("DELETE FROM ctf_flags WHERE id = ?", (flag_id,))
            # Reorder remaining flags to close the gap.
            conn.execute(
                "UPDATE ctf_flags SET position = position - 1 WHERE position > ?",
                (deleted_pos,),
            )
            return True

    # ── Flag matching ────────────────────────────────────────────────────

    def check_input_against_flags(self, user_input: str) -> dict | None:
        """Substring match against all flags, return first match by position."""
        flags = self.get_all_flags()
        for flag in flags:
            if flag["flag_value"] and flag["flag_value"] in user_input:
                return flag
        return None

    # ── User progress ────────────────────────────────────────────────────

    def get_user_progress(self, user_id: str) -> list[dict]:
        """All flags with found_at (NULL if not found by this user)."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT cf.id, cf.position, cf.flag_value, cf.success_message,
                          ufp.found_at
                   FROM ctf_flags cf
                   LEFT JOIN user_flag_progress ufp
                          ON ufp.flag_id = cf.id AND ufp.user_id = ?
                   ORDER BY cf.position""",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_user_max_position(self, user_id: str) -> int:
        """Highest flag position this user has found (0 if none)."""
        with get_db() as conn:
            row = conn.execute(
                """SELECT COALESCE(MAX(cf.position), 0) AS max_pos
                   FROM user_flag_progress ufp
                   JOIN ctf_flags cf ON cf.id = ufp.flag_id
                   WHERE ufp.user_id = ?""",
                (user_id,),
            ).fetchone()
            return row["max_pos"]

    def has_user_found_flag(self, user_id: str, flag_id: int) -> bool:
        with get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_flag_progress WHERE user_id = ? AND flag_id = ?",
                (user_id, flag_id),
            ).fetchone()
            return row is not None

    def record_flag_found(self, user_id: str, flag_id: int) -> bool:
        """Record a flag find. Returns True if newly inserted."""
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO user_flag_progress (user_id, flag_id, found_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT (user_id, flag_id) DO NOTHING""",
                (user_id, flag_id, db_now()),
            )
            return (cur.rowcount or 0) > 0

    def get_progress_leaderboard(self, limit: int = 250) -> list[dict]:
        """Users ranked by flags_found desc, then earliest last_found_at asc."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT ufp.user_id,
                          ku.first_name,
                          ku.username,
                          COUNT(*) AS flags_found,
                          MAX(ufp.found_at) AS last_found_at
                   FROM user_flag_progress ufp
                   LEFT JOIN known_users ku ON ku.user_id = ufp.user_id
                   GROUP BY ufp.user_id, ku.first_name, ku.username
                   ORDER BY flags_found DESC, last_found_at ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            result = []
            for i, r in enumerate(rows, 1):
                d = dict(r)
                d["rank"] = i
                result.append(d)
            return result
