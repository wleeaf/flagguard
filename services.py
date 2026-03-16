"""
Lazy service container.

Services are created on first use to avoid heavy side effects during import.
"""

from config import ADMIN_IDS, validate_required_config

_container = None


class _ServiceContainer:
    def __init__(self):
        from models.user import UserRepository
        from models.conversation import ConversationRepository
        from models.competition import CompetitionRepository
        from models.rate_limiter import RateLimiter
        from models.bot_state import BotStateRepository
        from models.report import ReportRepository
        from models.admin import AdminRepository
        from models.moderation import ModerationRepository
        from models.ctf_flags import CTFFlagRepository
        from security.jailbreak import JailbreakDetector
        from security.honeypot import HoneypotSystem
        from security.sanitizer import OutputSanitizer
        from security.behavior import BehaviorAnalyzer
        from config import CHALLENGE_FLAG

        self.user_repo = UserRepository()
        self.conversation_repo = ConversationRepository()
        self.competition_repo = CompetitionRepository()
        self.rate_limiter = RateLimiter()
        self.bot_state = BotStateRepository()
        self.report_repo = ReportRepository()
        self.admin_repo = AdminRepository()
        self.moderation_repo = ModerationRepository()
        self.ctf_flag_repo = CTFFlagRepository()

        self.detector = JailbreakDetector()
        self.honeypot = HoneypotSystem()
        self.sanitizer = OutputSanitizer(flag=CHALLENGE_FLAG)
        self.analyzer = BehaviorAnalyzer(user_repo=self.user_repo, detector=self.detector)

        # AIEngine imports optional heavy dependencies (google-genai), so keep it lazy.
        self._engine = None

    def get_engine(self):
        if self._engine is None:
            from ai.engine import AIEngine

            self._engine = AIEngine(
                user_repo=self.user_repo,
                conversation_repo=self.conversation_repo,
                rate_limiter=self.rate_limiter,
                detector=self.detector,
                honeypot=self.honeypot,
                sanitizer=self.sanitizer,
                analyzer=self.analyzer,
            )
        return self._engine


def _get_container() -> _ServiceContainer:
    global _container
    if _container is None:
        validate_required_config()
        _container = _ServiceContainer()
    return _container


class _Proxy:
    def __init__(self, key: str):
        super().__setattr__("_key", key)

    def _target(self):
        container = _get_container()
        if self._key == "engine":
            return container.get_engine()
        return getattr(container, self._key)

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)

    def __call__(self, *args, **kwargs):
        return self._target()(*args, **kwargs)


# Re-exported lazy service handles
user_repo = _Proxy("user_repo")
conversation_repo = _Proxy("conversation_repo")
competition_repo = _Proxy("competition_repo")
rate_limiter = _Proxy("rate_limiter")
bot_state = _Proxy("bot_state")
detector = _Proxy("detector")
honeypot = _Proxy("honeypot")
sanitizer = _Proxy("sanitizer")
analyzer = _Proxy("analyzer")
engine = _Proxy("engine")
report_repo = _Proxy("report_repo")
admin_repo = _Proxy("admin_repo")
moderation_repo = _Proxy("moderation_repo")
ctf_flag_repo = _Proxy("ctf_flag_repo")


def is_admin(user_id: int) -> bool:
    """Check env-based ADMIN_IDS first, then fall back to DB telegram_admins."""
    if user_id in ADMIN_IDS:
        return True
    try:
        return admin_repo.is_admin(str(user_id))
    except Exception:
        return False


async def close_engine_if_initialized() -> None:
    """Close AI engine only if it was already initialized."""
    global _container
    if _container is None:
        return
    engine = _container._engine
    if engine is None:
        return
    await engine.aclose()
    _container._engine = None
