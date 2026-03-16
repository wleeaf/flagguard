from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from config import PANEL_SECRET_KEY, PANEL_TOKEN_EXPIRE_HOURS

_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=PANEL_TOKEN_EXPIRE_HOURS)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, PANEL_SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, PANEL_SECRET_KEY, algorithms=[_ALGORITHM])
    except Exception:
        return None


def validate_password(password: str) -> str | None:
    """Return an error message if the password is invalid, or None if OK."""
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if len(password.encode("utf-8")) > 72:
        return "Password too long (bcrypt truncates at 72 bytes). Please shorten it."
    if password == password[0] * len(password):
        return "Password must not be a single repeated character."
    has_lower = any(c.islower() for c in password)
    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_lower and has_upper and has_digit):
        return "Password must contain at least one lowercase letter, one uppercase letter, and one digit."
    return None
