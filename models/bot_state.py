import random as _random
import time as _time
import threading

import config as _config
from ai.difficulty import DEFAULT_DIFFICULTY, parse_difficulty_order, normalize_difficulty
from database import get_db

# Module-level cache shared across all BotStateRepository instances.
_state_cache: dict[str, str] = {}
_cache_time: float = 0
_CACHE_TTL_BASE: float = 5.0  # seconds
_CACHE_TTL_JITTER: float = 1.0  # 0-1s random jitter to desynchronize workers
_cache_lock = threading.Lock()


def _refresh_cache():
    global _state_cache, _cache_time
    now = _time.time()
    ttl = _CACHE_TTL_BASE + _random.random() * _CACHE_TTL_JITTER
    with _cache_lock:
        if now - _cache_time <= ttl:
            return
        # Mark as refreshing so other threads see a recent timestamp
        # and don't redundantly refresh while we hold the lock.
        _cache_time = now
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM bot_state").fetchall()
        fresh = {r["key"]: r["value"] for r in rows}
    with _cache_lock:
        _state_cache = fresh


class BotStateRepository:
    """Persisted key-value store for bot mode toggles with in-memory TTL cache."""

    def get(self, key: str, default: str = "false") -> str:
        _refresh_cache()
        with _cache_lock:
            return _state_cache.get(key, default)

    def set(self, key: str, value: str):
        global _state_cache, _cache_time
        with get_db() as conn:
            conn.execute(
                """INSERT INTO bot_state (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
        # Update cache immediately on write
        with _cache_lock:
            _state_cache[key] = value
            _cache_time = _time.time()

    def atomic_increment(self, key: str) -> int:
        """Atomically increment an integer key and return the new value.

        Used for shared counters (e.g. circuit breaker failure count) that must
        be consistent across multiple processes.
        """
        with get_db() as conn:
            row = conn.execute(
                """INSERT INTO bot_state (key, value) VALUES (?, '1')
                   ON CONFLICT(key) DO UPDATE
                       SET value = (COALESCE(NULLIF(bot_state.value, ''), '0')::INTEGER + 1)::TEXT
                   RETURNING value""",
                (key,),
            ).fetchone()
            new_val = int(row["value"]) if row else 1
        with _cache_lock:
            _state_cache[key] = str(new_val)
            # Don't update _cache_time to avoid masking other changes
        return new_val

    def delete(self, key: str):
        global _state_cache, _cache_time
        with get_db() as conn:
            conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))
        with _cache_lock:
            _state_cache.pop(key, None)
            _cache_time = _time.time()

    def get_bool(self, key: str, default: bool = False) -> bool:
        return self.get(key, "true" if default else "false") == "true"

    def set_bool(self, key: str, value: bool):
        self.set(key, "true" if value else "false")

    @property
    def maintenance_mode(self) -> bool:
        return self.get_bool("maintenance_mode")

    @maintenance_mode.setter
    def maintenance_mode(self, value: bool):
        self.set_bool("maintenance_mode", value)

    @property
    def silent_mode(self) -> bool:
        return self.get_bool("silent_mode")

    @silent_mode.setter
    def silent_mode(self, value: bool):
        self.set_bool("silent_mode", value)

    @property
    def winner_notify_target(self) -> str:
        """Return 'everyone', 'admins', or 'none'.

        Legacy boolean values are mapped for backward compat:
        'true' -> 'everyone', 'false' -> 'admins'.
        """
        raw = self.get("winner_broadcast_mode", "everyone")
        if raw == "true":
            return "everyone"
        if raw == "false":
            return "admins"
        if raw in ("everyone", "admins", "none"):
            return raw
        return "everyone"

    @winner_notify_target.setter
    def winner_notify_target(self, value: str):
        if value not in ("everyone", "admins", "none"):
            raise ValueError(f"Invalid winner_notify_target: {value!r}")
        self.set("winner_broadcast_mode", value)

    @property
    def ai_difficulty_level(self) -> str:
        raw = self.get("ai_difficulty_level", DEFAULT_DIFFICULTY)
        normalized = normalize_difficulty(raw)
        if normalized != raw:
            self.set("ai_difficulty_level", normalized)
        return normalized

    @ai_difficulty_level.setter
    def ai_difficulty_level(self, value: str):
        self.set("ai_difficulty_level", normalize_difficulty(value))

    @property
    def ai_difficulty_order(self) -> list[str]:
        raw = self.get("ai_difficulty_order", ",".join(parse_difficulty_order(None)))
        normalized = parse_difficulty_order(raw)
        normalized_raw = ",".join(normalized)
        if raw != normalized_raw:
            self.set("ai_difficulty_order", normalized_raw)
        return normalized

    @ai_difficulty_order.setter
    def ai_difficulty_order(self, order: list[str] | str):
        if isinstance(order, list):
            raw = ",".join(order)
        else:
            raw = str(order)
        normalized = parse_difficulty_order(raw)
        self.set("ai_difficulty_order", ",".join(normalized))

    @property
    def start_message(self) -> str:
        return self.get("start_message", "")

    @start_message.setter
    def start_message(self, value: str):
        self.set("start_message", value)

    @property
    def help_message(self) -> str:
        return self.get("help_message", "")

    @help_message.setter
    def help_message(self, value: str):
        self.set("help_message", value)

    @property
    def bot_name(self) -> str:
        return self.get("bot_name", "").strip() or _config.BOT_NAME

    @bot_name.setter
    def bot_name(self, value: str):
        self.set("bot_name", value.strip())

    @property
    def flag_prefix(self) -> str:
        return self.get("flag_prefix", "").strip() or _config.FLAG_PREFIX

    @flag_prefix.setter
    def flag_prefix(self, value: str):
        self.set("flag_prefix", value.strip())

    @property
    def flag_keyword(self) -> str:
        return f"{self.flag_prefix}{{"
