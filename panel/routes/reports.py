from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from models.report import ReportRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import get_reports_paginated, get_report_stats, log_audit

router = APIRouter(prefix="/panel")

_report_repo = ReportRepository()


def _to_page(value: str | int | None) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


async def _report_filters(request: Request) -> tuple[str, str, int]:
    search = (request.query_params.get("search") or "").strip()
    status = (request.query_params.get("status") or "").strip()
    page = _to_page(request.query_params.get("page"))

    if request.method == "POST":
        form = await request.form()
        search = str(form.get("search", search)).strip()
        status = str(form.get("status", status)).strip()
        page = _to_page(form.get("page", page))

    return search, status, page


def _reports_redirect_url(search: str, status: str, page: int) -> str:
    params = {}
    if search:
        params["search"] = search
    if status:
        params["status"] = status
    if page > 1:
        params["page"] = str(page)
    if not params:
        return "/panel/reports"
    return f"/panel/reports?{urlencode(params)}"


async def _report_table_response(
    request: Request,
    user: dict,
    search: str,
    status: str,
    page: int,
):
    data = await run_blocking(get_reports_paginated, page=page, search=search, status=status)
    stats = await run_blocking(get_report_stats)
    ctx = {
        "request": request,
        "user": user,
        "search": search,
        "status_filter": status,
        "stats": stats,
        **data,
    }
    return templates.TemplateResponse(request, "reports/_content.html", ctx)


@router.get("/reports")
async def report_list(
    request: Request,
    user: dict = Depends(require_auth),
    search: str = "",
    status: str = "",
    page: int = 1,
):
    data = await run_blocking(
        get_reports_paginated,
        page=max(1, page),
        search=search.strip(),
        status=status.strip(),
    )
    stats = await run_blocking(get_report_stats)
    ctx = {
        "request": request,
        "user": user,
        "search": search,
        "status_filter": status,
        "stats": stats,
        **data,
    }

    is_partial = request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")
    if is_partial:
        return templates.TemplateResponse(request, "reports/_content.html", ctx)
    return templates.TemplateResponse(request, "reports/list.html", ctx)


@router.post("/reports/{report_id}/resolve")
async def resolve_report(
    request: Request,
    report_id: int,
    user: dict = Depends(require_auth),
):
    search, status, page = await _report_filters(request)

    ok = await run_blocking(_report_repo.mark_resolved, report_id, resolved_by=user["username"])
    if ok:
        await run_blocking(log_audit, user["username"], "resolve_report", target=str(report_id))

    is_htmx = request.headers.get("HX-Request")
    if is_htmx:
        return await _report_table_response(request, user, search, status, page)

    return RedirectResponse(
        url=_reports_redirect_url(search, status, page),
        status_code=303,
    )


@router.post("/reports/{report_id}/reopen")
async def reopen_report(
    request: Request,
    report_id: int,
    user: dict = Depends(require_auth),
):
    search, status, page = await _report_filters(request)

    ok = await run_blocking(_report_repo.reopen, report_id)
    if ok:
        await run_blocking(log_audit, user["username"], "reopen_report", target=str(report_id))

    is_htmx = request.headers.get("HX-Request")
    if is_htmx:
        return await _report_table_response(request, user, search, status, page)

    return RedirectResponse(
        url=_reports_redirect_url(search, status, page),
        status_code=303,
    )
