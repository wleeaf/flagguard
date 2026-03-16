from database import get_db
from time_utils import db_now


class ReportRepository:

    def save(self, user_id: str, first_name: str, username: str, message: str) -> int:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO reports (user_id, first_name, username, message, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   RETURNING id""",
                (user_id, first_name, username, message, db_now()),
            )
            row = cur.fetchone()
            if row is not None:
                return int(row["id"])
            if getattr(cur, "lastrowid", None) is not None:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM reports WHERE user_id = ? AND message = ? ORDER BY id DESC LIMIT 1",
                (user_id, message),
            ).fetchone()
            return int(row["id"]) if row else 0

    def get_by_id(self, report_id: int) -> dict | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
            return dict(row) if row else None

    def mark_resolved(self, report_id: int, resolved_by: str) -> bool:
        with get_db() as conn:
            cur = conn.execute(
                """UPDATE reports
                   SET status = 'resolved',
                       resolved_by = ?,
                       resolved_at = ?
                   WHERE id = ? AND status = 'open'""",
                (resolved_by, db_now(), report_id),
            )
            return cur.rowcount > 0

    def reopen(self, report_id: int) -> bool:
        with get_db() as conn:
            cur = conn.execute(
                """UPDATE reports
                   SET status = 'open',
                       resolved_by = NULL,
                       resolved_at = NULL
                   WHERE id = ? AND status = 'resolved'""",
                (report_id,),
            )
            return cur.rowcount > 0

    def count_open(self) -> int:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reports WHERE status = 'open'"
            ).fetchone()
            return row["cnt"]
