import logging

from aiogram import Router, types
from aiogram.filters import Command

from handlers._helpers import run_blocking
from services import is_admin, moderation_repo
from time_utils import db_now, parse_db_timestamp, now_local

admin_moderation_router = Router()


def _parse_duration_flags(text: str) -> int | None:
    """Parse --seconds/--minutes/--hours flags from command text. Returns total seconds or None."""
    parts = text.split()
    total = 0
    i = 0
    while i < len(parts):
        part = parts[i]
        if part in ("--seconds", "--s") and i + 1 < len(parts):
            try:
                total += int(parts[i + 1])
            except ValueError:
                return None
            i += 2
        elif part in ("--minutes", "--m") and i + 1 < len(parts):
            try:
                total += int(parts[i + 1]) * 60
            except ValueError:
                return None
            i += 2
        elif part in ("--hours", "--h") and i + 1 < len(parts):
            try:
                total += int(parts[i + 1]) * 3600
            except ValueError:
                return None
            i += 2
        else:
            i += 1
    return total if total > 0 else None


def _format_duration(seconds: int) -> str:
    """Format seconds into a human-readable Turkish duration string."""
    if seconds < 60:
        return f"{seconds} saniye"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    parts = []
    if hours:
        parts.append(f"{hours} saat")
    if minutes:
        parts.append(f"{minutes} dakika")
    return " ".join(parts) if parts else f"{seconds} saniye"


@admin_moderation_router.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            return await message.answer("Kullanım: /ban <user_id>")
        target = parts[1]
        if not target.isdigit():
            return await message.answer("USER_ID sadece rakamlardan oluşmalıdır.")
        if is_admin(int(target)):
            return await message.answer("Yöneticiler banlanamaz.")

        await run_blocking(
            moderation_repo.ban_user, target, str(message.from_user.id)
        )
        await message.answer("Kullanıcı banlandı.")
    except Exception:
        logging.exception("cmd_ban failed")
        await message.answer("Ban işlemi başarısız oldu.")


@admin_moderation_router.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            return await message.answer("Kullanım: /unban <user_id>")
        target = parts[1]
        if not target.isdigit():
            return await message.answer("USER_ID sadece rakamlardan oluşmalıdır.")

        await run_blocking(moderation_repo.unban_user, target)
        await message.answer("Ban kaldırıldı.")
    except Exception:
        logging.exception("cmd_unban failed")
        await message.answer("Ban kaldırma işlemi başarısız oldu.")


@admin_moderation_router.message(Command("timeout"))
async def cmd_timeout(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            return await message.answer(
                "Kullanım: /timeout <user_id> --hours H --minutes M --seconds S"
            )
        target = parts[1]
        if not target.isdigit():
            return await message.answer("USER_ID sadece rakamlardan oluşmalıdır.")
        if is_admin(int(target)):
            return await message.answer("Yöneticilere timeout uygulanamaz.")

        total_seconds = _parse_duration_flags(message.text)
        if total_seconds is None:
            return await message.answer(
                "Süre belirtmelisiniz: --hours H --minutes M --seconds S (en az biri gerekli)"
            )

        from datetime import timedelta
        until_dt = now_local() + timedelta(seconds=total_seconds)
        until_str = until_dt.strftime("%Y-%m-%d %H:%M:%S")

        await run_blocking(
            moderation_repo.timeout_user, target, until_str, str(message.from_user.id)
        )
        await message.answer(f"Kullanıcı {_format_duration(total_seconds)} süreliğine kısıtlandı.")
    except Exception:
        logging.exception("cmd_timeout failed")
        await message.answer("Timeout işlemi başarısız oldu.")


@admin_moderation_router.message(Command("untimeout"))
async def cmd_untimeout(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            return await message.answer("Kullanım: /untimeout <user_id>")
        target = parts[1]
        if not target.isdigit():
            return await message.answer("USER_ID sadece rakamlardan oluşmalıdır.")

        await run_blocking(moderation_repo.remove_timeout, target)
        await message.answer("Kısıtlama kaldırıldı.")
    except Exception:
        logging.exception("cmd_untimeout failed")
        await message.answer("Kısıtlama kaldırma işlemi başarısız oldu.")
