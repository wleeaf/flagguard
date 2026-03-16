"""
CLI for the admin management panel.

Usage:
    python -m panel.cli create-admin
    python -m panel.cli list-admins
    python -m panel.cli change-password
    python -m panel.cli delete-admin
    python -m panel.cli run
"""

import argparse
import getpass

from database import init_database, get_db
from panel.auth import hash_password, validate_password
from time_utils import db_now


def _resolve_username(arg_value: str = "") -> str:
    username = (arg_value or "").strip()
    if not username:
        username = input("Username: ").strip()
    if not username:
        print("Username cannot be empty.")
    return username


def create_admin(username_arg: str = "") -> int:
    init_database()
    username = _resolve_username(username_arg)
    if not username:
        return 1

    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM panel_users WHERE username = ?", (username,)
        ).fetchone()
    if existing:
        print(f"User '{username}' already exists.")
        return 1

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        return 1
    err = validate_password(password)
    if err:
        print(err)
        return 1

    pw_hash = hash_password(password)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO panel_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, pw_hash, db_now()),
        )
    print(f"Admin '{username}' created successfully.")
    return 0


def list_admins() -> int:
    init_database()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT username, is_active, created_at, last_login
               FROM panel_users
               ORDER BY username"""
        ).fetchall()
    if not rows:
        print("No panel users found.")
        return 0

    print("Panel users:")
    for row in rows:
        active = "active" if row["is_active"] else "inactive"
        created = row["created_at"] or "-"
        last_login = row["last_login"] or "-"
        print(f"- {row['username']} ({active}) created={created} last_login={last_login}")
    return 0


def change_password(username_arg: str = "") -> int:
    init_database()
    username = _resolve_username(username_arg)
    if not username:
        return 1

    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM panel_users WHERE username = ?", (username,)
        ).fetchone()
    if not existing:
        print(f"User '{username}' not found.")
        return 1

    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm new password: ")
    if password != confirm:
        print("Passwords do not match.")
        return 1
    err = validate_password(password)
    if err:
        print(err)
        return 1

    pw_hash = hash_password(password)
    with get_db() as conn:
        conn.execute(
            "UPDATE panel_users SET password_hash = ? WHERE username = ?",
            (pw_hash, username),
        )
    print(f"Password updated for '{username}'.")
    return 0


def delete_admin(username_arg: str = "", force: bool = False) -> int:
    init_database()
    username = _resolve_username(username_arg)
    if not username:
        return 1

    with get_db() as conn:
        user_row = conn.execute(
            "SELECT username FROM panel_users WHERE username = ?",
            (username,),
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) AS cnt FROM panel_users").fetchone()["cnt"]

    if not user_row:
        print(f"User '{username}' not found.")
        return 1

    if total <= 1 and not force:
        print("Refusing to delete the last panel user. Re-run with --force to override.")
        return 1

    confirmation = input(f"Type '{username}' to confirm deletion: ").strip()
    if confirmation != username:
        print("Aborted.")
        return 1

    with get_db() as conn:
        conn.execute("DELETE FROM panel_users WHERE username = ?", (username,))
    print(f"User '{username}' deleted.")
    return 0


def run_server():
    import logging
    from logging.handlers import RotatingFileHandler

    import uvicorn
    from config import PANEL_HOST, PANEL_PORT, PANEL_WORKERS, validate_panel_config

    validate_panel_config()
    handler = RotatingFileHandler("panel.log", maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)

    from config import BOT_NAME
    print(
        f"Starting {BOT_NAME} Panel on http://{PANEL_HOST}:{PANEL_PORT} "
        f"(workers={PANEL_WORKERS})"
    )
    uvicorn.run(
        "panel.app:app",
        host=PANEL_HOST,
        port=PANEL_PORT,
        workers=PANEL_WORKERS,
        reload=False,
    )


def main():
    parser = argparse.ArgumentParser(description="Admin Panel CLI")
    sub = parser.add_subparsers(dest="command")
    p_create = sub.add_parser("create-admin", help="Create a panel admin user")
    p_create.add_argument("--username", default="", help="Username (optional; prompts if omitted)")

    sub.add_parser("list-admins", help="List panel users")

    p_change = sub.add_parser("change-password", help="Change password for a panel user")
    p_change.add_argument("--username", default="", help="Username (optional; prompts if omitted)")

    p_delete = sub.add_parser("delete-admin", help="Delete a panel user")
    p_delete.add_argument("--username", default="", help="Username (optional; prompts if omitted)")
    p_delete.add_argument(
        "--force",
        action="store_true",
        help="Allow deleting the last remaining panel user",
    )

    sub.add_parser("run", help="Start the panel server")

    args = parser.parse_args()
    if args.command == "create-admin":
        raise SystemExit(create_admin(args.username))
    elif args.command == "list-admins":
        raise SystemExit(list_admins())
    elif args.command == "change-password":
        raise SystemExit(change_password(args.username))
    elif args.command == "delete-admin":
        raise SystemExit(delete_admin(args.username, force=args.force))
    elif args.command == "run":
        run_server()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
