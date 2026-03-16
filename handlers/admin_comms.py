import logging

from aiogram import Router, types
from aiogram.filters import Command
from services import user_repo, is_admin
from panel.queries import log_admin_message
import notifications
from handlers._helpers import run_blocking

admin_comms_router = Router()


@admin_comms_router.message(Command("dm"))
async def cmd_dm(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split(maxsplit=2)
        target, msg = parts[1], parts[2]

        if not target.isdigit():
            return await message.answer("USER_ID sadece rakamlardan oluşmalıdır.")

        await notifications.send_dm(target, msg)
        await run_blocking(
            log_admin_message,
            "dm",
            msg,
            f"tg_admin:{message.from_user.id}",
            target,
        )
        await message.answer("Mesaj iletildi.")
    except IndexError:
        await message.answer("Kullanım: /dm USER_ID MESAJ")
    except Exception as exc:
        logging.exception("cmd_dm failed for target %s", parts[1] if len(parts) > 1 else "?")
        await message.answer("Mesaj gönderilemedi. Lütfen tekrar deneyin.")


@admin_comms_router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    txt = message.text.replace("/broadcast", "").strip()
    if not txt:
        return await message.answer("Mesaj yazmalısınız.")

    try:
        user_ids = await run_blocking(user_repo.get_all_known_ids)
        job_id = await notifications.start_background_broadcast(
            user_ids,
            txt,
            initiated_by=f"tg_admin:{message.from_user.id}",
        )
        await run_blocking(log_admin_message, "broadcast", txt, f"tg_admin:{message.from_user.id}")
        await message.answer(
            f"Duyuru kuyruğa alındı.\n\nHedef: {len(user_ids)} kullanıcı\nİş: <code>{job_id}</code>",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("cmd_broadcast failed")
        await message.answer("Broadcast başarısız. Lütfen tekrar deneyin.")
