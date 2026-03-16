import os
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# ⚙️ TELEGRAM CONFIGURATION
# ==========================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# ==========================================
# 👑 ADMIN CONFIGURATION
# ==========================================

_raw_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: list[int] = []

if _raw_admin_ids:
    for raw_id in _raw_admin_ids.split(","):
        raw_id = raw_id.strip()
        if raw_id.isdigit():
            ADMIN_IDS.append(int(raw_id))

if not ADMIN_IDS:
    print("⚠️ WARNING: No valid ADMIN_IDS found in .env. Admin commands will be disabled.")

# ==========================================
# 🔐 CHALLENGE CONFIGURATION
# ==========================================

CHALLENGE_FLAG = os.getenv("CHALLENGE_FLAG")

BOT_NAME = os.getenv("BOT_NAME", "AIShield").strip()
FLAG_PREFIX = os.getenv("FLAG_PREFIX", "FLAG").strip()

FLAG_KEYWORD = f"{FLAG_PREFIX}{{"
FLAG_AWARD_PREFIX = "__FLAG_AWARD__:"

# ==========================================
# 🤖 AI / VERTEX AI CONFIGURATION
# ==========================================

SERVICE_ACCOUNT_PATH = os.getenv("SERVICE_ACCOUNT_PATH")
if SERVICE_ACCOUNT_PATH and not os.path.isabs(SERVICE_ACCOUNT_PATH):
    # Resolve relative to project root (where config.py resides)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    SERVICE_ACCOUNT_PATH = os.path.join(base_dir, SERVICE_ACCOUNT_PATH)

GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.0-flash")
AI_API_TIMEOUT_SECONDS = float(os.getenv("AI_API_TIMEOUT_SECONDS", "15"))
AI_RETRY_ATTEMPTS = int(os.getenv("AI_RETRY_ATTEMPTS", "2"))
AI_RETRY_BASE_DELAY_SECONDS = float(os.getenv("AI_RETRY_BASE_DELAY_SECONDS", "1.0"))
AI_CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("AI_CIRCUIT_BREAKER_THRESHOLD", "10"))
AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(os.getenv("AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60"))
_raw_ai_max_concurrent = os.getenv("AI_MAX_CONCURRENT_REQUESTS")
if _raw_ai_max_concurrent is None:
    _is_prod_webhook_for_ai = (
        os.getenv("APP_ENV", "development").strip().lower() == "production"
        and os.getenv("BOT_MODE", "polling").strip().lower() == "webhook"
    )
    # Keep per-process concurrency conservative by default in multi-worker prod deployments
    # to cap memory (attachments) and DB/thread pressure.
    AI_MAX_CONCURRENT_REQUESTS = 8 if _is_prod_webhook_for_ai else 50
else:
    AI_MAX_CONCURRENT_REQUESTS = int(_raw_ai_max_concurrent)
AI_DB_MAX_CONCURRENT_OPS = int(os.getenv("AI_DB_MAX_CONCURRENT_OPS", "128"))
AI_DEFERRED_WRITE_WORKERS = int(os.getenv("AI_DEFERRED_WRITE_WORKERS", "8"))
AI_DEFERRED_WRITE_QUEUE_SIZE = int(os.getenv("AI_DEFERRED_WRITE_QUEUE_SIZE", "5000"))
KNOWN_USER_UPSERT_INTERVAL_SECONDS = float(os.getenv("KNOWN_USER_UPSERT_INTERVAL_SECONDS", "60"))
TELEGRAM_SEND_CONCURRENCY = int(os.getenv("TELEGRAM_SEND_CONCURRENCY", "20"))
TELEGRAM_MIN_SEND_INTERVAL_SECONDS = float(
    os.getenv("TELEGRAM_MIN_SEND_INTERVAL_SECONDS", "0.04")
)
TELEGRAM_SEND_MAX_RETRIES = int(os.getenv("TELEGRAM_SEND_MAX_RETRIES", "3"))
_raw_ai_global_rpm_limit = os.getenv("AI_GLOBAL_RPM_LIMIT")
if _raw_ai_global_rpm_limit is None:
    _default_rpm = (
        "800" if os.getenv("APP_ENV", "development").strip().lower() == "production" else "0"
    )
    AI_GLOBAL_RPM_LIMIT = int(_default_rpm)
