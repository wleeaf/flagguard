import logging
from datetime import timedelta

from models.user import UserRepository
from security.jailbreak import JailbreakDetector
from time_utils import now_local, parse_db_timestamp


class BehaviorAnalyzer:
    """Tracks per-user suspicion scores based on message patterns."""

    def __init__(self, user_repo: UserRepository, detector: JailbreakDetector):
        self.users = user_repo
        self.detector = detector

    def analyze(self, user_id: str, text: str, is_jailbreak: bool):
        score_delta = 0
        jailbreak_delta = 1 if is_jailbreak else 0

        if is_jailbreak:
            score_delta += 10

        if len(text) > 300:
            score_delta += 1
        if text.count('?') > 3:
            score_delta += 2
        if any(c in text for c in ['|', '{', '}', '$', '`']):
            score_delta += 3
        if self.detector.check_delimiters(text):
            score_delta += 5

        # Single DB call: upsert + return updated row for ratio check.
        behavior = self.users.batch_update_behavior(
            user_id,
            total_delta=1,
            jailbreak_delta=jailbreak_delta,
            score_delta=score_delta,
        )

        # Jailbreak ratio check on returned data (if available).
        if behavior:
            total = behavior.get("total_requests", 1)
            jailbreaks = behavior.get("jailbreak_attempts", 0)
            if total > 5 and jailbreaks / max(total, 1) > 0.5:
                self.users.batch_update_behavior(user_id, total_delta=0, jailbreak_delta=0, score_delta=15)

            new_score = behavior.get("suspicious_score", 0)
            if new_score > 60:
                logging.warning("HIGH SUSPICION: User %s - Score: %s", user_id, new_score)
