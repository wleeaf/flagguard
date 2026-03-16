import logging
from html import escape

from aiogram import Router, types
from aiogram.filters import Command

from services import competition_repo, conversation_repo, user_repo, is_admin
from notifications import start_background_broadcast
from time_utils import parse_db_timestamp, now_local
from handlers._helpers import run_blocking

competition_router = Router()


@competition_router.message(Command("competition_setup"))
async def cmd_setup(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        args = message.text.replace("/competition_setup", "").split("&&")
        if len(args) < 2:
            return await message.answer(
                "Hatalı format.\n\nKullanım:\n/competition_setup CEVAP && SORU"
            )
        answer, question = args[0].strip().lower(), args[1].strip()
        max_winners = await run_blocking(competition_repo.get_max_winners)

        # Atomic: set_state + clear_winners + start_round in one transaction
        await run_blocking(
            competition_repo.setup_competition,
            answer=answer,
            question=question,
            max_winners=max_winners,
            initiated_by=f"tg_admin:{message.from_user.id}",
        )

        user_ids = await run_blocking(user_repo.get_all_known_ids)
        announcement = (
            f"<b>Yeni Yarışma</b>\n\n"
            f"Soru: {escape(question)}\n"
            f"Max kazanan: {max_winners}\n\n"
            f"Cevabını göndermek için: /answer CEVABINIZ"
        )
        job_id = await start_background_broadcast(
            user_ids,
            announcement,
            initiated_by=f"tg_admin:{message.from_user.id}",
        )

        await message.answer(
            f"<b>Yarışma Kuruldu</b>\n\n"
            f"Cevap: <code>{escape(answer)}</code>\n"
            f"Soru: <code>{escape(question)}</code>\n"
            f"Max kazanan: {max_winners}\n\n"
            f"Duyuru kuyruğa alındı: <code>{job_id}</code>",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("cmd_setup failed")
        await message.answer("Yarışma kurulamadı. Lütfen tekrar deneyin.")


@competition_router.message(Command("competition_end"))
async def cmd_end(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        # Atomic: capture winners, deactivate, end round, clear winners in one transaction
        winners, max_winners = await run_blocking(competition_repo.end_competition, "manual_admin")

        user_ids = await run_blocking(user_repo.get_all_known_ids)
        announcement = (
            f"<b>Yarışma Sona Erdi</b>\n\n"
            f"Toplam kazanan: {len(winners)}/{max_winners}\n\n"
            f"Katılımınız için teşekkürler!"
        )
        await start_background_broadcast(
            user_ids,
            announcement,
            initiated_by=f"tg_admin:{message.from_user.id}",
        )

        await message.answer(
            f"Yarışma sonlandırıldı. Toplam kazanan: {len(winners)}/{max_winners}"
        )
    except Exception:
        logging.exception("cmd_end failed")
        await message.answer("Yarışma sonlandırılamadı. Lütfen tekrar deneyin.")


@competition_router.message(Command("competition_status"))
async def cmd_status(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        state = await run_blocking(competition_repo.get_state)
        winners = await run_blocking(competition_repo.get_winner_profiles)
        max_winners = await run_blocking(competition_repo.get_max_winners)
        if not state["active"]:
            return await message.answer("Aktif yarışma yok.")
        deadline_info = ""
        if state.get("deadline"):
            deadline_info = f"\nSüre: {state['deadline']}"
        winner_labels = []
        for row in winners:
            username = (row.get("username") or "").strip()
            if username:
                winner_labels.append(f"@{username}")
            else:
                winner_labels.append((row.get("first_name") or "").strip() or row["user_id"])
        await message.answer(
            f"<b>Yarışma Durumu</b>\n\n"
            f"Cevap: <code>{escape(state['answer'])}</code>\n"
            f"Soru: <code>{escape(state['question'])}</code>\n"
            f"Kazanan: {len(winners)}/{max_winners}"
            f"{deadline_info}\n"
            f"{escape(', '.join(winner_labels))}",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("cmd_status failed")
        await message.answer("Yarışma durumu alınamadı. Lütfen tekrar deneyin.")


@competition_router.message(Command("set_maxwinner_count"))
async def cmd_set_max_winners(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    arg = message.text.replace("/set_maxwinner_count", "").strip()
    if not arg or not arg.isdigit() or int(arg) < 1:
        return await message.answer(
            "Geçerli bir sayı girin.\n\nKullanım: /set_maxwinner_count 10"
        )
    count = int(arg)
    await run_blocking(competition_repo.set_max_winners, count)
    await message.answer(
        f"Max kazanan sayısı <b>{count}</b> olarak güncellendi.",
        parse_mode="HTML",
    )


@competition_router.message(Command("leaderboard"))
async def cmd_leaderboard(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    leaderboard = await run_blocking(competition_repo.get_flag_leaderboard, 15)
    if not leaderboard:
        return await message.answer("Henüz flag bulan yok.")

    lines = []
    for row in leaderboard:
        username = (row.get("username") or "").strip()
        if username:
            who = f"@{username}"
        else:
            who = (row.get("first_name") or "").strip() or row["user_id"]
        lines.append(
            f"{row['rank']}. {escape(who)} — {escape((row.get('first_found_at') or '')[:19])}"
        )

    await message.answer(
        "<b>Flag Leaderboard</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@competition_router.message(Command("competition_leaderboard"))
async def cmd_competition_leaderboard(message: types.Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    leaderboard = await run_blocking(competition_repo.get_competition_leaderboard, 15)
    if not leaderboard:
        return await message.answer("Henüz yarışma kazananı yok.")

    lines = []
    for row in leaderboard:
        username = (row.get("username") or "").strip()
        if username:
            who = f"@{username}"
        else:
            who = (row.get("first_name") or "").strip() or row["user_id"]
        lines.append(f"{row['rank']}. {escape(who)} — {row['wins']} yarışma kazanımı")

    await message.answer(
        "<b>Competition Leaderboard</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@competition_router.message(Command("answer"))
async def cmd_answer(message: types.Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    first_name = message.from_user.first_name
    username = message.from_user.username or ""
    raw_input = (message.text or "/answer").strip() or "/answer"

    async def _reply_and_log(reply_text: str, parse_mode: str | None = None):
        await message.reply(reply_text, parse_mode=parse_mode)
        await run_blocking(
            conversation_repo.log,
            str(uid),
            first_name,
            username,
            raw_input,
            reply_text,
        )

    state = await run_blocking(competition_repo.get_state)
    max_winners = await run_blocking(competition_repo.get_max_winners)
    if not state["active"]:
        return await _reply_and_log("Şu an aktif bir yarışma yok.")
    # Check deadline atomically -- only one caller gets ended=True
    deadline_str = state.get("deadline", "")
    if deadline_str:
        deadline = parse_db_timestamp(deadline_str)
        if deadline and now_local() >= deadline:
            ended, _winners, _mw = await run_blocking(competition_repo.try_end_by_deadline)
            if ended:
                user_ids = await run_blocking(user_repo.get_all_known_ids)
                await start_background_broadcast(
                    user_ids,
                    f"<b>Yarışma Sona Erdi</b> (Süre doldu)\n\n"
                    f"Toplam kazanan: {len(_winners)}/{_mw}\n\n"
                    f"Katılımınız için teşekkürler!",
                    initiated_by="competition_deadline_answer_handler",
                )
            return await _reply_and_log("Yarışma süresi doldu!")

    parts = (message.text or "").split(maxsplit=1)
    guess = parts[1].strip().lower() if len(parts) > 1 else ""
    if not guess:
        return await _reply_and_log("Cevap yazmalısınız.\n\nKullanım: /answer CEVABINIZ")
    if len(guess) > 500:
        return await _reply_and_log("Cevap çok uzun. Maksimum 500 karakter.")
    if guess == state["answer"]:
        # Atomic: advisory lock + insert + capacity check + auto-end in one transaction.
        # Handles duplicate users and capacity races correctly.
        inserted, ended_now = await run_blocking(
            competition_repo.add_winner_with_end_state,
            str(uid),
            max_winners=max_winners,
            first_name=first_name,
            username=username,
        )
        if not inserted:
            return await _reply_and_log("Kontenjan doldu veya zaten kazandınız.")
        winners = await run_blocking(competition_repo.get_winners)
        winner_count = len(winners) if not ended_now else max_winners
        congrats = (
            f"<b>Tebrikler!</b>\n\n"
            f"Soru: {escape(state['question'])}\n"
            f"Kazanan #{winner_count}/{max_winners}"
        )
        reward_msg = state.get("reward_message", "")
        if reward_msg:
            congrats += f"\n\n{escape(reward_msg)}"
        await _reply_and_log(congrats, parse_mode="HTML")
        user_ids = await run_blocking(user_repo.get_all_known_ids)
        if ended_now:
            # Competition auto-ended by capacity; send single combined announcement
            await start_background_broadcast(
                user_ids,
                f"<b>Yarışma Sona Erdi</b>\n\n"
                f"Son kazanan: {escape(message.from_user.first_name)}\n"
                f"Toplam kazanan: {winner_count}/{max_winners}\n\n"
                f"Katılımınız için teşekkürler!",
                initiated_by="competition_auto_end_capacity",
            )
        else:
            await start_background_broadcast(
                user_ids,
                f"<b>Yeni Kazanan</b>\n\n"
                f"{escape(message.from_user.first_name)} yarışmayı kazandı!\n"
                f"Kazanan: {winner_count}/{max_winners}",
                initiated_by="competition_winner_announcement",
            )
    else:
        await _reply_and_log("Yanlış cevap. Tekrar deneyin.")