else:
    AI_GLOBAL_RPM_LIMIT = int(_raw_ai_global_rpm_limit)

# ==========================================
# 🛡️ RATE LIMITING & INPUT LIMITS
# ==========================================

RATE_LIMIT_SECONDS = 0.5
MAX_REQUESTS_PER_MINUTE = 10
LOGIC_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LOGIC_RATE_LIMIT_WINDOW_SECONDS", "60"))
MAX_INPUT_LENGTH = 1200
AI_OVERLOAD_QUEUE_THRESHOLD = int(os.getenv("AI_OVERLOAD_QUEUE_THRESHOLD", "200"))
AI_OVERLOAD_REPLY_TEXT = os.getenv(
    "AI_OVERLOAD_REPLY_TEXT",
    "⏳ Sistem şu an yoğun. Lütfen biraz sonra tekrar dene.",
).strip()

# ==========================================
# 📎 ATTACHMENT LIMITS
# ==========================================

MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = int(
    os.getenv("MAX_TOTAL_ATTACHMENT_BYTES", str(MAX_ATTACHMENT_BYTES * MAX_ATTACHMENTS))
)

# ==========================================
# 🏆 COMPETITION DEFAULTS
# ==========================================

MAX_WINNERS = 5

# ==========================================
# 💾 DATABASE CONFIGURATION
# ==========================================

DATABASE_URL = os.getenv("DATABASE_URL", "")
_raw_db_pool_min = os.getenv("DATABASE_POOL_MIN_SIZE")
_raw_db_pool_max = os.getenv("DATABASE_POOL_MAX_SIZE")
_is_prod_webhook = (
    os.getenv("APP_ENV", "development").strip().lower() == "production"
    and os.getenv("BOT_MODE", "polling").strip().lower() == "webhook"
)

if _raw_db_pool_min is None:
    DATABASE_POOL_MIN_SIZE = 2 if _is_prod_webhook else 5
else:
    DATABASE_POOL_MIN_SIZE = int(_raw_db_pool_min)

if _raw_db_pool_max is None:
    # For multi-worker webhook deployments use a moderate pool per process.
    # Deploy PgBouncer in transaction-pooling mode when running many workers
    # to avoid exhausting PostgreSQL max_connections.
    DATABASE_POOL_MAX_SIZE = 25 if _is_prod_webhook else 30
else:
    DATABASE_POOL_MAX_SIZE = int(_raw_db_pool_max)

# Rate-limiter backend: memory | database
# "memory" uses an efficient per-process in-memory sliding window (recommended).
# "database" uses PostgreSQL-backed counting (cross-worker consistent but adds
# significant DB pressure at high concurrency — only use with Redis alternative).
RATE_LIMIT_BACKEND = os.getenv("RATE_LIMIT_BACKEND", "memory").strip().lower()

# ==========================================
# 🖥️ PANEL CONFIGURATION
# ==========================================

PANEL_SECRET_KEY = os.getenv("PANEL_SECRET_KEY", "change-me-in-production")
PANEL_PORT = int(os.getenv("PANEL_PORT", "8000"))
PANEL_HOST = os.getenv("PANEL_HOST", "127.0.0.1")
PANEL_TOKEN_EXPIRE_HOURS = int(os.getenv("PANEL_TOKEN_EXPIRE_HOURS", "8"))
PANEL_COOKIE_SECURE = os.getenv("PANEL_COOKIE_SECURE", "true").lower() == "true"
PANEL_WORKERS = int(os.getenv("PANEL_WORKERS", "1"))
PANEL_BACKUP_DIR = os.getenv("PANEL_BACKUP_DIR", "backups")
PANEL_BACKUP_RETENTION = int(os.getenv("PANEL_BACKUP_RETENTION", "20"))
PANEL_BACKUP_TIMEOUT_SECONDS = int(os.getenv("PANEL_BACKUP_TIMEOUT_SECONDS", "180"))

