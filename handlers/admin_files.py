import asyncio
import logging

from aiogram import Router, types
from aiogram.filters import Command

from panel.pg_backup import create_pg_backup
from services import is_admin

admin_files_router = Router()


@admin_files_router.message(Command("backup"))
async def cmd_backup_db(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        backup = await asyncio.to_thread(create_pg_backup)
    except Exception as exc:
        logging.exception("cmd_backup_db failed")
        await message.answer("Backup oluşturulamadı. Lütfen tekrar deneyin.")
        return

    await message.answer(
        f"Backup oluşturuldu.\n`{backup['filename']}`\n{round(backup['size_bytes'] / 1024, 1)} KB",
        parse_mode="Markdown",
    )


@admin_files_router.message(Command("alllogs", "getlog", "deletelog"))
async def cmd_removed_file_logs(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await message.answer(
        "Dosya tabanlı loglama kaldırıldı. Loglar PostgreSQL üzerinden panelde görüntülenir/export edilir."
    )
