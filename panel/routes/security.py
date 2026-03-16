import json

from fastapi import APIRouter, Request, Depends

from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import (
    get_top_jailbreakers,
    get_suspicion_distribution,
    get_security_stats,
)

router = APIRouter(prefix="/panel")


@router.get("/security")
async def security_dashboard(
    request: Request,
    user: dict = Depends(require_auth),
):
    stats = await run_blocking(get_security_stats)
    distribution = await run_blocking(get_suspicion_distribution)
    top = await run_blocking(get_top_jailbreakers, 10)
    labels = ["Low (0-19)", "Medium (20-39)", "High (40-59)", "Critical (60+)"]
    data = [
        distribution["low"], distribution["medium"],
        distribution["high"], distribution["critical"],
    ]

    return templates.TemplateResponse(
        request,
        "security.html",
        {
            "request": request,
            "user": user,
            "stats": stats,
            "distribution": distribution,
            "suspicion_chart_json": json.dumps({"labels": labels, "data": data}),
            "top_jailbreakers": top,
        },
    )
