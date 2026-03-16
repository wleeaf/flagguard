from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import JSONResponse, RedirectResponse

from models.ctf_flags import CTFFlagRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import log_audit

router = APIRouter(prefix="/panel")

_flag_repo = CTFFlagRepository()


@router.get("/flags")
async def flags_page(
    request: Request,
    user: dict = Depends(require_auth),
    msg: str = "",
    msg_type: str = "success",
):
    flags = await run_blocking(_flag_repo.get_all_flags)
    return templates.TemplateResponse(
        request,
        "flags.html",
        {
            "request": request,
            "user": user,
            "flags": flags,
            "total_flags": len(flags),
            "msg": msg,
            "msg_type": msg_type,
        },
    )


@router.post("/flags")
async def create_flag(
    request: Request,
    user: dict = Depends(require_auth),
    flag_value: str = Form(...),
    success_message: str = Form(""),
    tag: str = Form(""),
    notify_on_find: str = Form(""),
    notify_message: str = Form(""),
):
    flag_value = flag_value.strip()
    if not flag_value:
        return RedirectResponse(
            "/panel/flags?msg=Flag+value+cannot+be+empty&msg_type=error",
            status_code=303,
        )
    notify = notify_on_find.strip().lower() in ("on", "true", "1")
    flag_id = await run_blocking(
        _flag_repo.create_flag, flag_value, success_message.strip(),
        tag=tag.strip(), notify_on_find=notify, notify_message=notify_message.strip(),
    )
    await run_blocking(log_audit, user["username"], "create_flag", detail=f"id={flag_id}")
    return RedirectResponse(
        "/panel/flags?msg=Flag+created+successfully",
        status_code=303,
    )


@router.post("/flags/reorder")
async def reorder_flags(request: Request, user: dict = Depends(require_auth)):
    body = await request.json()
    ordered_ids = body.get("ids", [])
    if not ordered_ids or not all(isinstance(i, int) for i in ordered_ids):
        return JSONResponse({"ok": False, "error": "invalid ids"}, status_code=400)
    await run_blocking(_flag_repo.reorder_flags, ordered_ids)
    await run_blocking(
        log_audit, user["username"], "reorder_flags", detail=f"order={ordered_ids}"
    )
    return JSONResponse({"ok": True})


@router.post("/flags/{flag_id}/update")
async def update_flag(
    request: Request,
    flag_id: int,
    user: dict = Depends(require_auth),
    flag_value: str = Form(...),
    success_message: str = Form(""),
    tag: str = Form(""),
    notify_on_find: str = Form(""),
    notify_message: str = Form(""),
):
    flag_value = flag_value.strip()
    if not flag_value:
        return RedirectResponse(
            "/panel/flags?msg=Flag+value+cannot+be+empty&msg_type=error",
            status_code=303,
        )
    notify = notify_on_find.strip().lower() in ("on", "true", "1")
    updated = await run_blocking(
        _flag_repo.update_flag, flag_id, flag_value, success_message.strip(),
        tag=tag.strip(), notify_on_find=notify, notify_message=notify_message.strip(),
    )
    if not updated:
        return RedirectResponse(
            "/panel/flags?msg=Flag+not+found&msg_type=error",
            status_code=303,
        )
    await run_blocking(log_audit, user["username"], "update_flag", detail=f"id={flag_id}")
    return RedirectResponse(
        "/panel/flags?msg=Flag+updated+successfully",
        status_code=303,
    )


@router.post("/flags/{flag_id}/delete")
async def delete_flag(
    request: Request,
    flag_id: int,
    user: dict = Depends(require_auth),
):
    deleted = await run_blocking(_flag_repo.delete_flag, flag_id)
    if not deleted:
        return RedirectResponse(
            "/panel/flags?msg=Flag+not+found&msg_type=error",
            status_code=303,
        )
    await run_blocking(log_audit, user["username"], "delete_flag", detail=f"id={flag_id}")
    return RedirectResponse(
        "/panel/flags?msg=Flag+deleted+successfully",
        status_code=303,
    )
