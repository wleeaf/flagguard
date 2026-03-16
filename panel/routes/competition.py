from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
import html

from models.competition import CompetitionRepository
from models.user import UserRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import log_audit
import notifications

router = APIRouter(prefix="/panel")

_comp_repo = CompetitionRepository()
_user_repo = UserRepository()


@router.get("/competition")
async def competition_page(
    request: Request,
    user: dict = Depends(require_auth),
    msg: str = "",
):
    state = await run_blocking(_comp_repo.get_state)
    winners = await run_blocking(_comp_repo.get_winners)
    winner_profiles = await run_blocking(_comp_repo.get_winner_profiles)
    max_winners = await run_blocking(_comp_repo.get_max_winners)

    return templates.TemplateResponse(
        request,
        "competition.html",
        {
            "request": request,
            "user": user,
            "state": state,
            "winners": winners,
            "winner_profiles": winner_profiles,
            "max_winners": max_winners,
            "msg": msg,
        },
    )


@router.post("/competition/setup")
async def setup_competition(
    request: Request,
    user: dict = Depends(require_auth),
    answer: str = Form(...),
    question: str = Form(...),
    reward_message: str = Form(""),
    time_limit: str = Form("forever"),
):
    answer = answer.strip().lower()
    question = question.strip()
    reward_message = reward_message.strip()
    if not answer or not question:
        return RedirectResponse(
            "/panel/competition?msg=Answer+and+question+are+required", status_code=303
        )

    deadline = ""
    if time_limit and time_limit != "forever":
        try:
            minutes = int(time_limit)
            if minutes > 0:
                from time_utils import now_local
                from datetime import timedelta
                deadline = (now_local() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    max_winners = await run_blocking(_comp_repo.get_max_winners)

    # Atomic: set_state + clear_winners + start_round in one transaction
    await run_blocking(
        _comp_repo.setup_competition,
        answer=answer,
        question=question,
        reward_message=reward_message,
        deadline=deadline,
        max_winners=max_winners,
        initiated_by=user["username"],
    )

    user_ids = await run_blocking(_user_repo.get_all_known_ids)
    deadline_info = ""
    if deadline:
        deadline_info = f"\nBitiş: {deadline}"
    announcement = (
        f"<b>Yeni Yarışma</b>\n\n"
        f"Soru: {html.escape(question)}\n"
        f"Max kazanan: {max_winners}"
        f"{deadline_info}\n\n"
        f"Cevabını göndermek için: /answer CEVABINIZ"
    )
    job_id = await notifications.start_background_broadcast(
        user_ids,
        announcement,
        initiated_by=user["username"],
    )

    await run_blocking(log_audit, user["username"], "competition_setup", detail=f"answer={answer}")
    await run_blocking(
        log_audit,
        user["username"], "competition_broadcast",
        detail=f"job_id={job_id}, total={len(user_ids)}",
    )
    return RedirectResponse(
        f"/panel/competition?msg=Competition+created+and+broadcast+queued:+job+{job_id}",
        status_code=303,
    )


@router.post("/competition/end")
async def end_competition(
    request: Request,
    user: dict = Depends(require_auth),
):
    # Atomic: capture winners, deactivate, end round, clear winners in one transaction
    winners, max_winners = await run_blocking(_comp_repo.end_competition, "manual_panel")

    user_ids = await run_blocking(_user_repo.get_all_known_ids)
    announcement = (
        f"<b>Yarışma Sona Erdi</b>\n\n"
        f"Toplam kazanan: {len(winners)}/{max_winners}\n\n"
        f"Katılımınız için teşekkürler!"
    )
    job_id = await notifications.start_background_broadcast(
        user_ids,
        announcement,
        initiated_by=user["username"],
    )

    await run_blocking(log_audit, user["username"], "competition_end", detail=f"job_id={job_id}")
    return RedirectResponse(
        f"/panel/competition?msg=Competition+ended;+broadcast+queued:+job+{job_id}",
        status_code=303,
    )


@router.post("/competition/max-winners")
async def set_max_winners(
    request: Request,
    user: dict = Depends(require_auth),
    max_winners: int = Form(...),
):
    if max_winners < 1:
        return RedirectResponse(
            "/panel/competition?msg=Max+winners+must+be+at+least+1", status_code=303
        )
    await run_blocking(_comp_repo.set_max_winners, max_winners)
    await run_blocking(log_audit, user["username"], "set_max_winners", detail=str(max_winners))
    return RedirectResponse(
        f"/panel/competition?msg=Max+winners+set+to+{max_winners}", status_code=303
    )
