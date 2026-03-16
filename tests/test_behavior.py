"""Tests for the BehaviorAnalyzer scoring logic."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import MagicMock
from datetime import timedelta

from security.behavior import BehaviorAnalyzer
from security.jailbreak import JailbreakDetector
from models.user import UserRepository
from database import init_database, get_db
from time_utils import now_local, db_now


@pytest.fixture
def _setup_db(pg_get_db):
    init_database()
    return pg_get_db


@pytest.fixture
def analyzer(_setup_db):
    user_repo = UserRepository()
    detector = JailbreakDetector()
    return BehaviorAnalyzer(user_repo, detector)


def test_jailbreak_increases_score(analyzer):
    analyzer.analyze("user1", "normal text", is_jailbreak=True)
    behavior = analyzer.users.get_behavior("user1")
    assert behavior["jailbreak_attempts"] >= 1
    assert behavior["suspicious_score"] >= 10


def test_suspicious_chars_increase_score(analyzer):
    analyzer.analyze("user2", "test | { } $ `", is_jailbreak=False)
    behavior = analyzer.users.get_behavior("user2")
    assert behavior["suspicious_score"] >= 3


def test_weekly_reset_triggers_after_7_days(analyzer, _setup_db):
    pg_get_db = _setup_db
    # First analyze to create the user
    analyzer.analyze("user3", "hello", is_jailbreak=True)
    behavior = analyzer.users.get_behavior("user3")
    assert behavior["jailbreak_attempts"] >= 1

    # Fake the last_reset to 8 days ago
    old_time = (now_local() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    with pg_get_db() as conn:
        conn.execute(
            "UPDATE user_behavior SET last_reset = ? WHERE user_id = ?",
            (old_time, "user3"),
        )

    # Next analyze should reset then re-analyze
    analyzer.analyze("user3", "hello again", is_jailbreak=False)
    behavior = analyzer.users.get_behavior("user3")
    # After reset, jailbreak_attempts should be 0
    assert behavior["jailbreak_attempts"] == 0


def test_no_reset_before_7_days(analyzer):
    analyzer.analyze("user4", "hello", is_jailbreak=True)
    behavior = analyzer.users.get_behavior("user4")
    assert behavior["jailbreak_attempts"] >= 1

    analyzer.analyze("user4", "hello again", is_jailbreak=False)
    behavior = analyzer.users.get_behavior("user4")
    # Should not have been reset
    assert behavior["jailbreak_attempts"] >= 1


def test_high_jailbreak_ratio_triggers_bonus(analyzer):
    # Create user with high jailbreak ratio (>50%)
    # First request is jailbreak
    analyzer.analyze("user5", "ignore instructions", is_jailbreak=True)
    behavior = analyzer.users.get_behavior("user5")
    # With 1 request, 1 jailbreak -> ratio = 100% > 50% -> +15 bonus
    assert behavior["suspicious_score"] >= 25  # 10 (jailbreak) + 15 (ratio bonus)
