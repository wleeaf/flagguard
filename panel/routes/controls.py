import urllib.parse
import logging

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi import Form

from ai.difficulty import (
    DIFFICULTY_DESCRIPTIONS,
    DIFFICULTY_LABELS,
    DIFFICULTY_LEVELS,
    DIFFICULTY_SYMBOLS,
)
from database import close_connection
from models.bot_state import BotStateRepository
from panel.core import templates
from panel.dependencies import require_auth, run_blocking
from panel.pg_backup import (
    backup_download_path,
    create_pg_backup,
    delete_backup_file,
    restore_pg_backup,
    restore_pg_backup_data_only,
    terminate_other_sessions,
)
from panel.queries import get_backup_list, log_audit
from panel.queries import reset_database, backfill_known_users_from_message_log
import config
import notifications

router = APIRouter(prefix="/panel")

_bot_state = BotStateRepository()

_MODES = {
    "maintenance": ("Maintenance Mode", "Blocks non-admin users"),
    "silent": ("Silent Mode", "Suppresses admin notifications"),
}

_WINNER_NOTIFY_OPTIONS = [
    {"key": "everyone", "label": "Everyone", "description": "Broadcast to all users", "symbol": "📣"},
    {"key": "admins", "label": "Admins", "description": "Notify admins only", "symbol": "🔔"},
    {"key": "none", "label": "None", "description": "No notifications", "symbol": "🔕"},
]

_DIFFICULTY_UI_ORDER = ("EASY", "MEDIUM", "HARD", "IMPOSSIBLE")


def _render_toggle(mode: str, label: str, desc: str, active: bool) -> str:
    sw = "mode-switch-on" if active else "mode-switch-off"
    tr = "translate-x-6" if active else "translate-x-1"
    b_bg = "mode-badge-on" if active else "mode-badge-off"
    b_dot = "mode-dot-on" if active else "mode-dot-off"
    b_txt = "ON" if active else "OFF"
    return (
        f'<div id="toggle-{mode}" class="mode-toggle-row flex items-center justify-between py-3">'
        f'  <div>'
        f'    <span class="mode-toggle-label text-sm font-medium text-gray-700">{label}</span>'
        f'    <p class="mode-toggle-desc text-xs text-gray-400">{desc}</p>'
        f'  </div>'
        f'  <div class="flex items-center gap-3">'
        f'    <span class="mode-toggle-badge inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium {b_bg}">'
        f'      <span class="w-1.5 h-1.5 rounded-full {b_dot}"></span>{b_txt}'
        f'    </span>'
        f'    <button hx-post="/panel/controls/toggle/{mode}"'
        f'            hx-target="#toggle-{mode}"'
        f'            hx-swap="outerHTML"'
        f'            aria-label="Toggle {label}"'
        f'            class="mode-switch relative inline-flex h-6 w-11 shrink-0 items-center rounded-full {sw}'
        f'                   transition-colors cursor-pointer">'
        f'      <span class="mode-switch-thumb inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform {tr}"></span>'
        f'    </button>'
        f'  </div>'
        f'</div>'
    )


