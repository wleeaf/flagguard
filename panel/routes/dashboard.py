import json

from fastapi import APIRouter, Query, Request, Depends

from models.user import UserRepository
from models.bot_state import BotStateRepository
from models.competition import CompetitionRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import get_requests_per_hour, get_recent_activity

router = APIRouter(prefix="/panel")

_user_repo = UserRepository()
_bot_state = BotStateRepository()
_comp_repo = CompetitionRepository()

_CHART_RANGES: dict[str, dict] = {
    "24h": {"hours": 24, "daily": False, "label": "24h"},
    "3d":  {"hours": 72, "daily": False, "label": "3 days"},
    "7d":  {"hours": 168, "daily": True, "label": "7 days"},
    "30d": {"hours": 720, "daily": True, "label": "30 days"},
}


@router.get("/")
async def dashboard(
    request: Request,
    user: dict = Depends(require_auth),
    range: str = Query("24h", alias="range"),
):
    cfg = _CHART_RANGES.get(range, _CHART_RANGES["24h"])
    active_range = range if range in _CHART_RANGES else "24h"

    stats = await run_blocking(_user_repo.get_global_stats)
    modes = await run_blocking(
        lambda: {
            "maintenance": _bot_state.maintenance_mode,
            "silent": _bot_state.silent_mode,
            "winner_notify_target": _bot_state.winner_notify_target,
            "ai_difficulty": _bot_state.ai_difficulty_level,
        }
    )
    competition = await run_blocking(_comp_repo.get_state)
    competition["winners"] = await run_blocking(_comp_repo.get_winners)
    competition["max_winners"] = await run_blocking(_comp_repo.get_max_winners)
    rows = await run_blocking(get_requests_per_hour, cfg["hours"], cfg["daily"])
    activity = await run_blocking(get_recent_activity, 20)

    if cfg["daily"]:
        # "2026-02-22" → "02/22"
        labels = [h["hour"][5:].replace("-", "/") for h in rows]
    elif cfg["hours"] > 24:
        # "2026-02-22 14:00" → "02/22 14h"
        labels = [h["hour"][5:11].replace("-", "/") + h["hour"][11:13] + "h" for h in rows]
    else:
        # "2026-02-22 14:00" → "14:00"
        labels = [h["hour"][-5:] for h in rows]
    counts = [h["count"] for h in rows]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "stats": stats,
            "modes": modes,
            "competition": competition,
            "requests_chart_json": json.dumps({"labels": labels, "data": counts}),
            "activity": activity,
            "chart_ranges": _CHART_RANGES,
            "active_range": active_range,
        },
    )
