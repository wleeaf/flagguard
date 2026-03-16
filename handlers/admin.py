import logging

from aiogram import Router, types
from aiogram.filters import Command

from services import user_repo, bot_state, competition_repo, is_admin
from handlers._helpers import run_blocking

admin_router = Router()


@admin_router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        gs = await run_blocking(user_repo.get_global_stats)
        known_count = await run_blocking(user_repo.count_known_users)
        state = await run_blocking(competition_repo.get_state)
        modes = await run_blocking(
            lambda: {
                "maintenance": bot_state.maintenance_mode,
                "silent": bot_state.silent_mode,
                "winner_notify_target": bot_state.winner_notify_target,
                "ai_difficulty": bot_state.ai_difficulty_level,
            }
        )
        _winner_labels = {"everyone": "herkese", "admins": "yöneticilere", "none": "kimseye bildirim yok"}
        winner_label = _winner_labels.get(modes["winner_notify_target"], modes["winner_notify_target"])
        await message.answer(
            f"<b>İstatistikler</b>\n\n"
            f"Kullanıcı: {known_count}\n"
            f"İstek: {gs['total_requests']}\n"
            f"Jailbreak: {gs['total_jailbreaks']}\n"
            f"Honeypot: {gs['total_honeypots']}\n\n"
            f"Yarışma: {'aktif' if state['active'] else 'kapalı'}\n"
            f"Bakım: {'aktif' if modes['maintenance'] else 'normal'}\n"
            f"Sessiz: {'aktif' if modes['silent'] else 'kapalı'}\n"
            f"Kazanan duyurusu: {winner_label}\n"
            f"Zorluk: {modes['ai_difficulty']}",
            parse_mode="HTML",
        )
    except Exception as exc:
        logging.exception("cmd_stats failed")
        await message.answer("İstatistikler alınamadı. Lütfen tekrar deneyin.")


@admin_router.message(Command("user"))
async def cmd_user_stats(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        target = message.text.split()[1]
        if not target.isdigit():
            return await message.answer("USER_ID sadece rakamlardan oluşmalıdır.")

        stats = await run_blocking(user_repo.get_stats, target)
        await message.answer(
            f"<b>Kullanıcı</b>\n\n"
            f"ID: {target}\n"
            f"İstek: {stats['total_requests']}\n"
            f"Jailbreak: {stats['jailbreak_attempts']}\n"
            f"Honeypot: {stats['honeypot_caught']}\n"
            f"Şüphe skoru: {stats['suspicious_score']}",
            parse_mode="HTML",
        )
    except (IndexError, ValueError):
        await message.answer("Kullanım: /user USER_ID")


@admin_router.message(Command("top_jailbreakers"))
async def cmd_top_jailbreakers(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        top = await run_blocking(user_repo.get_top_jailbreakers, 10)
        msg = "<b>Top 10 Jailbreakers</b>\n\n"
        for i, d in enumerate(top, 1):
            msg += f"{i}. {d['user_id']}: {d['jailbreak_attempts']} deneme\n"
        await message.answer(msg if top else "Henüz veri yok.", parse_mode="HTML")
    except Exception as exc:
        logging.exception("cmd_top_jailbreakers failed")
        await message.answer("Veriler alınamadı. Lütfen tekrar deneyin.")


@admin_router.message(Command("admin"))
async def cmd_admin_help(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Komutlar</b>\n\n"
        "/stats · /user ID · /top_jailbreakers\n"
        "/maintenance · /silent · /winner_broadcast\n"
        "/dm ID MESAJ · /broadcast MESAJ\n"
        "/competition_setup CEVAP &amp;&amp; SORU\n"
        "/competition_status · /competition_end\n"
        "/set_maxwinner_count SAYI\n"
        "/leaderboard · /competition_leaderboard\n"
        "/backup",
        parse_mode="HTML",
    )
