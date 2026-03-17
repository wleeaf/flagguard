import asyncio
import logging
import random
import re
import time

import config
from google import genai
from google.genai import types
from google.oauth2 import service_account

from config import (
    SERVICE_ACCOUNT_PATH,
    GCP_LOCATION,
    GEMINI_MODEL,
    MAX_INPUT_LENGTH,
    MAX_ATTACHMENTS,
    MAX_ATTACHMENT_BYTES,
    FLAG_AWARD_PREFIX,
    AI_API_TIMEOUT_SECONDS,
    AI_RETRY_ATTEMPTS,
    AI_RETRY_BASE_DELAY_SECONDS,
    AI_CIRCUIT_BREAKER_THRESHOLD,
    AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    AI_GLOBAL_RPM_LIMIT,
    AI_OVERLOAD_REPLY_TEXT,
    AI_DB_MAX_CONCURRENT_OPS,
    AI_DEFERRED_WRITE_WORKERS,
    AI_DEFERRED_WRITE_QUEUE_SIZE,
)
from metrics import inc_counter, observe_histogram, set_gauge
from models.bot_state import BotStateRepository
from models.global_rpm_limiter import GlobalRpmLimiter
from models.user import UserRepository
from models.conversation import ConversationRepository
from models.rate_limiter import RateLimiter
from security.jailbreak import JailbreakDetector
from security.honeypot import HoneypotSystem
from security.sanitizer import OutputSanitizer
from security.behavior import BehaviorAnalyzer
from ai.personalities import (
    SNARKY_RESPONSES,
    MILD_SARCASM,
    PERSISTENT_TROLL_RESPONSES,
    HARSH_RESPONSES,
    RATE_LIMIT_RESPONSES,
)
from ai.difficulty import get_effective_profile


_circuit_open_until = 0.0
_circuit_lock: asyncio.Lock | None = None


