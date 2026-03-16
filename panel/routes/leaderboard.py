from fastapi import APIRouter, Depends, Request

from models.competition import CompetitionRepository
from models.ctf_flags import CTFFlagRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking

router = APIRouter(prefix="/panel")

_comp_repo = CompetitionRepository()
_flag_repo = CTFFlagRepository()


@router.get("/leaderboard")
async def leaderboard_page(
    request: Request,
    user: dict = Depends(require_auth),
):
    flag_board = await run_blocking(_comp_repo.get_flag_leaderboard, 250)
    progress_board = await run_blocking(_flag_repo.get_progress_leaderboard, 250)
    total_flags = await run_blocking(_flag_repo.get_flag_count)
    state = await run_blocking(_comp_repo.get_state)
    if state.get("active"):
        competition_board = await run_blocking(
            _comp_repo.get_current_competition_leaderboard,
            250,
        )
    else:
        competition_board = []
    all_rounds = await run_blocking(_comp_repo.get_recent_rounds, 50)
    rounds = [r for r in all_rounds if str(r.get("status", "")).lower() == "ended"]
    round_ids = [int(r["id"]) for r in rounds if str(r.get("id", "")).isdigit()]
    per_round = 250
    if rounds:
        max_winner_values = [
            int(r.get("max_winners") or 0)
            for r in rounds
            if str(r.get("max_winners", "")).isdigit()
        ]
        per_round = max(
            25,
            min(
                500,
                max(max_winner_values or [25]),
            ),
        )
    round_winners = await run_blocking(
        _comp_repo.get_round_winners_for_rounds,
        round_ids,
        per_round,
    )

    return templates.TemplateResponse(
        request,
        "leaderboard.html",
        {
            "request": request,
            "user": user,
            "competition_active": bool(state.get("active")),
            "flag_board": flag_board,
            "progress_board": progress_board,
            "total_flags": total_flags,
            "competition_board": competition_board,
            "rounds": rounds,
            "round_winners": round_winners,
        },
    )
