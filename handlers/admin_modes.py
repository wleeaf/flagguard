import logging

from aiogram import Router, types
from aiogram.filters import Command

from handlers._helpers import run_blocking
from models.admin import AdminRepository
from services import is_admin
import notifications

admin_modes_router = Router()


@admin_modes_router.message(Command("maintenance"))
async def cmd_maintenance(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        result = await notifications.toggle_mode("maintenance", message.from_user.first_name)
        state = "aktif" if result.get("new_value") else "kapalı"
        await message.answer(f"Bakım modu: {state}")
    except Exception:
        logging.exception("cmd_maintenance failed")
        await message.answer("Mod değiştirilemedi. Lütfen tekrar deneyin.")


@admin_modes_router.message(Command("silent"))
async def cmd_silent(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        admin_repo = AdminRepository()
        tid = str(message.from_user.id)
        # Ensure DB row exists (env-only admins)
        await run_blocking(admin_repo.add_admin, tid, message.from_user.first_name or "")
        new_val = await run_blocking(admin_repo.toggle_silent, tid)
        state = "aktif" if new_val else "kapalı"
        await message.answer(f"Sessiz mod (kişisel): {state}")
    except Exception:
        logging.exception("cmd_silent failed")
        await message.answer("Mod değiştirilemedi. Lütfen tekrar deneyin.")


@admin_modes_router.message(Command("winner_broadcast"))
async def cmd_winner_broadcast(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        from models.bot_state import BotStateRepository
        current = await run_blocking(lambda: BotStateRepository().winner_notify_target)
        cycle = notifications._WINNER_NOTIFY_CYCLE
        next_target = cycle[(cycle.index(current) + 1) % len(cycle)]
        await notifications.set_winner_notify_target(next_target, message.from_user.first_name)
        label = notifications._WINNER_NOTIFY_LABELS[next_target]
        await message.answer(f"Kazanan duyurusu: {label}")
    except Exception:
        logging.exception("cmd_winner_broadcast failed")
        await message.answer("Mod değiştirilemedi. Lütfen tekrar deneyin.")
