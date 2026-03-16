from fastapi import APIRouter, Request, Depends

from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import get_audit_log_paginated

router = APIRouter(prefix="/panel")


@router.get("/audit")
async def audit_log(
    request: Request,
    user: dict = Depends(require_auth),
    page: int = 1,
):
    data = await run_blocking(get_audit_log_paginated, page=max(1, page))

    ctx = {
        "request": request,
        "user": user,
        **data,
    }

    is_partial = request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")
    if is_partial:
        return templates.TemplateResponse(request, "audit/_table.html", ctx)
    return templates.TemplateResponse(request, "audit.html", ctx)
