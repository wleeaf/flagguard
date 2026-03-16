import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

from config import BOT_NAME, FLAG_PREFIX

PANEL_DIR = Path(__file__).parent


def _static_hash(filename: str) -> str:
    """Return a short content hash for a static file (cache-busting)."""
    path = PANEL_DIR / "static" / filename
    try:
        digest = hashlib.md5(path.read_bytes()).hexdigest()[:8]
    except FileNotFoundError:
        digest = "0"
    return digest


def _get_bot_name():
    try:
        from models.bot_state import BotStateRepository
        return BotStateRepository().bot_name
    except Exception:
        return BOT_NAME


def _get_flag_prefix():
    try:
        from models.bot_state import BotStateRepository
        return BotStateRepository().flag_prefix
    except Exception:
        return FLAG_PREFIX


templates = Jinja2Templates(directory=PANEL_DIR / "templates")
templates.env.globals["static_hash"] = _static_hash
templates.env.globals["bot_name"] = _get_bot_name
templates.env.globals["flag_prefix"] = _get_flag_prefix
