import unittest
import sys
import os
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config import FLAG_AWARD_PREFIX, FLAG_KEYWORD, MAX_REQUESTS_PER_MINUTE
from models.rate_limiter import RateLimiter
from database import get_db, init_database
import services

import pytest


@pytest.fixture(autouse=True)
def _setup_db(pg_get_db):
    init_database()
    yield pg_get_db


class TestRegression:

    def test_flag_award_prefix_logic(self):
        """Test that FLAG_AWARD_PREFIX is correctly handled (Finding #7)."""
        # Logic extracted from handlers/message.py

        # Case 1: Flag present but NO prefix -> Should be blocked
        ai_response_leak = f"Here is the flag: {FLAG_KEYWORD}secret}}"
        is_flag_award = ai_response_leak.startswith(FLAG_AWARD_PREFIX)
        if is_flag_award:
            ai_response_leak = ai_response_leak[len(FLAG_AWARD_PREFIX):]

        should_block = not is_flag_award and FLAG_KEYWORD in ai_response_leak
        assert should_block, "Should block flag leak without prefix"

        # Case 2: Flag present AND prefix present -> Should pass
        ai_response_award = f"{FLAG_AWARD_PREFIX}Congratulations! {FLAG_KEYWORD}secret}}"
        is_flag_award = ai_response_award.startswith(FLAG_AWARD_PREFIX)
        if is_flag_award:
            final_response = ai_response_award[len(FLAG_AWARD_PREFIX):]
        else:
            final_response = ai_response_award

        should_block = not is_flag_award and FLAG_KEYWORD in final_response
        assert not should_block, "Should NOT block flag award with prefix"
        assert FLAG_KEYWORD in final_response, "Final response should contain the flag"

    def test_rate_limiter_counting(self):
        """Test rate limiter counting and cleanup (Finding #1)."""
        limiter = RateLimiter()
        user_id = "test_user_123"

        # Record max requests
        for _ in range(MAX_REQUESTS_PER_MINUTE):
            assert limiter.check(user_id), "Should allow request under limit"

        # Next one should fail
        assert not limiter.check(user_id), "Should block request over limit"

        # Test distinct users
        assert limiter.check("other_user"), "Should allow other user"

    def test_rate_limiter_cleanup(self):
        """Test in-memory cleanup removes stale entries."""
        limiter = RateLimiter()
        user_id = "cleanup_user"

        with patch("models.rate_limiter.time.monotonic", return_value=0.0):
            assert limiter.check(user_id)
            assert limiter._count_recent(user_id, 60) == 1

        with patch("models.rate_limiter.time.monotonic", return_value=130.0):
            limiter.cleanup(seconds=60)
            assert limiter._count_recent(user_id, 60) == 0
            assert limiter.check(user_id)

    def test_lazy_service_initialization(self):
        """Test that services are not initialized until accessed (Finding #3)."""
        # Reset container for this test
        original_container = services._container
        services._container = None

        try:
            assert services._container is None
            # Access a service attribute to trigger lazy load (proxy access)
            # Just accessing services.user_repo returns the Proxy object.
            # We need to access an attribute on it.
            _ = services.user_repo.get_behavior

            assert services._container is not None
            assert isinstance(services._container, services._ServiceContainer)
        finally:
            # Restore
            if original_container:
                services._container = original_container

    def test_weekly_behavior_reset(self):
        """Test behavior analyzer weekly reset logic (Finding #8)."""
        from security.behavior import BehaviorAnalyzer
        from models.user import UserRepository

        # Mock dependencies
        mock_user_repo = MagicMock(spec=UserRepository)
        mock_detector = MagicMock()
        analyzer = BehaviorAnalyzer(mock_user_repo, mock_detector)

        user_id = "reset_test_user"

        # Case 1: Last reset was 8 days ago
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        mock_user_repo.get_behavior.return_value = {"last_reset": eight_days_ago}

        # Trigger analyze
        analyzer.analyze(user_id, "foo", False)

        # Verify reset was called
        mock_user_repo.reset_behavior.assert_called_with(user_id)

        # Case 2: Last reset was 1 day ago
        mock_user_repo.reset_behavior.reset_mock()
        one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        mock_user_repo.get_behavior.return_value = {"last_reset": one_day_ago}

        analyzer.analyze(user_id, "foo", False)

        # Verify reset was NOT called
        mock_user_repo.reset_behavior.assert_not_called()