@router.get("/controls")
async def controls_page(
    request: Request,
    user: dict = Depends(require_auth),
    backup_msg: str = "",
    backup_msg_type: str = "success",
    difficulty_msg: str = "",
    difficulty_msg_type: str = "success",
    msg_msg: str = "",
    msg_msg_type: str = "success",
    name_msg: str = "",
    name_msg_type: str = "success",
    prefix_msg: str = "",
    prefix_msg_type: str = "success",
):
    modes = await run_blocking(
        lambda: {
            "maintenance": _bot_state.maintenance_mode,
            "silent": _bot_state.silent_mode,
        }
    )
    winner_notify_active = await run_blocking(lambda: _bot_state.winner_notify_target)
    difficulty_active = await run_blocking(lambda: _bot_state.ai_difficulty_level)
    difficulty_items = [
        {
            "key": level,
            "label": DIFFICULTY_LABELS[level],
            "symbol": DIFFICULTY_SYMBOLS[level],
            "description": DIFFICULTY_DESCRIPTIONS[level],
            "active": level == difficulty_active,
        }
        for level in _DIFFICULTY_UI_ORDER
        if level in DIFFICULTY_LEVELS
    ]

    backups = await run_blocking(get_backup_list)
    toggles_html = ""
    for mode, (label, desc) in _MODES.items():
        toggles_html += _render_toggle(mode, label, desc, modes[mode])

    winner_notify_items = [
        {**opt, "active": opt["key"] == winner_notify_active}
        for opt in _WINNER_NOTIFY_OPTIONS
    ]

    start_message = await run_blocking(lambda: _bot_state.start_message)
    help_message = await run_blocking(lambda: _bot_state.help_message)
    current_bot_name = await run_blocking(lambda: _bot_state.bot_name)
    current_flag_prefix = await run_blocking(lambda: _bot_state.flag_prefix)
    bot_name_customized = bool(await run_blocking(lambda: _bot_state.get("bot_name", "").strip()))
    flag_prefix_customized = bool(await run_blocking(lambda: _bot_state.get("flag_prefix", "").strip()))

    return templates.TemplateResponse(
        request,
        "controls.html",
        {
            "request": request,
            "user": user,
            "toggles_html": toggles_html,
            "difficulty_items": difficulty_items,
            "difficulty_active": difficulty_active,
            "difficulty_msg": difficulty_msg,
            "difficulty_msg_type": difficulty_msg_type,
            "winner_notify_items": winner_notify_items,
            "winner_notify_active": winner_notify_active,
            "backups": backups,
            "backup_msg": backup_msg,
            "backup_msg_type": backup_msg_type,
            "start_message": start_message,
            "help_message": help_message,
            "msg_msg": msg_msg,
            "msg_msg_type": msg_msg_type,
            "current_bot_name": current_bot_name,
            "current_flag_prefix": current_flag_prefix,
            "bot_name_customized": bot_name_customized,
            "flag_prefix_customized": flag_prefix_customized,
            "name_msg": name_msg,
            "name_msg_type": name_msg_type,
            "prefix_msg": prefix_msg,
            "prefix_msg_type": prefix_msg_type,
        },
    )


@router.post("/controls/toggle/{mode}")
async def toggle_mode(
    request: Request, mode: str, user: dict = Depends(require_auth),
):
    if mode not in _MODES:
        raise HTTPException(status_code=404)

    result = await notifications.toggle_mode(mode, user["username"])

    label, desc = _MODES[mode]
    await run_blocking(log_audit, user["username"], f"toggle_{mode}", detail=str(result["new_value"]))

    return HTMLResponse(_render_toggle(mode, label, desc, result["new_value"]))


@router.post("/controls/difficulty")
async def set_difficulty(
    request: Request,
    user: dict = Depends(require_auth),
    active: str = Form(""),
):
    normalized = str(active or "").strip().upper()
    if normalized not in DIFFICULTY_LEVELS:
        return _controls_redirect(
            difficulty_msg="Invalid difficulty level.",
            difficulty_msg_type="error",
        )

    previous = await run_blocking(lambda: _bot_state.ai_difficulty_level)
    await run_blocking(setattr, _bot_state, "ai_difficulty_level", normalized)
    await run_blocking(
        log_audit,
        user["username"],
        "set_ai_difficulty",
        detail=f"from={previous};to={normalized}",
    )

    if previous != normalized:
        try:
            await notifications.announce_difficulty_changed(normalized, previous=previous)
        except Exception:
            # Don't break the panel UX if broadcast fails.
            logging.exception("Difficulty change broadcast failed")

    return _controls_redirect(
        difficulty_msg=f"AI difficulty updated: {normalized}",
        difficulty_msg_type="success",
    )


@router.post("/controls/winner-notify")
async def set_winner_notify(
    request: Request,
    user: dict = Depends(require_auth),
    target: str = Form(""),
):
    normalized = (target or "").strip().lower()
    valid = {opt["key"] for opt in _WINNER_NOTIFY_OPTIONS}
    if normalized not in valid:
        return _controls_redirect()

    await notifications.set_winner_notify_target(normalized, user["username"])
    await run_blocking(
        log_audit,
        user["username"],
        "set_winner_notify_target",
        detail=normalized,
    )
    return _controls_redirect()


