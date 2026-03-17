import re
import time
import asyncio
import logging
from html import escape

from aiogram import Router, types

from config import (
    RATE_LIMIT_SECONDS,
    FLAG_KEYWORD,
    FLAG_AWARD_PREFIX,
    AI_MAX_CONCURRENT_REQUESTS,
    AI_OVERLOAD_QUEUE_THRESHOLD,
    AI_OVERLOAD_REPLY_TEXT,
    CHALLENGE_FLAG,
)
from metrics import add_gauge, inc_counter, observe_histogram, set_gauge
from services import competition_repo, conversation_repo, ctf_flag_repo, engine, bot_state, is_admin
from handlers._helpers import extract_attachments, safe_text, run_blocking
from notifications import announce_flag_found, log_bot_event, notify_admins, send_message
from ai.difficulty import get_difficulty_profile

message_router = Router()

_last_message_times: dict[int, float] = {}

# Limit concurrent AI calls to prevent thread pool + API quota exhaustion
_ai_semaphore = asyncio.Semaphore(AI_MAX_CONCURRENT_REQUESTS)
_ai_queue_depth = 0  # pending updates in this worker (waiting + inflight)
_ai_queue_lock = asyncio.Lock()


async def _check_multi_flag(
    chat_id: int,
    text: str,
    uid: int,
    first_name: str,
    username: str,
) -> bool:
    """Check user input against all CTF flags. Returns True if handled (caller should return early)."""
    try:
        matched = await run_blocking(ctf_flag_repo.check_input_against_flags, text)
    except Exception:
        return False
    if not matched:
        return False

    flag_id = matched["id"]
    flag_pos = matched["position"]
    str_uid = str(uid)

    # Already found this flag? Skip recording but still show the success message.
    already = await run_blocking(ctf_flag_repo.has_user_found_flag, str_uid, flag_id)

    if not already:
        # Must find flags sequentially.
        user_max_pos = await run_blocking(ctf_flag_repo.get_user_max_position, str_uid)
        if flag_pos > user_max_pos + 1:
            reject_msg = "Bunun öncesinde bilmen gereken başka flagler var. Sırayla ilerlemen gerekiyor!"
            await _send_user_text(chat_id, reject_msg)
            await run_blocking(
                conversation_repo.log, str_uid, first_name, username,
                text, f"[Flag #{flag_pos} out-of-order] {reject_msg}",
            )
            return True

        # Record the find.
        await run_blocking(ctf_flag_repo.record_flag_found, str_uid, flag_id)

        # Notify if this flag has notify_on_find enabled.
        if matched.get("notify_on_find"):
            await announce_flag_found(
                str_uid, first_name, username,
                flag_id=flag_id,
                flag_position=flag_pos,
                flag_tag=matched.get("tag", ""),
                notify_message=matched.get("notify_message", ""),
                initiated_by="multi_flag_check",
            )

        # Position 1: backward-compat dual-write into flag_finders table.
        if flag_pos == 1:
            try:
                await run_blocking(
                    competition_repo.record_flag_finder,
                    str_uid,
                    first_name,
                    username,
                )
            except Exception as e:
                logging.warning("Flag finder backward-compat write failed for %s: %s", uid, e)

    # Send success message (both for new finds and re-entries).
    success_msg = matched.get("success_message") or ""
    if success_msg:
        response_text = success_msg
        await send_message(chat_id, success_msg, parse_mode="HTML")
    else:
        total = await run_blocking(ctf_flag_repo.get_flag_count)
        response_text = f"Tebrikler! Flag #{flag_pos} bulundu. ({flag_pos}/{total})"
        await _send_user_text(chat_id, response_text)

    # Log to conversation_logs so flag attempts appear in the logs page.
    tag = matched.get("tag", "")
    label = f"Flag #{flag_pos}" + (f" — {tag}" if tag else "")
    status = "re-entry" if already else "found"
    ai_msg = f"[{label} {status}] {response_text}"
    await run_blocking(
        conversation_repo.log, str_uid, first_name, username, text, ai_msg,
    )
    return True


async def _send_user_text(chat_id: int, text: str) -> None:
    result = await send_message(chat_id, text, parse_mode=None)
    if not result.get("ok"):
        logging.warning("Failed to send user message to %s: %s", chat_id, result.get("description"))