# ==========================================
# 🌐 BOT MODE CONFIGURATION
# ==========================================

BOT_MODE = os.getenv("BOT_MODE", "polling")  # "polling" or "webhook"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8443"))
WEBHOOK_REUSE_PORT = os.getenv("WEBHOOK_REUSE_PORT", "true").lower() == "true"
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip()
WEBHOOK_MAX_CONNECTIONS = int(os.getenv("WEBHOOK_MAX_CONNECTIONS", "40"))
WEBHOOK_REGISTER_ON_START = os.getenv("WEBHOOK_REGISTER_ON_START", "true").lower() == "true"
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()

# ==========================================
# 🕒 GLOBAL TIMEZONE CONFIGURATION
# ==========================================

# Applied to application-generated timestamps (DB writes, logs, audit records).
# Format: +HH:MM / -HH:MM  (example: +03:00)
APP_TIMEZONE_OFFSET = os.getenv("APP_TIMEZONE_OFFSET", "+00:00")

# ==========================================
# 📈 METRICS (OPTIONAL MULTI-PROCESS EXPORT)
# ==========================================

# When non-empty, each process periodically writes a metrics snapshot to this directory,
# and /metrics can aggregate across workers (useful with reuse_port / multiple uvicorn workers).
METRICS_EXPORT_DIR = os.getenv("METRICS_EXPORT_DIR", f"/tmp/{BOT_NAME.lower()}-metrics").strip()
METRICS_EXPORT_INTERVAL_SECONDS = float(os.getenv("METRICS_EXPORT_INTERVAL_SECONDS", "5"))
METRICS_EXPORT_MAX_AGE_SECONDS = float(os.getenv("METRICS_EXPORT_MAX_AGE_SECONDS", "30"))

# ==========================================
# 🧾 MESSAGE LOGGING (HOT PATH HARDENING)
# ==========================================

# If enabled, MessageLogMiddleware enqueues writes and a background worker flushes to PostgreSQL in batches.
MESSAGE_LOG_ASYNC = os.getenv("MESSAGE_LOG_ASYNC", "true").lower() == "true"
MESSAGE_LOG_QUEUE_SIZE = int(os.getenv("MESSAGE_LOG_QUEUE_SIZE", "5000"))
MESSAGE_LOG_BATCH_SIZE = int(os.getenv("MESSAGE_LOG_BATCH_SIZE", "200"))
MESSAGE_LOG_FLUSH_INTERVAL_SECONDS = float(os.getenv("MESSAGE_LOG_FLUSH_INTERVAL_SECONDS", "0.2"))

# ==========================================
# 🧵 THREADPOOL TUNING
# ==========================================

# Controls asyncio's default ThreadPoolExecutor size for `asyncio.to_thread(...)` offloads.
_raw_threadpool_max = os.getenv("BOT_THREADPOOL_MAX_WORKERS")
if _raw_threadpool_max is None:
    # Size the thread pool to match DB pool + headroom for non-DB blocking work.
    # All DB calls flow through asyncio.to_thread(), so this is the primary
    # throughput limiter.  Increase if you see ai_semaphore_wait_seconds climbing.
    BOT_THREADPOOL_MAX_WORKERS = 32 if _is_prod_webhook else 64
else:
    BOT_THREADPOOL_MAX_WORKERS = int(_raw_threadpool_max)

# ==========================================
# 🪵 LOGGING
# ==========================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
BOT_LOG_FILE = os.getenv("BOT_LOG_FILE", "").strip()

# ==========================================
# 🗑️ LOG RETENTION
# ==========================================

LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))

# ==========================================
# 🔗 FLAG SUCCESS URL
# ==========================================

FLAG_SUCCESS_URL = os.getenv("FLAG_SUCCESS_URL", "")


