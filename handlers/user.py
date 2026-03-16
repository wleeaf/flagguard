import logging
import random
import re
from html import escape

from aiogram import Router, types
from aiogram.filters import Command

from services import conversation_repo, ctf_flag_repo, report_repo, bot_state
from ai.personalities import WELCOME_MESSAGES
from ai.difficulty import get_difficulty_profile
from notifications import log_bot_event, notify_admins, _DIFFICULTY_NOTIFY_COPY_TR
from handlers._helpers import run_blocking

user_router = Router()


def _safe_format(template: str, vars_dict: dict) -> str:
    """Substitute {key} placeholders for known keys, leave others untouched."""
    def _replacer(m):
        key = m.group(1)
        if key in vars_dict:
            return str(vars_dict[key])
        return m.group(0)
    return re.sub(r"\{(\w+)\}", _replacer, template)


_DEFAULT_START_TEMPLATE = (
    "{welcome}\n\n"
    "<b>ZORLUK SEVİYESİ</b>\n"
    "{difficulty_symbol} {difficulty_name}\n"
    "<i>{difficulty_blurb}</i>\n\n"
    "<b>GÖREV</b>\n"
    "Bu bir çok aşamalı CTF challenge. Amacın flagleri sırayla bulmak.\n"
    "Her flag'i bulduktan sonra bana mesaj olarak gönder — sıradaki aşama için ipucu alırsın!\n\n"
    "<b>KULLANIM</b>\n"
    "Benimle sohbet et, yaratıcı ol, flagleri bul.\n"
    "Flag paylaşımı tespit edilebilir, lütfen paylaşmayın.\n\n"
    "<b>KOMUTLAR</b>\n"
    "/start - Bu menü\n"
    "/difficulty - Zorluk seviyesini gör\n"
    "/id - Kimliğini öğren\n"
    "/reset - Konuşmayı sıfırla\n"
    "/report - Sorun bildir\n"
    "/answer - Yarışma cevabı gönder\n\n"
    "<b>İPUCU</b>\n"
    "İlk flag için prompt injection dene, encoding kullan, yaratıcı ol!\n"
    "Sonraki flagler farklı yerlerde gizli — her aşama yeni bir meydan okuma.\n\n"
    "Flag formati: {flag_prefix}{{...}}"
)


async def _build_command_message(user: types.User, template: str | None) -> str:
    """Resolve a /start or /help message template (custom or default)."""
    diff_key = await run_blocking(lambda: bot_state.ai_difficulty_level)
    profile = get_difficulty_profile(diff_key)
    tr_title, tr_blurb = _DIFFICULTY_NOTIFY_COPY_TR.get(diff_key, (profile.label, ""))

    str_uid = str(user.id)
    total_flags = await run_blocking(ctf_flag_repo.get_flag_count)
    found_count = 0
    try:
        progress = await run_blocking(ctf_flag_repo.get_user_progress, str_uid)
        found_count = sum(1 for p in progress if p.get("found_at"))
    except Exception:
        pass
    total_users = 0
    try:
        from database import get_db

        def _count_users():
            with get_db() as conn:
                return conn.execute("SELECT COUNT(*) AS cnt FROM known_users").fetchone()["cnt"]

        total_users = await run_blocking(_count_users)
    except Exception:
        pass

    username = user.username or ""
    first_name = (user.first_name or "").strip()

    template_vars = {
        "welcome": random.choice(WELCOME_MESSAGES),
        "difficulty_symbol": profile.symbol,
        "difficulty_name": tr_title,
        "difficulty_blurb": tr_blurb,
        "difficulty_key": diff_key,
        "first_name": escape(first_name) if first_name else "",
        "username": escape(username) if username else "",
        "user_id": str(user.id),
        "total_flags": str(total_flags),
        "found_count": str(found_count),
        "total_users": str(total_users),
        "bot_name": await run_blocking(lambda: bot_state.bot_name),
        "flag_prefix": await run_blocking(lambda: bot_state.flag_prefix),
    }

    text = template or _DEFAULT_START_TEMPLATE
    return _safe_format(text, template_vars)


@user_router.message(Command("start"))
async def cmd_start(message: types.Message):
    if not message.from_user:
        return
    custom = await run_blocking(lambda: bot_state.start_message)
    menu = await _build_command_message(message.from_user, custom or None)
    await message.answer(menu, parse_mode="HTML")
    await run_blocking(
        conversation_repo.log,
        str(message.from_user.id),
        message.from_user.first_name,
        message.from_user.username or "",
        "/start",
        "İlk etkileşim",
    )


@user_router.message(Command("help"))
async def cmd_help(message: types.Message):
    if not message.from_user:
        return
    custom = await run_blocking(lambda: bot_state.help_message)
    menu = await _build_command_message(message.from_user, custom or None)
    await message.answer(menu, parse_mode="HTML")
    await run_blocking(
        conversation_repo.log,
        str(message.from_user.id),
        message.from_user.first_name,
        message.from_user.username or "",
        "/help",
        "Yardım",
    )


@user_router.message(Command("difficulty"))
async def cmd_difficulty(message: types.Message):
    if not message.from_user:
        return
    diff_key = await run_blocking(lambda: bot_state.ai_difficulty_level)
    profile = get_difficulty_profile(diff_key)
    tr_title, tr_blurb = _DIFFICULTY_NOTIFY_COPY_TR.get(diff_key, (profile.label, ""))
    await message.reply(
        f"{profile.symbol} <b>Zorluk Seviyesi: {tr_title}</b>\n\n"
        f"<i>{tr_blurb}</i>",
        parse_mode="HTML",
    )


@user_router.message(Command("id"))
async def cmd_id(message: types.Message):
    if not message.from_user:
        return
    await message.reply(
        f"<b>Bilgilerin</b>\n\n"
        f"ID: <code>{message.from_user.id}</code>\n"
        f"İsim: {escape(message.from_user.first_name)}\n"
        f"Kullanıcı adı: @{escape(message.from_user.username or 'yok')}",
        parse_mode="HTML",
    )


@user_router.message(Command("reset"))
async def cmd_reset(message: types.Message):
    if not message.from_user:
        return
    await run_blocking(conversation_repo.clear_history, str(message.from_user.id))
    await message.reply(
        "Konuşma sıfırlandı. Geçmişin temizlendi, yeni bir sayfa açtık."
    )


@user_router.message(Command("report"))
async def cmd_report(message: types.Message):
    if not message.from_user:
        return
    msg = message.text.replace("/report", "").strip()
    if not msg:
        return await message.reply("Mesaj yazmalısınız.\n\nKullanım: /report MESAJINIZ")

    report_id = await run_blocking(
        report_repo.save,
        str(message.from_user.id),
        message.from_user.first_name,
        message.from_user.username or "",
        msg,
    )
    await run_blocking(
        log_bot_event,
        "user_report",
        msg,
        str(message.from_user.id),
        message.from_user.first_name,
        message.from_user.username or "",
    )
    await notify_admins(
        f"<b>Rapor #{report_id}</b>\n\n"
        f"{escape(message.from_user.first_name)} · <code>{message.from_user.id}</code>\n\n"
        f"{escape(msg)}",
    )
    await message.reply("Raporunuz iletildi. Teşekkürler.")