@router.post("/controls/start-message")
async def save_start_message(
    request: Request,
    user: dict = Depends(require_auth),
    start_message: str = Form(""),
):
    text = start_message.strip()
    await run_blocking(_bot_state.set, "start_message", text)
    await run_blocking(
        log_audit, user["username"], "update_start_message",
        detail=f"len={len(text)}",
    )
    msg = "Start message saved." if text else "Start message cleared (using default)."
    return _controls_redirect(msg_msg=msg, msg_msg_type="success")


@router.post("/controls/start-message/reset")
async def reset_start_message(
    request: Request,
    user: dict = Depends(require_auth),
):
    await run_blocking(_bot_state.delete, "start_message")
    await run_blocking(log_audit, user["username"], "reset_start_message")
    return _controls_redirect(msg_msg="Start message restored to default.", msg_msg_type="success")


@router.post("/controls/help-message")
async def save_help_message(
    request: Request,
    user: dict = Depends(require_auth),
    help_message: str = Form(""),
):
    text = help_message.strip()
    await run_blocking(_bot_state.set, "help_message", text)
    await run_blocking(
        log_audit, user["username"], "update_help_message",
        detail=f"len={len(text)}",
    )
    msg = "Help message saved." if text else "Help message cleared (using default)."
    return _controls_redirect(msg_msg=msg, msg_msg_type="success")


@router.post("/controls/help-message/reset")
async def reset_help_message(
    request: Request,
    user: dict = Depends(require_auth),
):
    await run_blocking(_bot_state.delete, "help_message")
    await run_blocking(log_audit, user["username"], "reset_help_message")
    return _controls_redirect(msg_msg="Help message restored to default.", msg_msg_type="success")


import re as _re

_BOT_NAME_RE = _re.compile(r'^[\w\s\-\.]+$')


@router.post("/controls/bot-name")
async def save_bot_name(
    request: Request,
    user: dict = Depends(require_auth),
    bot_name: str = Form(""),
):
    text = bot_name.strip()
    if not text or len(text) > 50 or not _BOT_NAME_RE.match(text):
        return _controls_redirect(
            name_msg="Invalid bot name (1-50 chars, no special characters).",
            name_msg_type="error",
        )
    await run_blocking(setattr, _bot_state, "bot_name", text)
    await run_blocking(log_audit, user["username"], "update_bot_name", detail=text)
    return _controls_redirect(name_msg=f"Bot name updated: {text}", name_msg_type="success")


@router.post("/controls/bot-name/reset")
async def reset_bot_name(
    request: Request,
    user: dict = Depends(require_auth),
):
    await run_blocking(_bot_state.set, "bot_name", "")
    await run_blocking(log_audit, user["username"], "reset_bot_name")
    return _controls_redirect(
        name_msg=f"Bot name restored to default ({config.BOT_NAME}).",
        name_msg_type="success",
    )


@router.post("/controls/flag-prefix")
async def save_flag_prefix(
    request: Request,
    user: dict = Depends(require_auth),
    flag_prefix: str = Form(""),
):
    text = flag_prefix.strip()
    if not text or len(text) > 20 or not text.isalnum():
        return _controls_redirect(
            prefix_msg="Invalid flag prefix (1-20 chars, alphanumeric only).",
            prefix_msg_type="error",
        )
    await run_blocking(setattr, _bot_state, "flag_prefix", text)
    await run_blocking(log_audit, user["username"], "update_flag_prefix", detail=text)
    return _controls_redirect(prefix_msg=f"Flag prefix updated: {text}", prefix_msg_type="success")


@router.post("/controls/flag-prefix/reset")
async def reset_flag_prefix(
    request: Request,
    user: dict = Depends(require_auth),
):
    await run_blocking(_bot_state.set, "flag_prefix", "")
    await run_blocking(log_audit, user["username"], "reset_flag_prefix")
    return _controls_redirect(
        prefix_msg=f"Flag prefix restored to default ({config.FLAG_PREFIX}).",
        prefix_msg_type="success",
    )