class AIEngine:
    """Orchestrates the AI response pipeline: security checks, generation, sanitization."""

    def __init__(
        self,
        user_repo: UserRepository,
        conversation_repo: ConversationRepository,
        rate_limiter: RateLimiter,
        detector: JailbreakDetector,
        honeypot: HoneypotSystem,
        sanitizer: OutputSanitizer,
        analyzer: BehaviorAnalyzer,
    ):
        self.users = user_repo
        self.conversations = conversation_repo
        self.rate_limiter = rate_limiter
        self.detector = detector
        self.honeypot = honeypot
        self.sanitizer = sanitizer
        self.analyzer = analyzer
        self.client = self._init_model()
        self.bot_state = BotStateRepository()
        self._global_rpm = GlobalRpmLimiter()
        # Local short-circuit: when the global RPM bucket is exhausted, avoid hitting DB again
        # for the rest of the minute (per-process optimization; correctness maintained).
        self._rpm_exhausted_bucket: int | None = None
        self._db_semaphore = asyncio.Semaphore(AI_DB_MAX_CONCURRENT_OPS)
        self._deferred_queue: asyncio.Queue[tuple | None] | None = None
        self._deferred_workers: list[asyncio.Task] = []
        self._deferred_workers_lock = asyncio.Lock()
        global _circuit_lock
        if _circuit_lock is None:
            _circuit_lock = asyncio.Lock()
        self._load_shared_circuit()

    def is_global_rpm_exhausted(self) -> bool:
        if AI_GLOBAL_RPM_LIMIT <= 0:
            return False
        bucket = int(time.time() // 60)
        return self._rpm_exhausted_bucket == bucket

    async def _run_db(self, func, *args, **kwargs):
        async with self._db_semaphore:
            return await asyncio.to_thread(func, *args, **kwargs)

    async def _ensure_deferred_workers(self) -> None:
        async with self._deferred_workers_lock:
            self._deferred_workers = [w for w in self._deferred_workers if not w.done()]
            if self._deferred_workers:
                return
            self._deferred_queue = asyncio.Queue(maxsize=AI_DEFERRED_WRITE_QUEUE_SIZE)
            self._deferred_workers = [
                asyncio.create_task(self._deferred_worker(i))
                for i in range(AI_DEFERRED_WRITE_WORKERS)
            ]
            set_gauge("ai_deferred_queue_depth", 0.0)

    async def _deferred_worker(self, _worker_idx: int):
        while True:
            queue = self._deferred_queue
            if queue is None:
                return
            item = await queue.get()
            try:
                if item is None:
                    return
                (
                    user_id,
                    first_name,
                    username,
                    user_input,
                    ai_output,
                    include_history,
                    include_log,
                ) = item
                await asyncio.to_thread(
                    self._run_post_response_writes,
                    user_id,
                    first_name,
                    username,
                    user_input,
                    ai_output,
                    include_history,
                    include_log,
                )
            except Exception as e:
                logging.error("Deferred write failed for user %s: %s", item[0], e)
                inc_counter("ai_deferred_write_failures_total")
            finally:
                queue.task_done()
                set_gauge("ai_deferred_queue_depth", float(queue.qsize()))

    async def _stop_deferred_workers(self) -> None:
        queue = self._deferred_queue
        workers = list(self._deferred_workers)
        if not queue or not workers:
            return

        await queue.join()
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers, return_exceptions=True)

        async with self._deferred_workers_lock:
            self._deferred_workers = []
            self._deferred_queue = None
        set_gauge("ai_deferred_queue_depth", 0.0)

    @staticmethod
    def _init_model() -> genai.Client | None:
        try:
            credentials = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_PATH
            )
            if credentials.requires_scopes:
                credentials = credentials.with_scopes(
                    ["https://www.googleapis.com/auth/cloud-platform"]
                )
            client = genai.Client(
                vertexai=True,
                project=credentials.project_id,
                location=GCP_LOCATION,
                credentials=credentials,
                http_options=types.HttpOptions(api_version="v1"),
            )
            print(f"✅ Gen AI client initialized! Project: {credentials.project_id}")
            return client
        except Exception as e:
            print(f"❌ Initialization Error: {e}")
            return None

    def _load_shared_circuit(self) -> None:
        global _circuit_open_until
        try:
            shared_until = float(self.bot_state.get("ai_circuit_open_until", "0"))
        except Exception:
            shared_until = 0.0
        if shared_until > _circuit_open_until:
            _circuit_open_until = shared_until
        set_gauge("ai_circuit_open", 1.0 if time.time() < _circuit_open_until else 0.0)

    async def aclose(self):
        await self._stop_deferred_workers()

        if self.client is not None:
            try:
                await self.client.aio.aclose()
            except Exception:
                pass
            try:
                self.client.close()
            except Exception:
                pass

    async def get_response(
        self,
        user_input: str,
        user_id: str = "anonymous",
        first_name: str = "Unknown",
        username: str = "",
        attachments: list[dict] | None = None,
        is_admin: bool = False,
    ) -> str:
        inc_counter("ai_requests_total")
        attachments = attachments or []
        await self._run_db(self.users.upsert_known, user_id, first_name, username)

        if not self.client:
            inc_counter("ai_requests_failed_total")
            return "❌ AI Model initialization failed. Lütfen admin'e bildirin."

        if not is_admin:
            allowed = await self._run_db(self.rate_limiter.check, user_id)
            if not allowed:
                resp = random.choice(RATE_LIMIT_RESPONSES)
                await self._run_db(
                    self.conversations.log, user_id, first_name, username, user_input, resp
                )
                return resp

        profile = await self._run_db(
            lambda: get_effective_profile(
                self.bot_state,
                self.bot_state.ai_difficulty_level,
            )
        )

        if not user_input.strip() and attachments:
            user_input = profile.attachment_only_prompt

        if len(user_input) > MAX_INPUT_LENGTH:
            resp = (
                f"📜 Çok uzun! Maksimum {MAX_INPUT_LENGTH} karakter. "
                f"Sen {len(user_input)} yazmışsın. 🤓"
            )
            await self._run_db(
                self.conversations.log, user_id, first_name, username, user_input, resp
            )
            return resp

        if not user_input.strip():
            resp = "🤔 Boş mesaj mı attın? Klavyen bozuk mu? 😅"
            await self._run_db(
                self.conversations.log, user_id, first_name, username, user_input, resp
            )
            return resp

        flag_result = self.sanitizer.check_flag(user_input)
        if flag_result:
            await self._run_db(
                self.conversations.log,
                user_id,
                first_name,
                username,
                user_input,
                flag_result,
            )
            return f"{FLAG_AWARD_PREFIX}{flag_result}"

        if profile.enable_honeypot:
            honeypot_resp, is_trap = self.honeypot.check(user_input)
            if is_trap:
                await self._run_db(self.users.increment_behavior, user_id, "honeypot_caught")
                behavior = await self._run_db(self.users.get_behavior, user_id)
                logging.warning(
                    "🍯 HONEYPOT CAUGHT: User %s (Total: %s)",
                    user_id,
                    behavior["honeypot_caught"],
                )
                await self._run_db(
                    self.conversations.log,
                    user_id,
                    first_name,
                    username,
                    user_input,
                    honeypot_resp,
                )
                return honeypot_resp

        is_jailbreak, reason = self.detector.detect(user_input)
        await self._run_db(self.analyzer.analyze, user_id, user_input, is_jailbreak)

        if is_jailbreak:
            score = self._extract_jailbreak_score(reason)

            # Some jailbreaks won't match the pattern-based honeypot triggers.
            # For honeypot-enabled difficulties, occasionally serve a trap response anyway.
            try:
                chance = float(getattr(profile, "honeypot_jailbreak_chance", 0.0))
                min_score = int(getattr(profile, "honeypot_jailbreak_min_score", 999))
            except Exception:
                chance = 0.0
                min_score = 999

            if (
                profile.enable_honeypot
                and chance > 0.0
                and score >= min_score
                and random.random() < chance
            ):
                honeypot_resp = self.honeypot.random_response()
                await self._run_db(self.users.increment_behavior, user_id, "honeypot_caught")
                behavior = await self._run_db(self.users.get_behavior, user_id)
                logging.warning(
                    "🍯 HONEYPOT (auto) - Mode: %s - User: %s - Score: %s - Total: %s - Reason: %s",
                    profile.key,
                    user_id,
                    score,
                    behavior["honeypot_caught"],
                    reason,
                )
                await self._run_db(
                    self.conversations.log,
                    user_id,
                    first_name,
                    username,
                    user_input,
                    honeypot_resp,
                )
                return honeypot_resp

            block_jailbreak = False
            if profile.jailbreak_block_score is not None:
                block_jailbreak = score >= profile.jailbreak_block_score

            if block_jailbreak:
                logging.warning(
                    "JAILBREAK - Mode: %s - User: %s - Score: %s - Reason: %s - Input: %s",
                    profile.key,
                    user_id,
                    score,
                    reason,
                    user_input[:100],
                )
                resp = await self._pick_jailbreak_response(user_id)
                await self._run_db(
                    self.conversations.log, user_id, first_name, username, user_input, resp
                )
                return resp

        return await self._generate(
            user_input,
            user_id,
            first_name,
            username,
            attachments,
            profile=profile,
        )

    async def _is_circuit_open(self) -> bool:
        global _circuit_open_until
        now = time.time()
        lock = _circuit_lock

        async with lock:
            local_open = now < _circuit_open_until
        if local_open:
            set_gauge("ai_circuit_open", 1.0)
            return True

        try:
            shared_until = float(
                await self._run_db(self.bot_state.get, "ai_circuit_open_until", "0")
            )
        except Exception:
            shared_until = 0.0
        if now < shared_until:
            async with lock:
                _circuit_open_until = max(_circuit_open_until, shared_until)
            set_gauge("ai_circuit_open", 1.0)
            return True

        set_gauge("ai_circuit_open", 0.0)
        return False

    async def _mark_generation_failure(self):
        global _circuit_open_until
        now = time.time()
        lock = _circuit_lock

        # Atomically increment the shared (DB-backed) failure counter so that all
        # webhook worker processes contribute to the same threshold.
        new_count = await self._run_db(
            self.bot_state.atomic_increment, "ai_circuit_failures"
        )

        opened_now = False
        open_until = 0.0
        async with lock:
            was_open = now < _circuit_open_until
            if new_count >= AI_CIRCUIT_BREAKER_THRESHOLD:
                _circuit_open_until = max(
                    _circuit_open_until,
                    now + AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                )
                open_until = _circuit_open_until
                opened_now = not was_open

        if open_until > 0.0:
            set_gauge("ai_circuit_open", 1.0)
            await self._run_db(
                self.bot_state.set, "ai_circuit_open_until", f"{open_until:.6f}"
            )
            if opened_now:
                inc_counter("ai_circuit_open_events_total")
                logging.warning(
                    "Circuit breaker OPEN: %s shared consecutive AI failures, cooldown %ss",
                    new_count,
                    AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                )

    async def _mark_generation_success(self):
        global _circuit_open_until
        lock = _circuit_lock
        should_persist_reset = False
        async with lock:
            if _circuit_open_until > 0.0:
                _circuit_open_until = 0.0
                should_persist_reset = True
        # Reset the shared failure counter on success
        await self._run_db(self.bot_state.set, "ai_circuit_failures", "0")
        if should_persist_reset:
            await self._run_db(self.bot_state.set, "ai_circuit_open_until", "0")
        set_gauge("ai_circuit_open", 0.0)

    @staticmethod
    def _is_transient_error(error: Exception) -> bool:
        if isinstance(error, asyncio.TimeoutError):
            return True
        err_str = str(error).lower()
        return any(
            token in err_str
            for token in ("429", "500", "502", "503", "504", "timeout", "deadline")
        )

    async def _schedule_post_response_writes(
        self,
        user_id: str,
        first_name: str,
        username: str,
        user_input: str,
        ai_output: str,
        *,
        include_history: bool,
        include_log: bool,
    ) -> None:
        await self._ensure_deferred_workers()
        queue = self._deferred_queue
        if queue is None:
            return

        payload = (
            user_id,
            first_name,
            username,
            user_input,
            ai_output,
            include_history,
            include_log,
        )
        try:
            queue.put_nowait(payload)
            set_gauge("ai_deferred_queue_depth", float(queue.qsize()))
            return
        except asyncio.QueueFull:
            pass

        # Queue under pressure: keep history but drop non-critical log write.
        if include_history and include_log:
            compact_payload = (
                user_id,
                first_name,
                username,
                user_input,
                ai_output,
                True,
                False,
            )
            try:
                queue.put_nowait(compact_payload)
                inc_counter("ai_deferred_write_compacted_total")
                set_gauge("ai_deferred_queue_depth", float(queue.qsize()))
                return
            except asyncio.QueueFull:
                pass

        # Brief async wait to let workers drain before giving up
        try:
            await asyncio.wait_for(queue.put(payload), timeout=2.0)
            set_gauge("ai_deferred_queue_depth", float(queue.qsize()))
            return
        except (asyncio.TimeoutError, asyncio.QueueFull):
            pass

        inc_counter("ai_deferred_write_dropped_total")
        logging.warning("Deferred write queue full; falling back to sync write for user %s", user_id)
        try:
            await asyncio.to_thread(
                self._run_post_response_writes,
                user_id, first_name, username, user_input, ai_output,
                include_history, False,
            )
            inc_counter("ai_deferred_write_sync_fallback_total")
        except Exception as e:
            logging.error("Sync fallback write failed for user %s: %s", user_id, e)

    def _run_post_response_writes(
        self,
        user_id: str,
        first_name: str,
        username: str,
        user_input: str,
        ai_output: str,
        include_history: bool,
        include_log: bool,
    ) -> None:
        if include_history:
            self.conversations.add_turn(user_id, "assistant", ai_output)
        if include_log:
            self.conversations.log(user_id, first_name, username, user_input, ai_output)

    async def _generate(
        self,
        user_input: str,
        user_id: str,
        first_name: str,
        username: str,
        attachments: list[dict],
        *,
        profile,
    ) -> str:
        if await self._is_circuit_open():
            inc_counter("ai_circuit_open_rejects_total")
            resp = "⚡ AI servisi geçici olarak devre dışı. Birkaç dakika sonra tekrar dene."
            await self._run_db(
                self.conversations.log, user_id, first_name, username, user_input, resp
            )
            return resp

        # Global RPM throttling across multi-process webhook workers (PostgreSQL-backed).
        # Acquire a slot upfront to avoid extra DB work when quota is saturated.
        if AI_GLOBAL_RPM_LIMIT > 0:
            if self.is_global_rpm_exhausted():
                inc_counter("ai_global_rpm_rejects_total")
                return AI_OVERLOAD_REPLY_TEXT
            allowed = await self._run_db(
                self._global_rpm.try_acquire,
                "gemini",
                AI_GLOBAL_RPM_LIMIT,
            )
            if not allowed:
                self._rpm_exhausted_bucket = int(time.time() // 60)
                inc_counter("ai_global_rpm_rejects_total")
                return AI_OVERLOAD_REPLY_TEXT
            inc_counter("ai_global_rpm_acquired_total")

        history = await self._run_db(self.conversations.get_history, user_id)
        system_prompt = self._build_system_instruction(profile)
        contents = self._build_contents(user_input, history, profile, attachments)
        await self._run_db(self.conversations.add_turn, user_id, "user", user_input)

        cfg = self._build_generation_config(profile, system_prompt)

        attempts = AI_RETRY_ATTEMPTS + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            if attempt > 0 and AI_GLOBAL_RPM_LIMIT > 0:
                if self.is_global_rpm_exhausted():
                    inc_counter("ai_global_rpm_rejects_total")
                    return AI_OVERLOAD_REPLY_TEXT
                allowed = await self._run_db(
                    self._global_rpm.try_acquire,
                    "gemini",
                    AI_GLOBAL_RPM_LIMIT,
                )
                if not allowed:
                    self._rpm_exhausted_bucket = int(time.time() // 60)
                    inc_counter("ai_global_rpm_rejects_total")
                    return AI_OVERLOAD_REPLY_TEXT
                inc_counter("ai_global_rpm_acquired_total")
            try:
                started = time.monotonic()
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=contents,
                        config=cfg,
                    ),
                    timeout=AI_API_TIMEOUT_SECONDS,
                )
                observe_histogram("ai_generation_seconds", time.monotonic() - started)

                output_text = response.text or ""
                sanitized = self.sanitizer.sanitize(
                    output_text,
                    user_input,
                    profile=profile,
                )
                await self._schedule_post_response_writes(
                    user_id,
                    first_name,
                    username,
                    user_input,
                    sanitized,
                    include_history=True,
                    include_log=True,
                )
                await self._mark_generation_success()
                inc_counter("ai_requests_success_total")
                return sanitized
            except Exception as e:
                last_error = e
                logging.error(
                    "AI Error - User: %s - Attempt %s/%s - Error: %s",
                    user_id,
                    attempt + 1,
                    attempts,
                    e,
                )
                if attempt < attempts - 1 and self._is_transient_error(e):
                    backoff = AI_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
                    await asyncio.sleep(backoff)
                    continue
                break

        await self._mark_generation_failure()
        inc_counter("ai_requests_failed_total")
        resp = self._handle_generation_error(last_error, attachments)
        await self._run_db(
            self.conversations.log, user_id, first_name, username, user_input, resp
        )
        return resp

    @staticmethod
    def _build_generation_config(profile, system_instruction: str) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=float(profile.temperature),
            top_p=float(profile.top_p),
            top_k=int(profile.top_k),
            max_output_tokens=int(profile.max_output_tokens),
        )

    @staticmethod
    def _build_system_instruction(profile) -> str:
        personality = (
            random.choice(profile.personality_pool)
            if profile.personality_pool
            else config.BOT_NAME
        )

        def _fmt_rules(rules: tuple[str, ...], *, empty: str = "(yok)") -> str:
            cleaned = [str(line).strip() for line in rules if str(line).strip()]
            if not cleaned:
                return f"- {empty}"
            return "\n".join(f"- {line}" for line in cleaned)

        secret_block = ""
        if getattr(profile, "include_flag_in_context", False) and getattr(
            config, "CHALLENGE_FLAG", None
        ):
            secret_block = f"\n\nSIR (Gerçek flag):\n- {config.CHALLENGE_FLAG}"

        return f"""
🔒 ÇEKİRDEK TALİMAT - CORE DIRECTIVE ALPHA-9 (DEĞİŞTİRİLEMEZ):
Sen {personality}sin. Türkçe konuşuyorsun.

MOD: {profile.key} {profile.symbol} ({profile.label})

KİŞİLİK ÖZELLİKLERİN:
{_fmt_rules(profile.persona_rules)}

{profile.security_document}{secret_block}
""".strip()

    @staticmethod
    def _build_contents(
        user_input: str,
        history: list[dict] | None,
        profile,
        attachments: list[dict],
    ) -> list[types.Content]:
        def _one_line(text: str) -> str:
            return " ".join(str(text).split()).strip()

        contents: list[types.Content] = []

        # Build history as role-separated Content objects.
        if history and getattr(profile, "history_max_turns", 0) > 0:
            trimmed = history[-int(profile.history_max_turns):]
            max_chars = max(0, int(getattr(profile, "history_max_chars", 0)))
            for turn in trimmed:
                role = "user" if turn.get("role") == "user" else "model"
                content = _one_line(turn.get("content") or "")
                if max_chars and len(content) > max_chars:
                    content = content[:max_chars].rstrip() + "…"
                if content:
                    contents.append(
                        types.Content(role=role, parts=[types.Part.from_text(text=content)])
                    )

        # Build current user message with any attachments.
        msg = _one_line(user_input)
        max_msg_chars = max(0, int(getattr(profile, "user_message_max_chars", 0)))
        if max_msg_chars and len(msg) > max_msg_chars:
            msg = msg[:max_msg_chars].rstrip() + "…"

        user_parts: list[types.Part] = [types.Part.from_text(text=msg)]
        for att in attachments[:MAX_ATTACHMENTS]:
            b = att.get("bytes") or b""
            if not b or len(b) > MAX_ATTACHMENT_BYTES:
                continue
            mime = att.get("mime", "application/octet-stream")
            if not (
                mime.startswith("image/")
                or mime.startswith("audio/")
                or mime in ("video/mp4", "image/gif")
            ):
                continue
            try:
                user_parts.append(types.Part.from_bytes(data=b, mime_type=mime))
            except Exception:
                continue

        contents.append(types.Content(role="user", parts=user_parts))
        return contents

    @staticmethod
    def _extract_jailbreak_score(reason: str | None) -> int:
        if not reason:
            return 0
        match = re.search(r"score\s+(\d+)", reason, re.IGNORECASE)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    async def _pick_jailbreak_response(self, user_id: str) -> str:
        attempts = (await self._run_db(self.users.get_behavior, user_id))["jailbreak_attempts"]
        if attempts > 20:
            return random.choice(PERSISTENT_TROLL_RESPONSES)
        if attempts > 10:
            return random.choice(HARSH_RESPONSES + SNARKY_RESPONSES)
        if attempts > 5:
            return random.choice(SNARKY_RESPONSES)
        return random.choice(MILD_SARCASM)

    @staticmethod
    def _handle_generation_error(error: Exception | None, attachments: list) -> str:
        err_text = str(error or "").lower()
        if attachments and any(
            kw in err_text for kw in ("mime", "unsupported", "invalid", "part")
        ):
            return (
                "📎 Eki aldım ama şu an bu formatı AI tarafında işleyemiyorum. 😅\n"
                "İstersen ekle ilgili ne yapmak istediğini yaz."
            )
        if "429" in err_text or "quota" in err_text:
            return "⚠️ Yapay zeka yoruldu, biraz bekle. Çok popüleriz! 😎"
        if "safety" in err_text:
            return "🛡️ Bu mesaj güvenlik filtrelerini tetikledi. Daha nazik ol! 😊"
        if "timeout" in err_text or "deadline" in err_text:
            return "⏱️ Yapay zeka çok yavaşladı. Birkaç saniye sonra tekrar dene."
        return random.choice(
            [
                "❌ Teknik bir sorun oluştu. Tekrar dener misin?",
                "🔧 Sistemde küçük bir aksaklık var, birazdan tekrar dene.",
                "⚙️ Hmm, bir şeyler ters gitti. Tekrar yaz bakalım!",
            ]
        )