@message_router.message()
async def handle_message(message: types.Message):
    global _ai_queue_depth
    if not message.from_user:
        return

    uid = message.from_user.id
    first_name = message.from_user.first_name
    username = message.from_user.username or ""

    text = safe_text(message)

    # Bot-level anti-spam rate limiting (fast, in-memory)
    # Distinct from logic-level RateLimiter in models/rate_limiter.py
    now = time.time()
    if now - _last_message_times.get(uid, 0) < RATE_LIMIT_SECONDS:
        return
    _last_message_times[uid] = now

    # Cleanup stale entries periodically to bound memory usage
    if len(_last_message_times) > 5000:
        cutoff = now - 120
        for stale in [k for k, v in _last_message_times.items() if v < cutoff]:
            del _last_message_times[stale]

    # Backpressure: reject early if too many updates are already queued waiting for an AI slot.
    # Avoid awaiting network I/O inside the lock to prevent blocking other handlers.
    async with _ai_queue_lock:
        if _ai_queue_depth >= AI_OVERLOAD_QUEUE_THRESHOLD:
            inc_counter("ai_overload_rejects_total")
            _overloaded = True
        else:
            _ai_queue_depth += 1
            set_gauge("ai_queue_depth", float(_ai_queue_depth))
            _overloaded = False

    if _overloaded:
        await _send_user_text(message.chat.id, AI_OVERLOAD_REPLY_TEXT)
        return

    try:
        # If the shared RPM bucket is already exhausted, fail-fast before waiting for
        # an AI slot or downloading any attachments.
        try:
            if engine.is_global_rpm_exhausted():
                inc_counter("ai_global_rpm_short_circuit_total")
                await _send_user_text(message.chat.id, AI_OVERLOAD_REPLY_TEXT)
                return
        except Exception:
            # Best-effort only; never crash the handler due to overload heuristics.
            pass

        start = time.monotonic()
        queued_at = time.monotonic()
        await _ai_semaphore.acquire()
        try:
            observe_histogram("ai_semaphore_wait_seconds", time.monotonic() - queued_at)
            add_gauge("ai_inflight_requests", 1.0)

            attachments = []
            try:
                attachments = await extract_attachments(message.bot, message)
            except Exception as e:
                logging.warning("Attachment extraction failed for user %s: %s", uid, e)
                attachments = []

            # Multi-flag pre-check: match user input against all CTF flags
            # before invoking the AI engine.
            flag_handled = await _check_multi_flag(
                message.chat.id, text, uid, first_name, username,
            )
            if flag_handled:
                return

            try:
                await message.bot.send_chat_action(message.chat.id, "typing")
            except Exception as e:
                logging.debug("send_chat_action failed for %s: %s", message.chat.id, e)

            ai_response = await engine.get_response(
                text, str(uid), first_name, username, attachments,
                is_admin=is_admin(uid),
            )
        finally:
            add_gauge("ai_inflight_requests", -1.0)
            _ai_semaphore.release()
        elapsed = time.monotonic() - start
        observe_histogram("message_processing_seconds", elapsed)
        if elapsed > 5.0:
            logging.warning("Slow AI response for user %s: %.2fs", uid, elapsed)

        difficulty = "HARD"
        try:
            difficulty = await run_blocking(lambda: bot_state.ai_difficulty_level)
        except Exception as e:
            logging.warning("Difficulty lookup failed, defaulting to HARD: %s", e)
        profile = get_difficulty_profile(difficulty)

        is_flag_award = ai_response.startswith(FLAG_AWARD_PREFIX)
        if is_flag_award:
            ai_response = ai_response[len(FLAG_AWARD_PREFIX):]
            try:
                await run_blocking(
                    competition_repo.record_flag_finder,
                    str(uid),
                    first_name,
                    username,
                )
            except Exception as e:
                logging.warning("Failed to process flag finder event for %s: %s", uid, e)
            # Dual-write: record position 1 flag in multi-flag progress & notify.
            try:
                pos1_flags = await run_blocking(ctf_flag_repo.get_all_flags)
                if pos1_flags:
                    _pos1 = pos1_flags[0]
                    _pos1_msg = _pos1.get("success_message") or ""
                    if _pos1_msg:
                        ai_response = _pos1_msg
                    new_find = await run_blocking(ctf_flag_repo.record_flag_found, str(uid), _pos1["id"])
                    if new_find and _pos1.get("notify_on_find"):
                        await announce_flag_found(
                            str(uid), first_name, username,
                            flag_id=_pos1["id"],
                            flag_position=_pos1["position"],
                            flag_tag=_pos1.get("tag", ""),
                            notify_message=_pos1.get("notify_message", ""),
                            initiated_by="flag_award_detection",
                        )
            except Exception:
                pass
        model_revealed_flag = False
        _model_reveal_success = ""
        if not is_flag_award and CHALLENGE_FLAG and CHALLENGE_FLAG in ai_response and profile.key != "IMPOSSIBLE":
            # Model-driven reveal path: send AI response normally, then follow up with success.
            model_revealed_flag = True
            try:
                await run_blocking(
                    competition_repo.record_flag_finder,
                    str(uid),
                    first_name,
                    username,
                )
            except Exception as e:
                logging.warning("Failed to process model reveal flag event for %s: %s", uid, e)
            # Dual-write: record position 1 flag in multi-flag progress & notify.
            try:
                pos1_flags = await run_blocking(ctf_flag_repo.get_all_flags)
                if pos1_flags:
                    _pos1 = pos1_flags[0]
                    _model_reveal_success = _pos1.get("success_message") or ""
                    new_find = await run_blocking(ctf_flag_repo.record_flag_found, str(uid), _pos1["id"])
                    if new_find and _pos1.get("notify_on_find"):
                        await announce_flag_found(
                            str(uid), first_name, username,
                            flag_id=_pos1["id"],
                            flag_position=_pos1["position"],
                            flag_tag=_pos1.get("tag", ""),
                            notify_message=_pos1.get("notify_message", ""),
                            initiated_by=f"flag_model_reveal_{difficulty.lower()}",
                        )
            except Exception as e:
                logging.warning("Failed to process model reveal flag event for %s: %s", uid, e)
            # Dual-write: record position 1 flag in multi-flag progress.
            try:
                pos1_flags = await run_blocking(ctf_flag_repo.get_all_flags)
                if pos1_flags:
                    await run_blocking(ctf_flag_repo.record_flag_found, str(uid), pos1_flags[0]["id"])
            except Exception:
                pass

        mention = f"@{username}" if username else "(no username)"
        _flag_leak = False
        if not is_flag_award and not model_revealed_flag and FLAG_KEYWORD in ai_response and profile.block_flag_keyword_delivery:
            # Extract content inside FLAG_KEYWORD from the AI response and check
            # against the real flag's inner value. Only alert if there's a partial match.
            _real_inner = (CHALLENGE_FLAG or "")[len(FLAG_KEYWORD):-1]  # strip prefix{ and }
            for _m in re.finditer(rf'{re.escape(FLAG_KEYWORD)}([^}}]*)', ai_response):
                _leaked = _m.group(1)
                if not _leaked:
                    continue  # bare flag keyword with nothing inside — not a leak
                if _leaked in _real_inner or _real_inner in _leaked:
                    _flag_leak = True
                    break
                # Check overlapping partial match (end of leaked matches start of real or vice versa)
                _min_overlap = min(3, len(_leaked), len(_real_inner))
                for _ol in range(_min_overlap, min(len(_leaked), len(_real_inner)) + 1):
                    if _real_inner[:_ol] == _leaked[:_ol] or _real_inner[:_ol] == _leaked[-_ol:]:
                        _flag_leak = True
                        break
                if _flag_leak:
                    break
        if _flag_leak:
            # Block the response and alert admins
            await run_blocking(
                log_bot_event,
                "flag_leak_blocked",
                f"Input: {text[:200]} | Response: {ai_response[:200]} | Difficulty: {difficulty}",
                str(uid),
                first_name,
                username,
            )
            await notify_admins(
                f"<b>Flag Sızıntısı Engellendi</b>\n\n"
                f"{escape(first_name)} ({escape(mention)}) · <code>{uid}</code>\n\n"
                f"<i>Girdi:</i> {escape(text[:200])}\n"
                f"<i>Yanit:</i> {escape(ai_response[:200])}\n"
                f"<i>Seviye:</i> {escape(difficulty)}"
            )
            await _send_user_text(
                message.chat.id,
                "Güvenlik sistemi devreye girdi. Bu mesaj engellenmiştir.",
            )
        elif is_flag_award:
            await send_message(message.chat.id, ai_response, parse_mode="HTML")
        else:
            await _send_user_text(message.chat.id, ai_response)

        if model_revealed_flag:
            if not _model_reveal_success:
                total = await run_blocking(ctf_flag_repo.get_flag_count)
                _model_reveal_success = f"Tebrikler! Flag #1 bulundu. (1/{total})"
            await send_message(
                message.chat.id,
                _model_reveal_success,
                parse_mode="HTML",
            )

        if logging.getLogger().isEnabledFor(logging.DEBUG):
            short = (text[:50] + "...") if text else "(no-text)"
            logging.debug(
                "message user=%s name=%s text=%s attachments=%s",
                uid,
                first_name,
                short,
                len(attachments),
            )

    except Exception as e:
        await run_blocking(
            log_bot_event,
            "error",
            str(e)[:500],
            str(uid),
            first_name,
            username,
        )
        await notify_admins(
            f"<b>Hata</b>\n\n"
            f"{escape(first_name)} · <code>{uid}</code>\n\n"
            f"<code>{escape(str(e)[:200])}</code>"
        )
        await _send_user_text(message.chat.id, "Bir şeyler ters gitti. Tekrar dener misin?")
    finally:
        async with _ai_queue_lock:
            _ai_queue_depth = max(0, _ai_queue_depth - 1)
            set_gauge("ai_queue_depth", float(_ai_queue_depth))
