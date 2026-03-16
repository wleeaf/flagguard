import csv
import io

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response

from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import get_logs_paginated, get_all_logs_for_export, get_log_entry, get_log_users

router = APIRouter(prefix="/panel")


@router.get("/logs")
async def log_list(
    request: Request,
    user: dict = Depends(require_auth),
    search: str = "",
    user_id: str = "",
    page: int = 1,
):
    data = await run_blocking(
        get_logs_paginated,
        page=max(1, page),
        per_page=25,
        search=search.strip(),
        user_id=user_id.strip(),
        include_total=False,
    )
    users = await run_blocking(get_log_users)
    ctx = {
        "request": request,
        "user": user,
        "search": search,
        "filter_user_id": user_id,
        "users": users,
        **data,
    }

    is_partial = request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")
    if is_partial:
        return templates.TemplateResponse(request, "logs/_table.html", ctx)
    return templates.TemplateResponse(request, "logs/list.html", ctx)


@router.get("/logs/export")
async def export_csv(
    request: Request,
    user: dict = Depends(require_auth),
    user_id: str = "",
):
    rows = await run_blocking(get_all_logs_for_export, user_id=user_id.strip())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "user_id", "first_name", "username", "user_msg", "ai_msg"])
    for r in rows:
        writer.writerow([
            r["timestamp"], r["user_id"], r["first_name"],
            r["username"], r["user_msg"], r["ai_msg"],
        ])

    buf.seek(0)
    filename = f"logs_{user_id}.csv" if user_id else "logs_all.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/logs/{log_id}/chat")
async def log_chat_detail(
    request: Request,
    log_id: int,
    user: dict = Depends(require_auth),
):
    entry = await run_blocking(get_log_entry, log_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Log entry not found")
    return templates.TemplateResponse(
        request,
        "logs/_chat.html",
        {"request": request, "entry": entry},
    )
