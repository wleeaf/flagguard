import logging

from database import get_db

from config import MAX_WINNERS as _DEFAULT_MAX_WINNERS
from models.user import _normalize_username
from time_utils import db_now


class CompetitionRepository:
    """Manages competition state, rounds, and leaderboard data."""

    def get_state(self) -> dict:
        with get_db() as conn:
            rows = conn.execute("SELECT key, value FROM competition_state").fetchall()
            state = {r["key"]: r["value"] for r in rows}
            return {
                "active": state.get("active", "false") == "true",
                "answer": state.get("answer", ""),
                "question": state.get("question", ""),
                "reward_message": state.get("reward_message", ""),
                "deadline": state.get("deadline", ""),
            }

    def get_max_winners(self) -> int:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM competition_state WHERE key = 'max_winners'"
            ).fetchone()
            if row and str(row["value"]).isdigit():
                return int(row["value"])
            return _DEFAULT_MAX_WINNERS

    def set_max_winners(self, count: int):
        with get_db() as conn:
            conn.execute(
                """INSERT INTO competition_state (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                ("max_winners", str(count)),
            )
            round_id = self._get_active_round_id_tx(conn)
            if round_id is not None:
                conn.execute(
                    """UPDATE competition_rounds
                       SET max_winners = ?
                       WHERE id = ? AND status = 'active'""",
                    (count, round_id),
                )

    @staticmethod
    def _set_state_value_tx(conn, key: str, value: str) -> None:
        conn.execute(
            """INSERT INTO competition_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )

    @staticmethod
    def _get_active_round_id_tx(conn) -> int | None:
        row = conn.execute(
            "SELECT value FROM competition_state WHERE key = 'active_round_id'"
        ).fetchone()
        if not row:
            return None
        value = str(row["value"] or "").strip()
        if not value.isdigit():
            return None
        return int(value)

    def _close_active_round_tx(self, conn, reason: str, now_ts: str) -> bool:
        round_id = self._get_active_round_id_tx(conn)
        self._set_state_value_tx(conn, "active_round_id", "")
        if round_id is None:
            return False
        cur = conn.execute(
            """UPDATE competition_rounds
               SET status = 'ended',
                   end_reason = COALESCE(NULLIF(?, ''), end_reason),
                   ended_at = COALESCE(ended_at, ?)
               WHERE id = ?
                 AND status != 'ended'""",
            (reason, now_ts, round_id),
        )
        return cur.rowcount > 0

    def start_round(
        self,
        *,
        question: str,
        reward_message: str,
        max_winners: int,
        initiated_by: str,
    ) -> int:
        now_ts = db_now()
        safe_question = (question or "").strip()
        safe_reward = (reward_message or "").strip()
        safe_initiated = (initiated_by or "").strip()
        with get_db() as conn:
            self._close_active_round_tx(conn, "replaced", now_ts)
            cur = conn.execute(
                """INSERT INTO competition_rounds
                   (question, reward_message, max_winners, initiated_by, status, started_at)
                   VALUES (?, ?, ?, ?, 'active', ?)
                   RETURNING id""",
                (safe_question, safe_reward, max_winners, safe_initiated, now_ts),
            )
            row = cur.fetchone()
            if row is not None:
                round_id = int(row["id"])
            elif getattr(cur, "lastrowid", None) is not None:
                round_id = int(cur.lastrowid)
            else:
                fallback = conn.execute(
                    """SELECT id
                       FROM competition_rounds
                       WHERE started_at = ?
                       ORDER BY id DESC
                       LIMIT 1""",
                    (now_ts,),
                ).fetchone()
                round_id = int(fallback["id"])
            self._set_state_value_tx(conn, "active_round_id", str(round_id))
        return round_id

    def end_active_round(self, reason: str = "manual") -> bool:
        now_ts = db_now()
        with get_db() as conn:
            return self._close_active_round_tx(conn, reason, now_ts)

    def set_state(
        self,
        active: bool,
        answer: str = "",
        question: str = "",
        reward_message: str = "",
        deadline: str = "",
    ):
        with get_db() as conn:
            for key, value in [
                ("active", "true" if active else "false"),
                ("answer", answer),
                ("question", question),
                ("reward_message", reward_message),
                ("deadline", deadline),
            ]:
                self._set_state_value_tx(conn, key, value)

    def setup_competition(
        self,
        *,
        answer: str,
        question: str,
        max_winners: int,
        initiated_by: str,
        reward_message: str = "",
        deadline: str = "",
    ) -> int:
        """Atomically set up a new competition in a single transaction.

        Sets state, clears old winners, and starts a new round.
        Returns the new round id.
        """
        now_ts = db_now()
        safe_question = (question or "").strip()
        safe_reward = (reward_message or "").strip()
        safe_initiated = (initiated_by or "").strip()
        with get_db() as conn:
            # Set all competition state atomically
            for key, value in [
                ("active", "true"),
                ("answer", answer),
                ("question", safe_question),
                ("reward_message", safe_reward),
                ("deadline", deadline),
                ("max_winners", str(max_winners)),
            ]:
                self._set_state_value_tx(conn, key, value)

            # Clear old winners
            conn.execute("DELETE FROM competition_winners")

            # Close any existing round and start a new one
            self._close_active_round_tx(conn, "replaced", now_ts)
            cur = conn.execute(
                """INSERT INTO competition_rounds
                   (question, reward_message, max_winners, initiated_by, status, started_at)
                   VALUES (?, ?, ?, ?, 'active', ?)
                   RETURNING id""",
                (safe_question, safe_reward, max_winners, safe_initiated, now_ts),
            )
            row = cur.fetchone()
            if row is not None:
                round_id = int(row["id"])
            elif getattr(cur, "lastrowid", None) is not None:
                round_id = int(cur.lastrowid)
            else:
                fallback = conn.execute(
                    """SELECT id FROM competition_rounds
                       WHERE started_at = ? ORDER BY id DESC LIMIT 1""",
                    (now_ts,),
                ).fetchone()
                round_id = int(fallback["id"])
            self._set_state_value_tx(conn, "active_round_id", str(round_id))
        return round_id

    def end_competition(self, reason: str = "manual") -> tuple[list[str], int]:
        """Atomically end a competition in a single transaction.

        Returns (winner_user_ids, max_winners) captured before clearing.
        """
        now_ts = db_now()
        with get_db() as conn:
            # Capture current winners before clearing
            rows = conn.execute(
                "SELECT user_id FROM competition_winners ORDER BY won_at"
            ).fetchall()
            winners = [r["user_id"] for r in rows]

            row = conn.execute(
                "SELECT value FROM competition_state WHERE key = 'max_winners'"
            ).fetchone()
            max_winners = int(row["value"]) if row and str(row["value"]).isdigit() else _DEFAULT_MAX_WINNERS

            # Deactivate
            self._set_state_value_tx(conn, "active", "false")
            self._close_active_round_tx(conn, reason, now_ts)

            # Clear winners
            conn.execute("DELETE FROM competition_winners")
        return winners, max_winners

    def try_end_by_deadline(self) -> tuple[bool, list[str], int]:
        """Atomically end a competition if the deadline has passed.

        Uses UPDATE ... WHERE value = 'true' to ensure only one caller succeeds.
        Returns (ended, winner_user_ids, max_winners).
        """
        now_ts = db_now()
        with get_db() as conn:
            cur = conn.execute(
                """UPDATE competition_state
                   SET value = 'false'
                   WHERE key = 'active'
                     AND value = 'true'
                     AND EXISTS (
                         SELECT 1
                         FROM competition_state d
                         WHERE d.key = 'deadline'
                           AND d.value IS NOT NULL
                           AND d.value != ''
                           AND d.value <= ?
                     )""",
                (now_ts,),
            )
            ended = cur.rowcount > 0
            if not ended:
                return False, [], 0

            # We won the race; do cleanup in the same transaction
            rows = conn.execute(
                "SELECT user_id FROM competition_winners ORDER BY won_at"
            ).fetchall()
            winners = [r["user_id"] for r in rows]

            row = conn.execute(
                "SELECT value FROM competition_state WHERE key = 'max_winners'"
            ).fetchone()
            max_winners = int(row["value"]) if row and str(row["value"]).isdigit() else _DEFAULT_MAX_WINNERS

            self._close_active_round_tx(conn, "deadline", now_ts)
            conn.execute("DELETE FROM competition_winners")
        logging.info("Competition deadline reached, auto-ending.")
        return True, winners, max_winners

    def add_winner_with_end_state(
        self,
        user_id: str,
        max_winners: int | None = None,
        first_name: str = "",
        username: str = "",
    ) -> tuple[bool, bool]:
        """
        Try to insert a winner and atomically auto-end if capacity is reached.

        Uses a PostgreSQL advisory lock to serialize concurrent winner insertions,
        ensuring correct winner_rank and preventing count/capacity races.

        Returns:
            (inserted, ended_now)
        """
        if max_winners is None:
            max_winners = self.get_max_winners()
        now_ts = db_now()
        with get_db() as conn:
            # Serialize winner insertions across all processes.
            # Advisory lock is automatically released at transaction end.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('competition_winner'))")

            cur = conn.execute(
                """INSERT INTO competition_winners (user_id, won_at)
                   SELECT ?, ?
                   WHERE (SELECT COUNT(*) FROM competition_winners) < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM competition_winners WHERE user_id = ?
                     )""",
                (user_id, now_ts, max_winners, user_id),
            )
            inserted = cur.rowcount > 0
            if inserted:
                safe_first_name = (first_name or "").strip()
                safe_username = _normalize_username(username)
                if not safe_first_name or not safe_username:
                    known = conn.execute(
                        "SELECT first_name, username FROM known_users WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    if known:
                        if not safe_first_name:
                            safe_first_name = (known.get("first_name") or "").strip()
                        if not safe_username:
                            safe_username = (known.get("username") or "").strip()

                conn.execute(
                    """INSERT INTO competition_winner_log (user_id, first_name, username, won_at)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, safe_first_name, safe_username, now_ts),
                )

                round_id = self._get_active_round_id_tx(conn)
                if round_id is not None:
                    # Rank is accurate here because the advisory lock serializes insertions.
                    rank_row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM competition_winners"
                    ).fetchone()
                    winner_rank = int(rank_row["cnt"] if rank_row else 0)
                    conn.execute(
                        """INSERT INTO competition_round_winners
                           (round_id, user_id, first_name, username, won_at, winner_rank)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(round_id, user_id) DO NOTHING""",
                        (
                            round_id,
                            user_id,
                            safe_first_name,
                            safe_username,
                            now_ts,
                            winner_rank,
                        ),
                    )

            ended_now = conn.execute(
                """UPDATE competition_state
                   SET value = 'false'
                   WHERE key = 'active'
                     AND value = 'true'
                     AND ? <= (SELECT COUNT(*) FROM competition_winners)""",
                (max_winners,),
            ).rowcount > 0
            if ended_now:
                self._close_active_round_tx(conn, "capacity", now_ts)
                conn.execute("DELETE FROM competition_winners")
        if ended_now:
            logging.info("Competition max winners reached, auto-ending.")
        return inserted, ended_now

    def add_winner(self, user_id: str, max_winners: int | None = None) -> bool:
        inserted, _ended_now = self.add_winner_with_end_state(user_id, max_winners)
        return inserted

    def get_winners(self) -> list[str]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id FROM competition_winners ORDER BY won_at"
            ).fetchall()
            return [r["user_id"] for r in rows]

    def get_winner_profiles(self) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT cw.user_id, cw.won_at,
                          COALESCE(NULLIF(ku.first_name, ''), '') AS first_name,
                          COALESCE(NULLIF(ku.username, ''), '') AS username
                   FROM competition_winners cw
                   LEFT JOIN known_users ku ON ku.user_id = cw.user_id
                   ORDER BY cw.won_at, cw.id"""
            ).fetchall()
            return [dict(r) for r in rows]

    def get_current_competition_leaderboard(self, limit: int = 25) -> list[dict]:
        if limit < 1:
            limit = 25
        with get_db() as conn:
            rows = conn.execute(
                """SELECT cw.user_id,
                          cw.won_at,
                          COALESCE(NULLIF(ku.first_name, ''), '') AS first_name,
                          COALESCE(NULLIF(ku.username, ''), '') AS username
                   FROM competition_winners cw
                   LEFT JOIN known_users ku ON ku.user_id = cw.user_id
                   ORDER BY cw.won_at ASC, cw.id ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

        board = [dict(r) for r in rows]
        for idx, row in enumerate(board, 1):
            row["rank"] = idx
        return board

    def get_flag_leaderboard(self, limit: int = 25) -> list[dict]:
        if limit < 1:
            limit = 25
        with get_db() as conn:
            rows = conn.execute(
                """SELECT ff.user_id,
                          1 AS solves,
                          ff.found_at AS first_found_at,
                          ff.found_at AS last_found_at,
                          COALESCE(NULLIF(ff.first_name, ''), COALESCE(NULLIF(ku.first_name, ''), '')) AS first_name,
                          COALESCE(NULLIF(ff.username, ''), COALESCE(NULLIF(ku.username, ''), '')) AS username
                   FROM flag_finders ff
                   LEFT JOIN known_users ku ON ku.user_id = ff.user_id
                   ORDER BY ff.found_at ASC, ff.user_id ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

        board = [dict(r) for r in rows]
        for idx, row in enumerate(board, 1):
            row["rank"] = idx
        return board

    # Backwards-compatible alias used by existing bot command.
    def get_leaderboard(self, limit: int = 25) -> list[dict]:
        return self.get_flag_leaderboard(limit=limit)

    def record_flag_finder(
        self,
        user_id: str,
        first_name: str = "",
        username: str = "",
    ) -> bool:
        safe_first_name = (first_name or "").strip()
        safe_username = _normalize_username(username)
        now_ts = db_now()
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO flag_finders (user_id, first_name, username, found_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO NOTHING""",
                (user_id, safe_first_name, safe_username, now_ts),
            )
            return cur.rowcount > 0

    def get_competition_leaderboard(self, limit: int = 25) -> list[dict]:
        if limit < 1:
            limit = 25
        with get_db() as conn:
            rows = conn.execute(
                """WITH agg AS (
                       SELECT user_id,
                              COUNT(*) AS wins,
                              MIN(won_at) AS first_win_at,
                              MAX(won_at) AS last_win_at,
                              MIN(winner_rank) AS best_rank,
                              COUNT(DISTINCT round_id) AS rounds_won
                       FROM competition_round_winners
                       GROUP BY user_id
                   ),
                   latest AS (
                       SELECT DISTINCT ON (user_id)
                              user_id,
                              COALESCE(first_name, '') AS first_name,
                              COALESCE(username, '') AS username
                       FROM competition_round_winners
                       ORDER BY user_id, won_at DESC, id DESC
                   )
                   SELECT agg.user_id,
                          agg.wins,
                          agg.rounds_won,
                          agg.best_rank,
                          agg.first_win_at,
                          agg.last_win_at,
                          COALESCE(NULLIF(latest.first_name, ''), COALESCE(NULLIF(ku.first_name, ''), '')) AS first_name,
                          COALESCE(NULLIF(latest.username, ''), COALESCE(NULLIF(ku.username, ''), '')) AS username
                   FROM agg
                   LEFT JOIN latest ON latest.user_id = agg.user_id
                   LEFT JOIN known_users ku ON ku.user_id = agg.user_id
                   ORDER BY agg.wins DESC,
                            agg.best_rank ASC NULLS LAST,
                            agg.first_win_at ASC,
                            agg.user_id ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

        board = [dict(r) for r in rows]
        for idx, row in enumerate(board, 1):
            row["rank"] = idx
        return board

    def get_recent_rounds(self, limit: int = 20) -> list[dict]:
        if limit < 1:
            limit = 20
        with get_db() as conn:
            rows = conn.execute(
                """SELECT cr.id, cr.question, cr.reward_message, cr.max_winners,
                          cr.initiated_by, cr.status, cr.end_reason,
                          cr.started_at, cr.ended_at,
                          COALESCE(COUNT(rw.id), 0) AS winner_count
                   FROM competition_rounds cr
                   LEFT JOIN competition_round_winners rw ON rw.round_id = cr.id
                   GROUP BY cr.id
                   ORDER BY cr.started_at DESC, cr.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_round_winners_for_rounds(
        self,
        round_ids: list[int],
        per_round: int = 10,
    ) -> dict[int, list[dict]]:
        if per_round < 1:
            per_round = 10
        clean_ids = [int(rid) for rid in round_ids if str(rid).isdigit()]
        if not clean_ids:
            return {}

        placeholders = ",".join("?" for _ in clean_ids)
        params: tuple = (*clean_ids, per_round)
        with get_db() as conn:
            rows = conn.execute(
                f"""WITH ranked AS (
                        SELECT rw.id, rw.round_id, rw.user_id, rw.won_at, rw.winner_rank,
                               rw.first_name, rw.username,
                               ROW_NUMBER() OVER (
                                   PARTITION BY rw.round_id
                                   ORDER BY COALESCE(rw.winner_rank, 999999), rw.won_at, rw.id
                               ) AS rn
                        FROM competition_round_winners rw
                        WHERE rw.round_id IN ({placeholders})
                    )
                    SELECT ranked.round_id, ranked.user_id, ranked.won_at, ranked.winner_rank,
                           COALESCE(NULLIF(ranked.first_name, ''), COALESCE(NULLIF(ku.first_name, ''), '')) AS first_name,
                           COALESCE(NULLIF(ranked.username, ''), COALESCE(NULLIF(ku.username, ''), '')) AS username
                    FROM ranked
                    LEFT JOIN known_users ku ON ku.user_id = ranked.user_id
                    WHERE ranked.rn <= ?
                    ORDER BY ranked.round_id DESC, ranked.rn ASC""",
                params,
            ).fetchall()

        grouped: dict[int, list[dict]] = {}
        for row in rows:
            rid = int(row["round_id"])
            grouped.setdefault(rid, []).append(dict(row))
        return grouped

    def clear_winners(self):
        with get_db() as conn:
            conn.execute("DELETE FROM competition_winners")

    def check_deadline(self) -> bool:
        """Atomically end competition once when deadline has passed."""
        now_ts = db_now()
        with get_db() as conn:
            cur = conn.execute(
                """UPDATE competition_state
                   SET value = 'false'
                   WHERE key = 'active'
                     AND value = 'true'
                     AND EXISTS (
                         SELECT 1
                         FROM competition_state d
                         WHERE d.key = 'deadline'
                           AND d.value IS NOT NULL
                           AND d.value != ''
                           AND d.value <= ?
                     )""",
                (now_ts,),
            )
            ended = cur.rowcount > 0
            if ended:
                self._close_active_round_tx(conn, "deadline", now_ts)
        if ended:
            logging.info("Competition deadline reached, auto-ending.")
        return ended
