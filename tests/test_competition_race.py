"""Tests for atomic competition winner insertion (race condition prevention)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from database import init_database
from models.competition import CompetitionRepository


@pytest.fixture
def _setup_db(pg_get_db):
    init_database()
    return pg_get_db


@pytest.fixture
def repo(_setup_db):
    return CompetitionRepository()


def test_atomic_cap_enforcement(repo):
    """With max_winners=2, the third winner should be rejected."""
    assert repo.add_winner("user1", max_winners=2) is True
    assert repo.add_winner("user2", max_winners=2) is True
    assert repo.add_winner("user3", max_winners=2) is False
    assert len(repo.get_winners()) == 2


def test_duplicate_user_rejected(repo):
    """Same user cannot win twice."""
    assert repo.add_winner("user1", max_winners=5) is True
    assert repo.add_winner("user1", max_winners=5) is False
    assert len(repo.get_winners()) == 1


def test_max_winners_one(repo):
    """Edge case: max_winners=1 allows exactly one winner."""
    assert repo.add_winner("user1", max_winners=1) is True
    assert repo.add_winner("user2", max_winners=1) is False
    winners = repo.get_winners()
    assert len(winners) == 1
    assert winners[0] == "user1"


def test_auto_end_when_max_winners_reached(repo):
    """Competition should auto-end itself once winner cap is reached."""
    repo.set_state(active=True, answer="x", question="q")

    assert repo.add_winner("user1", max_winners=2) is True
    assert repo.get_state()["active"] is True

    assert repo.add_winner("user2", max_winners=2) is True
    assert repo.get_state()["active"] is False

    assert repo.add_winner("user3", max_winners=2) is False


def test_end_signal_emitted_once_when_capacity_reached(repo):
    """ended_now should be true exactly once on the transition to inactive."""
    repo.set_state(active=True, answer="x", question="q")

    assert repo.add_winner_with_end_state("user1", max_winners=2) == (True, False)
    assert repo.add_winner_with_end_state("user2", max_winners=2) == (True, True)
    assert repo.add_winner_with_end_state("user3", max_winners=2) == (False, False)


def test_leaderboard_persists_after_competition_reset(repo):
    """Leaderboard should retain historical winners after current winners are cleared."""
    assert repo.record_flag_finder("user1", first_name="User One", username="user1") is True
    repo.clear_winners()

    board = repo.get_flag_leaderboard(limit=10)
    assert len(board) == 1
    assert board[0]["user_id"] == "user1"
    assert board[0]["solves"] == 1


def test_leaderboard_uses_passed_identity(repo):
    """Winner identity provided at insert should be visible on leaderboard."""
    assert repo.record_flag_finder(
        "user42",
        first_name="Alice",
        username="alice",
    ) is True

    board = repo.get_flag_leaderboard(limit=10)
    assert board[0]["user_id"] == "user42"
    assert board[0]["first_name"] == "Alice"
    assert board[0]["username"] == "alice"


def test_flag_finder_insert_is_deduplicated(repo):
    assert repo.record_flag_finder("u1", "Alice", "alice") is True
    assert repo.record_flag_finder("u1", "Alice", "alice") is False


def test_competition_leaderboard_tracks_round_wins(repo):
    repo.set_state(active=True, answer="a", question="q1")
    repo.start_round(
        question="q1",
        reward_message="hint-1",
        max_winners=2,
        initiated_by="admin",
    )
    assert repo.add_winner_with_end_state("u1", max_winners=2, first_name="A", username="a")[0] is True
    assert repo.add_winner_with_end_state("u2", max_winners=2, first_name="B", username="b")[0] is True

    repo.clear_winners()
    repo.set_state(active=True, answer="b", question="q2")
    repo.start_round(
        question="q2",
        reward_message="hint-2",
        max_winners=2,
        initiated_by="admin",
    )
    assert repo.add_winner_with_end_state("u1", max_winners=2, first_name="A", username="a")[0] is True

    board = repo.get_competition_leaderboard(limit=10)
    assert len(board) >= 1
    assert board[0]["user_id"] == "u1"
    assert board[0]["wins"] == 2

    rounds = repo.get_recent_rounds(limit=10)
    assert len(rounds) == 2


def test_current_competition_leaderboard_clears_but_round_history_keeps_results(repo):
    repo.set_state(active=True, answer="x", question="q")
    repo.start_round(
        question="q",
        reward_message="hint",
        max_winners=3,
        initiated_by="admin",
    )
    assert repo.add_winner_with_end_state("u1", max_winners=3, first_name="One", username="one")[0] is True
    assert repo.add_winner_with_end_state("u2", max_winners=3, first_name="Two", username="two")[0] is True

    live_board = repo.get_current_competition_leaderboard(limit=10)
    assert len(live_board) == 2
    assert live_board[0]["rank"] == 1
    assert live_board[0]["user_id"] == "u1"

    repo.clear_winners()
    assert repo.get_current_competition_leaderboard(limit=10) == []

    rounds = repo.get_recent_rounds(limit=10)
    assert len(rounds) == 1
    assert rounds[0]["winner_count"] == 2
