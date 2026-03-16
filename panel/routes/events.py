from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from database import get_db
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import get_events_paginated, get_event_stats, log_audit

router = APIRouter(prefix="/panel")


def _to_page(value: str | int | None) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _events_redirect_url(search: str, event_type: str, page: int) -> str:
    params = {}
    if search:
        params["search"] = search
    if event_type:
        params["event_type"] = event_type
    if page > 1:
        params["page"] = str(page)
    if not params:
        return "/panel/events"
    return f"/panel/events?{urlencode(params)}"


async def _event_filters(request: Request) -> tuple[str, str, int]:
    search = (request.query_params.get("search") or "").strip()
    event_type = (request.query_params.get("event_type") or "").strip()
    page = _to_page(request.query_params.get("page"))

    if request.method == "POST":
        form = await request.form()
        search = str(form.get("search", search)).strip()
        event_type = str(form.get("event_type", event_type)).strip()
        page = _to_page(form.get("page", page))

    return search, event_type, page


async def _event_content_response(
    request: Request,
    user: dict,
    search: str,
    event_type: str,
    page: int,
):
    data = await run_blocking(
        get_events_paginated,
        page=page,
        search=search,
        event_type=event_type,
    )
    stats = await run_blocking(get_event_stats)
    ctx = {
        "request": request,
        "user": user,
        "search": search,
        "type_filter": event_type,
        "stats": stats,
        **data,
    }
    return templates.TemplateResponse(request, "events/_content.html", ctx)


@router.get("/events")
async def event_list(
    request: Request,
    user: dict = Depends(require_auth),
    search: str = "",
    event_type: str = "",
    page: int = 1,
):
    data = await run_blocking(
        get_events_paginated,
        page=max(1, page),
        search=search.strip(),
        event_type=event_type.strip(),
    )
    stats = await run_blocking(get_event_stats)
    ctx = {
        "request": request,
        "user": user,
        "search": search,
        "type_filter": event_type,
        "stats": stats,
        **data,
    }

    is_partial = request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")
    if is_partial:
        return templates.TemplateResponse(request, "events/_content.html", ctx)
    return templates.TemplateResponse(request, "events/list.html", ctx)


@router.post("/events/{event_id}/delete")
async def delete_event(
    request: Request,
    event_id: int,
    user: dict = Depends(require_auth),
):
    search, event_type, page = await _event_filters(request)

    def _delete():
        with get_db() as conn:
            cur = conn.execute("DELETE FROM bot_events WHERE id = ?", (event_id,))
            return (cur.rowcount or 0) > 0

    deleted = await run_blocking(_delete)
    if deleted:
        await run_blocking(log_audit, user["username"], "delete_event", target=str(event_id))

    is_htmx = request.headers.get("HX-Request")
    if is_htmx:
        return await _event_content_response(request, user, search, event_type, page)

    return RedirectResponse(
        url=_events_redirect_url(search, event_type, page),
        status_code=303,
    )


@router.post("/events/clear")
async def clear_events(
    request: Request,
    user: dict = Depends(require_auth),
):
    def _clear():
        with get_db() as conn:
            cur = conn.execute("DELETE FROM bot_events")
            return int(cur.rowcount or 0)

    count = await run_blocking(_clear)
    await run_blocking(
        log_audit,
        user["username"],
        "clear_all_events",
        detail=f"Deleted {count} events",
    )

    is_htmx = request.headers.get("HX-Request")
    if is_htmx:
        return await _event_content_response(request, user, "", "", 1)

    return RedirectResponse(url="/panel/events", status_code=303)
