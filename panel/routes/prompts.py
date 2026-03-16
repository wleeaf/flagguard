from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse

from ai.difficulty import (
    DIFFICULTY_LEVELS,
    PROFILES,
    _PROMPT_SECTIONS,
    get_default_personality_text,
    get_default_section_text,
)
from models.bot_state import BotStateRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.queries import log_audit

router = APIRouter(prefix="/panel")

_bot_state = BotStateRepository()

_SECTION_LABELS = {
    "persona": "Persona Rules",
    "safety": "Safety Rules",
    "task": "Task Rules",
    "response": "Response Rules",
    "trap": "Trap Rules",
}

_DIFFICULTY_UI_ORDER = ("EASY", "MEDIUM", "HARD", "IMPOSSIBLE")


def _read_prompts_data() -> dict:
    """Read all prompt data from bot_state for template rendering."""
    # Shared personality
    custom_personality = _bot_state.get("prompt_personality", "")
    personality_text = custom_personality if custom_personality else get_default_personality_text()
    personality_customized = bool(custom_personality)

    # Per-difficulty sections
    difficulties = []
    for level in _DIFFICULTY_UI_ORDER:
        if level not in PROFILES:
            continue
        profile = PROFILES[level]
        sections = []
        level_customized = False
        for section in _PROMPT_SECTIONS:
            db_key = f"prompt_{level}_{section}"
            custom = _bot_state.get(db_key, "")
            default_text = get_default_section_text(level, section)
            text = custom if custom else default_text
            is_custom = bool(custom)
            if is_custom:
                level_customized = True
            sections.append({
                "key": section,
                "label": _SECTION_LABELS[section],
                "text": text,
                "customized": is_custom,
            })
        difficulties.append({
            "key": level,
            "label": profile.label,
            "symbol": profile.symbol,
            "sections": sections,
            "customized": level_customized,
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
    persona: str = Form(""),
    safety: str = Form(""),
    task: str = Form(""),
    response: str = Form(""),
    trap: str = Form(""),
):
    level = level.upper()
    if level not in DIFFICULTY_LEVELS:
        return _prompts_redirect("Invalid difficulty level.", "error")

    form_data = {
        "persona": persona.strip(),
        "safety": safety.strip(),
        "task": task.strip(),
        "response": response.strip(),
        "trap": trap.strip(),
    }

    saved = []
    for section, text in form_data.items():
        db_key = f"prompt_{level}_{section}"
        if text:
            await run_blocking(_bot_state.set, db_key, text)
            saved.append(section)
        else:
            # Empty means restore default for that section
            await run_blocking(_bot_state.delete, db_key)

    await run_blocking(
        log_audit,
        user["username"],
        "update_prompt_level",
        detail=f"level={level};sections={','.join(saved) if saved else 'all_reset'}",
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

    for section in _PROMPT_SECTIONS:
        db_key = f"prompt_{level}_{section}"
        await run_blocking(_bot_state.delete, db_key)

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
