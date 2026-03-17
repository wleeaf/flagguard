from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse

from ai.difficulty import (
    DIFFICULTY_LEVELS,
    PROFILES,
    get_default_personality_text,
    get_default_document_text,
)
from models.bot_state import BotStateRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import log_audit

router = APIRouter(prefix="/panel")

_bot_state = BotStateRepository()

_DIFFICULTY_UI_ORDER = ("EASY", "MEDIUM", "HARD", "IMPOSSIBLE")

# Legacy per-section keys to clean up on reset
_LEGACY_SECTIONS = ("persona", "safety", "task", "response", "trap")


def _read_prompts_data() -> dict:
    """Read all prompt data from bot_state for template rendering."""
    # Shared personality
    custom_personality = _bot_state.get("prompt_personality", "")
    personality_text = custom_personality if custom_personality else get_default_personality_text()
    personality_customized = bool(custom_personality)

    # Per-difficulty single document
    difficulties = []
    for level in _DIFFICULTY_UI_ORDER:
        if level not in PROFILES:
            continue
        profile = PROFILES[level]
        db_key = f"prompt_{level}_document"
        custom = _bot_state.get(db_key, "")
        default_text = get_default_document_text(level)
        text = custom if custom else default_text
        is_custom = bool(custom)
        difficulties.append({
            "key": level,
            "label": profile.label,
            "symbol": profile.symbol,
            "document": text,
            "customized": is_custom,
        })

    return {
        "personality_text": personality_text,
        "personality_customized": personality_customized,
        "difficulties": difficulties,
    }


@router.get("/prompts")
async def prompts_page(
    request: Request,
    user: dict = Depends(require_auth),
    msg: str = "",
    msg_type: str = "success",
):
    data = await run_blocking(_read_prompts_data)
    return templates.TemplateResponse(
        request,
        "prompts.html",
        {
            "request": request,
            "user": user,
            "msg": msg,
            "msg_type": msg_type,
            **data,
        },
    )


@router.post("/prompts/personality")
async def save_personality(
    request: Request,
    user: dict = Depends(require_auth),
    personality: str = Form(""),
):
    text = personality.strip()
    if not text:
        return _prompts_redirect("Personality rules cannot be empty.", "error")

    await run_blocking(_bot_state.set, "prompt_personality", text)
    await run_blocking(
        log_audit,
        user["username"],
        "update_prompt_personality",
        detail=f"len={len(text)}",
    )
    return _prompts_redirect("Shared personality rules saved.", "success")


@router.post("/prompts/personality/reset")
async def reset_personality(
    request: Request,
    user: dict = Depends(require_auth),
):
    await run_blocking(_bot_state.delete, "prompt_personality")
    await run_blocking(
        log_audit,
        user["username"],
        "reset_prompt_personality",
    )
    return _prompts_redirect("Shared personality rules restored to default.", "success")


@router.post("/prompts/{level}")
async def save_level(
    request: Request,
    level: str,
    user: dict = Depends(require_auth),
    document: str = Form(""),
):
    level = level.upper()
    if level not in DIFFICULTY_LEVELS:
        return _prompts_redirect("Invalid difficulty level.", "error")

    text = document.strip()
    db_key = f"prompt_{level}_document"
    if text:
        await run_blocking(_bot_state.set, db_key, text)
    else:
        await run_blocking(_bot_state.delete, db_key)

    await run_blocking(
        log_audit,
        user["username"],
        "update_prompt_level",
        detail=f"level={level};len={len(text)}",
    )
    return _prompts_redirect(f"{level} prompts saved.", "success")


@router.post("/prompts/{level}/reset")
async def reset_level(
    request: Request,
    level: str,
    user: dict = Depends(require_auth),
):
    level = level.upper()
    if level not in DIFFICULTY_LEVELS:
        return _prompts_redirect("Invalid difficulty level.", "error")

    # Delete the document key
    await run_blocking(_bot_state.delete, f"prompt_{level}_document")

    # Clean up any legacy per-section keys
    for section in _LEGACY_SECTIONS:
        await run_blocking(_bot_state.delete, f"prompt_{level}_{section}")

    await run_blocking(
        log_audit,
        user["username"],
        "reset_prompt_level",
        detail=f"level={level}",
    )
    return _prompts_redirect(f"{level} prompts restored to default.", "success")


def _prompts_redirect(msg: str = "", msg_type: str = "success") -> RedirectResponse:
    import urllib.parse
    qs = urllib.parse.urlencode({"msg": msg, "msg_type": msg_type})
    return RedirectResponse(f"/panel/prompts?{qs}", status_code=303)