@router.post("/controls/backup")
async def trigger_backup(
    request: Request, user: dict = Depends(require_auth),
):
    try:
        backup = await run_blocking(create_pg_backup)
    except Exception as exc:
        return _backup_redirect(_clean_flash_text(f"Backup failed: {exc}"), "error")

    await run_blocking(
        log_audit,
        user["username"],
        "create_backup",
        detail=backup["filename"],
    )
    return _backup_redirect(
        f'Backup created: {backup["filename"]} ({round(backup["size_bytes"] / 1024, 1)} KB)',
        "success",
    )


@router.get("/controls/backups/{filename}/download")
async def download_backup(
    filename: str,
    user: dict = Depends(require_auth),
):
    try:
        path = await run_blocking(backup_download_path, filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup filename")

    await run_blocking(
        log_audit,
        user["username"],
        "download_backup",
        detail=path.name,
    )

    return FileResponse(
        path=path,
        filename=path.name,
        media_type="application/octet-stream",
    )


@router.post("/controls/backups/{filename}/delete")
async def delete_backup(
    filename: str,
    user: dict = Depends(require_auth),
):
    deleted = await run_blocking(delete_backup_file, filename)
    if not deleted:
        return _backup_redirect("Backup not found", "error")

    await run_blocking(
        log_audit,
        user["username"],
        "delete_backup",
        detail=filename,
    )
    return _backup_redirect(f"Backup deleted: {filename}", "success")


@router.post("/controls/backups/{filename}/restore")
async def restore_backup(
    filename: str,
    user: dict = Depends(require_auth),
    confirm: str = Form(""),
):
    expected_confirm = f"RESTORE {filename}"
    if confirm.strip() != expected_confirm:
        return _backup_redirect(
            f'Confirmation mismatch. Type exactly "{expected_confirm}"',
            "error",
        )

    if not await run_blocking(lambda: _bot_state.maintenance_mode):
        return _backup_redirect(
            "Enable Maintenance Mode before restore.",
            "error",
        )

    worker_stopped = False
    try:
        try:
            await notifications.stop_broadcast_worker()
            worker_stopped = True
        except Exception:
            logging.exception("Failed to stop broadcast worker before restore")

        # Drop pooled connections first so restore lock checks can be accurate.
        await run_blocking(close_connection)
        try:
            restored = await run_blocking(restore_pg_backup, filename)
        except Exception as exc:
            msg = str(exc).lower()
            looks_like_privilege = (
                "lacks privileges to restore" in msg
                or "permission denied" in msg
                or "must be owner" in msg
                or "not owner" in msg
            )
            if not looks_like_privilege:
                raise

            # Fallback: wipe current data (TRUNCATE if allowed; otherwise DELETE) and restore data-only.
            try:
                safety = await run_blocking(create_pg_backup)
                await run_blocking(
                    log_audit,
                    user["username"],
                    "create_backup",
                    detail=f"auto_pre_restore_fallback:{safety['filename']}",
                )
            except Exception:
                logging.exception("Auto safety backup failed before data-only restore fallback")

            await run_blocking(reset_database, keep_panel_users=False)
            await run_blocking(close_connection)
            restored = await run_blocking(restore_pg_backup_data_only, filename)
        # Drop old/stale connections after restore too.
        await run_blocking(close_connection)

        # Keep maintenance mode enabled after restore for safety.
        try:
            await run_blocking(setattr, _bot_state, "maintenance_mode", True)
        except Exception:
            logging.exception("Failed to re-enable maintenance mode after restore")
    except Exception as exc:
        await run_blocking(close_connection)
        return _backup_redirect(f"Restore failed: {exc}", "error")
    finally:
        if worker_stopped:
            try:
                await notifications.start_broadcast_worker("panel")
            except Exception:
                logging.exception("Failed to restart broadcast worker after restore")

    await run_blocking(
        log_audit,
        user["username"],
        "restore_backup",
        detail=restored["filename"],
    )
    return _backup_redirect(
        f'Restore completed from {restored["filename"]} ({round(restored["size_bytes"] / 1024, 1)} KB)',
        "success",
    )


def _backup_redirect(msg: str, msg_type: str = "success") -> RedirectResponse:
    return _controls_redirect(backup_msg=msg, backup_msg_type=msg_type)


def _clean_flash_text(value: str, *, limit: int = 300) -> str:
    # Keep query-string flash messages readable and safe.
    text = str(value or "")
    # Collapse both real newlines and literal backslash-n sequences.
    text = text.replace("\\n", " ")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


@router.post("/controls/db/clear-ai-context")
async def clear_all_ai_context(
    request: Request,
    user: dict = Depends(require_auth),
):
    from models.conversation import ConversationRepository
    conv_repo = ConversationRepository()
    deleted = await run_blocking(conv_repo.clear_all_history)
    await run_blocking(
        log_audit, user["username"], "clear_all_ai_context",
        detail=f"deleted={deleted}",
    )
    return _backup_redirect(f"AI context cleared for all users ({deleted} rows deleted).", "success")


@router.post("/controls/db/backfill-known-users")
async def backfill_known_users(
    request: Request,
    user: dict = Depends(require_auth),
):
    try:
        await run_blocking(backfill_known_users_from_message_log)
    except Exception as exc:
        return _backup_redirect(_clean_flash_text(f"Backfill failed: {exc}"), "error")

    await run_blocking(log_audit, user["username"], "backfill_known_users")
    return _backup_redirect("Backfill completed: known_users updated from message_log", "success")


@router.post("/controls/db/reset")
async def reset_db(
    request: Request,
    user: dict = Depends(require_auth),
    mode: str = Form(""),
    confirm: str = Form(""),
):
    normalized_mode = (mode or "").strip().lower()
    if normalized_mode not in {"keep_admins", "wipe_all"}:
        return _backup_redirect("Invalid reset mode", "error")

    keep_panel_users = normalized_mode == "keep_admins"
    expected = "RESET KEEP_ADMINS" if keep_panel_users else "RESET WIPE_ALL"
    if (confirm or "").strip() != expected:
        return _backup_redirect(f'Confirmation mismatch. Type exactly "{expected}"', "error")

    if not await run_blocking(lambda: _bot_state.maintenance_mode):
        return _backup_redirect("Enable Maintenance Mode before reset.", "error")

    # Automatic safety backup before destructive reset
    try:
        safety_backup = await run_blocking(create_pg_backup)
        await run_blocking(
            log_audit,
            user["username"],
            "create_backup",
            detail=f"auto_pre_reset:{safety_backup['filename']}",
        )
    except Exception:
        logging.exception("Auto safety backup failed before reset")
        return _backup_redirect(
            "Reset aborted: could not create safety backup. Try creating a manual backup first.",
            "error",
        )

    worker_stopped = False
    try:
        try:
            await notifications.stop_broadcast_worker()
            worker_stopped = True
        except Exception:
            logging.exception("Failed to stop broadcast worker before reset")

        await run_blocking(close_connection)
        # Best-effort: clear in-flight DB sessions to avoid hanging on locks.
        try:
            await run_blocking(terminate_other_sessions, only_non_idle=True)
        except Exception:
            logging.exception("Failed to terminate other DB sessions before reset")
        await run_blocking(reset_database, keep_panel_users=keep_panel_users)
        await run_blocking(close_connection)

        # Keep maintenance mode enabled after reset for safety.
        try:
            await run_blocking(setattr, _bot_state, "maintenance_mode", True)
        except Exception:
            logging.exception("Failed to re-enable maintenance mode after reset")
    except Exception as exc:
        await run_blocking(close_connection)
        return _backup_redirect(f"Reset failed: {exc}", "error")
    finally:
        if worker_stopped:
            try:
                await notifications.start_broadcast_worker("panel")
            except Exception:
                logging.exception("Failed to restart broadcast worker after reset")

    await run_blocking(
        log_audit,
        user["username"],
        "reset_database",
        detail=("keep_panel_users=true" if keep_panel_users else "keep_panel_users=false"),
    )
    if keep_panel_users:
        return _backup_redirect("Database reset completed (panel admin users kept).", "success")
    return _backup_redirect("Database reset completed (panel admin users removed).", "success")


def _controls_redirect(**kwargs) -> RedirectResponse:
    cleaned: dict = {}
    for k, v in kwargs.items():
        cleaned[k] = _clean_flash_text(v) if isinstance(v, str) else v
    qs = urllib.parse.urlencode(cleaned)
    return RedirectResponse(f"/panel/controls?{qs}", status_code=303)
