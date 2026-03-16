"""Repository for DB-backed Telegram admin management."""

from database import get_db
from time_utils import db_now


class AdminRepository:
    """CRUD operations for the telegram_admins table."""

    def list_admins(self) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT telegram_id, label, added_at, silent FROM telegram_admins ORDER BY added_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def add_admin(self, telegram_id: str, label: str = "") -> bool:
        """Add a telegram admin. Returns True if inserted, False if already exists."""
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO telegram_admins (telegram_id, label, added_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(telegram_id) DO NOTHING""",
                (str(telegram_id).strip(), (label or "").strip(), db_now()),
            )
            return (cur.rowcount or 0) > 0

    def remove_admin(self, telegram_id: str) -> bool:
        """Remove a telegram admin. Returns True if deleted."""
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM telegram_admins WHERE telegram_id = ?",
                (str(telegram_id).strip(),),
            )
            return (cur.rowcount or 0) > 0

    def is_admin(self, telegram_id: str) -> bool:
        with get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM telegram_admins WHERE telegram_id = ?",
                (str(telegram_id).strip(),),
            ).fetchone()
            return row is not None

    def get_all_admin_ids(self) -> list[int]:
        """Return all DB admin IDs as integers."""
        with get_db() as conn:
            rows = conn.execute(
                "SELECT telegram_id FROM telegram_admins"
            ).fetchall()
            result = []
            for r in rows:
                try:
                    result.append(int(r["telegram_id"]))
                except (ValueError, TypeError):
                    pass
            return result

    def toggle_silent(self, telegram_id: str) -> bool:
        """Flip the silent flag for an admin. Returns the new value."""
        tid = str(telegram_id).strip()
        with get_db() as conn:
            conn.execute(
                "UPDATE telegram_admins SET silent = NOT COALESCE(silent, FALSE) WHERE telegram_id = ?",
                (tid,),
            )
            row = conn.execute(
                "SELECT silent FROM telegram_admins WHERE telegram_id = ?",
                (tid,),
            ).fetchone()
            return bool(row["silent"]) if row else False

    def get_non_silent_admin_ids(self) -> list[int]:
        """Return DB admin IDs where silent IS NOT TRUE."""
        with get_db() as conn:
            rows = conn.execute(
                "SELECT telegram_id FROM telegram_admins WHERE silent IS NOT TRUE"
            ).fetchall()
            result = []
            for r in rows:
                try:
                    result.append(int(r["telegram_id"]))
                except (ValueError, TypeError):
                    pass
            return result

    def get_silent_admin_ids(self) -> list[int]:
        """Return DB admin IDs where silent = TRUE."""
        with get_db() as conn:
            rows = conn.execute(
                "SELECT telegram_id FROM telegram_admins WHERE silent = TRUE"
            ).fetchall()
            result = []
            for r in rows:
                try:
                    result.append(int(r["telegram_id"]))
                except (ValueError, TypeError):
                    pass
            return result