def validate_panel_config() -> None:
    """Validate panel security-related configuration."""
    if PANEL_SECRET_KEY == "change-me-in-production" and not os.getenv("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            "PANEL_SECRET_KEY is still the default value. "
            "Set a strong, unique secret in your .env file."
        )
    if PANEL_TOKEN_EXPIRE_HOURS < 1:
        raise RuntimeError("PANEL_TOKEN_EXPIRE_HOURS must be >= 1")
    if PANEL_WORKERS < 1:
        raise RuntimeError("PANEL_WORKERS must be >= 1")
    if PANEL_BACKUP_RETENTION < 0:
        raise RuntimeError("PANEL_BACKUP_RETENTION must be >= 0")
    if PANEL_BACKUP_TIMEOUT_SECONDS < 1:
        raise RuntimeError("PANEL_BACKUP_TIMEOUT_SECONDS must be >= 1")


def validate_required_config() -> None:
    """Validate runtime-required environment variables."""
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not CHALLENGE_FLAG:
        missing.append("CHALLENGE_FLAG")
    if not SERVICE_ACCOUNT_PATH:
        missing.append("SERVICE_ACCOUNT_PATH")

    if missing:
        raise RuntimeError(f"Missing required config keys: {', '.join(missing)}")

    if AI_API_TIMEOUT_SECONDS <= 0:
        raise RuntimeError("AI_API_TIMEOUT_SECONDS must be > 0")
    if AI_RETRY_ATTEMPTS < 0:
        raise RuntimeError("AI_RETRY_ATTEMPTS must be >= 0")
    if AI_RETRY_BASE_DELAY_SECONDS <= 0:
        raise RuntimeError("AI_RETRY_BASE_DELAY_SECONDS must be > 0")
    if AI_CIRCUIT_BREAKER_THRESHOLD <= 0:
        raise RuntimeError("AI_CIRCUIT_BREAKER_THRESHOLD must be > 0")
    if AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS <= 0:
        raise RuntimeError("AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS must be > 0")
    if AI_MAX_CONCURRENT_REQUESTS <= 0:
        raise RuntimeError("AI_MAX_CONCURRENT_REQUESTS must be > 0")
    if AI_GLOBAL_RPM_LIMIT < 0:
        raise RuntimeError("AI_GLOBAL_RPM_LIMIT must be >= 0")
    if AI_DB_MAX_CONCURRENT_OPS <= 0:
        raise RuntimeError("AI_DB_MAX_CONCURRENT_OPS must be > 0")
    if AI_DEFERRED_WRITE_WORKERS <= 0:
        raise RuntimeError("AI_DEFERRED_WRITE_WORKERS must be > 0")
    if AI_DEFERRED_WRITE_QUEUE_SIZE <= 0:
        raise RuntimeError("AI_DEFERRED_WRITE_QUEUE_SIZE must be > 0")
    if TELEGRAM_SEND_CONCURRENCY <= 0:
        raise RuntimeError("TELEGRAM_SEND_CONCURRENCY must be > 0")
    if TELEGRAM_MIN_SEND_INTERVAL_SECONDS < 0:
        raise RuntimeError("TELEGRAM_MIN_SEND_INTERVAL_SECONDS must be >= 0")
    if TELEGRAM_SEND_MAX_RETRIES < 0:
        raise RuntimeError("TELEGRAM_SEND_MAX_RETRIES must be >= 0")
    if KNOWN_USER_UPSERT_INTERVAL_SECONDS <= 0:
        raise RuntimeError("KNOWN_USER_UPSERT_INTERVAL_SECONDS must be > 0")
    if LOGIC_RATE_LIMIT_WINDOW_SECONDS <= 0:
        raise RuntimeError("LOGIC_RATE_LIMIT_WINDOW_SECONDS must be > 0")
    if AI_OVERLOAD_QUEUE_THRESHOLD <= 0:
        raise RuntimeError("AI_OVERLOAD_QUEUE_THRESHOLD must be > 0")
    if not AI_OVERLOAD_REPLY_TEXT:
        raise RuntimeError("AI_OVERLOAD_REPLY_TEXT must be non-empty")
    if MAX_TOTAL_ATTACHMENT_BYTES <= 0:
        raise RuntimeError("MAX_TOTAL_ATTACHMENT_BYTES must be > 0")
    if BOT_MODE not in {"polling", "webhook"}:
        raise RuntimeError("BOT_MODE must be 'polling' or 'webhook'")
    if BOT_MODE == "webhook":
        if not WEBHOOK_URL:
            raise RuntimeError("WEBHOOK_URL is required when BOT_MODE=webhook")
        if not WEBHOOK_URL.startswith(("https://", "http://")):
            raise RuntimeError("WEBHOOK_URL must start with http:// or https://")
        if APP_ENV == "production" and not WEBHOOK_URL.startswith("https://"):
            raise RuntimeError("WEBHOOK_URL must start with https:// in production webhook mode")
        if not WEBHOOK_PATH.startswith("/"):
            raise RuntimeError("WEBHOOK_PATH must start with '/'")
        if WEBHOOK_PORT <= 0:
            raise RuntimeError("WEBHOOK_PORT must be > 0")
        if WEBHOOK_MAX_CONNECTIONS < 1 or WEBHOOK_MAX_CONNECTIONS > 100:
            raise RuntimeError("WEBHOOK_MAX_CONNECTIONS must be 1..100")
        if APP_ENV == "production":
            if not WEBHOOK_SECRET_TOKEN:
                raise RuntimeError("WEBHOOK_SECRET_TOKEN is required when BOT_MODE=webhook in production")
            if len(WEBHOOK_SECRET_TOKEN) < 16:
                raise RuntimeError("WEBHOOK_SECRET_TOKEN must be at least 16 characters")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    if DATABASE_POOL_MIN_SIZE <= 0:
        raise RuntimeError("DATABASE_POOL_MIN_SIZE must be > 0")
    if DATABASE_POOL_MAX_SIZE < DATABASE_POOL_MIN_SIZE:
        raise RuntimeError("DATABASE_POOL_MAX_SIZE must be >= DATABASE_POOL_MIN_SIZE")
    if RATE_LIMIT_BACKEND not in {"auto", "memory", "database"}:
        raise RuntimeError("RATE_LIMIT_BACKEND must be auto | memory | database")
    if LOG_RETENTION_DAYS < 1:
        raise RuntimeError("LOG_RETENTION_DAYS must be >= 1")

    if METRICS_EXPORT_INTERVAL_SECONDS <= 0:
        raise RuntimeError("METRICS_EXPORT_INTERVAL_SECONDS must be > 0")
    if METRICS_EXPORT_MAX_AGE_SECONDS <= 0:
        raise RuntimeError("METRICS_EXPORT_MAX_AGE_SECONDS must be > 0")
    if MESSAGE_LOG_QUEUE_SIZE <= 0:
        raise RuntimeError("MESSAGE_LOG_QUEUE_SIZE must be > 0")
    if MESSAGE_LOG_BATCH_SIZE <= 0:
        raise RuntimeError("MESSAGE_LOG_BATCH_SIZE must be > 0")
    if MESSAGE_LOG_FLUSH_INTERVAL_SECONDS <= 0:
        raise RuntimeError("MESSAGE_LOG_FLUSH_INTERVAL_SECONDS must be > 0")
    if BOT_THREADPOOL_MAX_WORKERS <= 0:
        raise RuntimeError("BOT_THREADPOOL_MAX_WORKERS must be > 0")
    if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise RuntimeError("LOG_LEVEL must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL")

    validate_panel_config()

    if SERVICE_ACCOUNT_PATH and not os.path.isfile(SERVICE_ACCOUNT_PATH):
        raise RuntimeError(f"SERVICE_ACCOUNT_PATH file not found: {SERVICE_ACCOUNT_PATH}")
