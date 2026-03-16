import os
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from config import (
    DATABASE_URL,
    PANEL_BACKUP_DIR,
    PANEL_BACKUP_RETENTION,
    PANEL_BACKUP_TIMEOUT_SECONDS,
)


_ALLOWED_SUFFIXES = (".dump", ".sql", ".backup")
_RESTORE_LOCK = threading.Lock()
_RESTORE_LOCK_TIMEOUT_SECONDS = int(os.getenv("PANEL_RESTORE_LOCK_TIMEOUT_SECONDS", "10"))
_TAIL_BYTES_LIMIT = 32 * 1024


def _backup_dir() -> Path:
    backup_dir = Path(PANEL_BACKUP_DIR).expanduser()
    if not backup_dir.is_absolute():
        project_root = Path(__file__).resolve().parents[1]
        backup_dir = project_root / backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def _resolve_backup_path(filename: str) -> Path:
    if not filename:
        raise ValueError("Invalid backup filename")
    if "/" in filename or "\\" in filename:
        raise ValueError("Invalid backup filename")
    if not filename.endswith(_ALLOWED_SUFFIXES):
        raise ValueError("Invalid backup filename")

    path = _backup_dir() / filename
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(filename)
    return path


def _conninfo_to_pg_env() -> tuple[dict[str, str], str]:
    try:
        from psycopg.conninfo import conninfo_to_dict
    except Exception as exc:
        raise RuntimeError("psycopg is unavailable; cannot parse DATABASE_URL") from exc

    try:
        conn = conninfo_to_dict(DATABASE_URL)
    except Exception as exc:
        raise RuntimeError("Failed to parse DATABASE_URL for pg_dump") from exc

    dbname = str(conn.get("dbname") or conn.get("database") or "").strip()
    if not dbname:
        raise RuntimeError("DATABASE_URL does not include a database name")

    env_map = {
        "host": "PGHOST",
        "hostaddr": "PGHOSTADDR",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
        "sslcrl": "PGSSLCRL",
        "passfile": "PGPASSFILE",
        "options": "PGOPTIONS",
        "target_session_attrs": "PGTARGETSESSIONATTRS",
    }

    pg_env: dict[str, str] = {}
    for key, env_name in env_map.items():
        value = conn.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            pg_env[env_name] = value

    return pg_env, dbname


def _apply_retention(backup_dir: Path):
    keep = PANEL_BACKUP_RETENTION
    if keep <= 0:
        return

    files = [
        p for p in backup_dir.iterdir()
        if p.is_file() and p.name.endswith(_ALLOWED_SUFFIXES)
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except FileNotFoundError:
            continue


def create_pg_backup() -> dict:
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RuntimeError("pg_dump is not installed or not in PATH")

    pg_env, dbname = _conninfo_to_pg_env()
    backup_dir = _backup_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(4)
    filename = f"aishield_{timestamp}_{suffix}.dump"
    output_path = backup_dir / filename

    env = os.environ.copy()
    env.update(pg_env)

    cmd = [
        pg_dump,
        "--format=custom",
        "--file", str(output_path),
        "--no-owner",
        "--no-privileges",
        dbname,
    ]

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=PANEL_BACKUP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"pg_dump timed out after {PANEL_BACKUP_TIMEOUT_SECONDS}s"
        ) from exc

    if result.returncode != 0:
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        err = (result.stderr or result.stdout or "unknown error").strip()
        err = " ".join(err.split())
        if err in {"pg_dump: error:", "pg_dump: error"}:
            err = "no error details available (check DATABASE_URL connectivity and pg_hba/SSL settings)"
        if len(err) > 500:
            err = err[:500] + "..."
        raise RuntimeError(f"pg_dump failed: {err}")

    try:
        size_bytes = output_path.stat().st_size
    except FileNotFoundError as exc:
        raise RuntimeError("Backup file was not created") from exc

    _apply_retention(backup_dir)
    return {
        "filename": filename,
        "path": str(output_path),
        "size_bytes": int(size_bytes),
    }


def _tail_bytes_append(buf: bytearray, chunk: bytes, *, limit: int = _TAIL_BYTES_LIMIT) -> None:
    if not limit:
        return
    buf.extend(chunk)
    if len(buf) > limit:
        del buf[:-limit]


def _drain_stream_tail(stream, buf: bytearray) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            _tail_bytes_append(buf, chunk)
    except Exception:
        return


def _decode_tail(buf: bytearray) -> str:
    try:
        return bytes(buf).decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _is_transaction_timeout_guc_error(err: str) -> bool:
    lower = (err or "").lower()
    return "unrecognized configuration parameter" in lower and "transaction_timeout" in lower


def _strip_unsupported_restore_gucs(line: bytes) -> bytes | None:
    # Restore compatibility when pg_dump/pg_restore is newer than the target server.
    stripped = line.lstrip()
    if stripped.startswith(b"SET transaction_timeout"):
        return None
    if stripped.startswith(b"SET TRANSACTION_TIMEOUT"):
        return None
    return line


def _restore_dump_via_psql(
    *,
    backup_path: Path,
    env: dict[str, str],
    dbname: str,
    data_only: bool,
    timeout_seconds: int,
) -> None:
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("pg_restore is not installed or not in PATH")
    psql = shutil.which("psql")
    if not psql:
        raise RuntimeError("psql is not installed or not in PATH")

    # Stream SQL from pg_restore into psql so we can strip problematic
    # (newer-version) SET commands before they hit an older server.
    restore_cmd = [
        pg_restore,
        "--no-owner",
        "--no-privileges",
        "-f", "-",
    ]
    if data_only:
        restore_cmd.append("--data-only")
    else:
        restore_cmd.extend(["--clean", "--if-exists"])
    restore_cmd.append(str(backup_path))

    psql_cmd = [
        psql,
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "--dbname",
        dbname,
    ]

    started = time.monotonic()
    pg = subprocess.Popen(
        restore_cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    psql_p = subprocess.Popen(
        psql_cmd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    assert pg.stdout is not None
    assert pg.stderr is not None
    assert psql_p.stdin is not None
    assert psql_p.stderr is not None

    pg_err = bytearray()
    psql_err = bytearray()
    pg_err_t = threading.Thread(
        target=_drain_stream_tail, args=(pg.stderr, pg_err), daemon=True
    )
    psql_err_t = threading.Thread(
        target=_drain_stream_tail, args=(psql_p.stderr, psql_err), daemon=True
    )
    pg_err_t.start()
    psql_err_t.start()

    broken_pipe = False
    try:
        for line in iter(pg.stdout.readline, b""):
            out = _strip_unsupported_restore_gucs(line)
            if out is None:
                continue
            try:
                psql_p.stdin.write(out)
            except BrokenPipeError:
                broken_pipe = True
                break
    finally:
        try:
            psql_p.stdin.close()
        except Exception:
            pass
        try:
            pg.stdout.close()
        except Exception:
            pass

    if broken_pipe and pg.poll() is None:
        try:
            pg.terminate()
        except Exception:
            pass

    remaining = max(1.0, float(timeout_seconds) - (time.monotonic() - started))
    try:
        pg_rc = pg.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        try:
            pg.kill()
        except Exception:
            pass
        raise RuntimeError("pg_restore timed out")

    remaining = max(1.0, float(timeout_seconds) - (time.monotonic() - started))
    try:
        psql_rc = psql_p.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        try:
            psql_p.kill()
        except Exception:
            pass
        raise RuntimeError("psql restore timed out")

    # Best-effort: ensure drain threads complete.
    pg_err_t.join(timeout=1.0)
    psql_err_t.join(timeout=1.0)

    if pg_rc != 0:
        err = _decode_tail(pg_err) or "unknown error"
        err = " ".join(err.split())
        if len(err) > 500:
            err = err[:500] + "..."
        raise RuntimeError(f"pg_restore failed: {err}")

    if psql_rc != 0:
        err = _decode_tail(psql_err) or "unknown error"
        err = " ".join(err.split())
        if len(err) > 500:
            err = err[:500] + "..."
        raise RuntimeError(f"pg_restore failed: {err}")


def _get_active_non_idle_sessions(limit: int = 10) -> list[dict]:
    return _get_active_sessions(limit=limit, include_idle=False, only_current_user=True)


def _get_active_sessions(
    *,
    limit: int = 10,
    include_idle: bool = False,
    only_current_user: bool = True,
) -> list[dict]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:
        raise RuntimeError("psycopg is unavailable; cannot inspect active sessions") from exc

    state_filter = "" if include_idle else "AND state <> 'idle'"
    user_filter = "AND usename = current_user" if only_current_user else ""

    try:
        with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    pid,
                    usename,
                    COALESCE(application_name, '') AS application_name,
                    COALESCE(state, '') AS state,
                    COALESCE(client_addr::text, 'local') AS client_addr
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  {user_filter}
                  {state_filter}
                ORDER BY query_start NULLS LAST
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        raise RuntimeError(f"Could not inspect active sessions: {exc}") from exc


def terminate_other_sessions(*, only_non_idle: bool = True) -> dict:
    """
    Best-effort terminate other sessions connected as the same DB user.

    This helps restore/reset avoid hanging on locks when the bot/panel is still running.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:
        raise RuntimeError("psycopg is unavailable; cannot terminate sessions") from exc

    state_filter = "AND state <> 'idle'" if only_non_idle else ""
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
            matched = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND usename = current_user
                  {state_filter}
                """
            ).fetchone()["cnt"]

            rows = conn.execute(
                f"""
                SELECT pid, pg_terminate_backend(pid) AS terminated
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND usename = current_user
                  {state_filter}
                """
            ).fetchall()
            terminated = sum(1 for r in rows if r.get("terminated"))
            return {"matched": int(matched or 0), "terminated": int(terminated or 0)}
    except Exception as exc:
        raise RuntimeError(f"Could not terminate sessions: {exc}") from exc


def restore_pg_backup(filename: str) -> dict:
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("pg_restore is not installed or not in PATH")

    backup_path = _resolve_backup_path(filename)
    if not filename.endswith(".dump"):
        raise RuntimeError("Restore currently supports only .dump backups")

    if not _RESTORE_LOCK.acquire(blocking=False):
        raise RuntimeError("Another restore operation is already running")
    try:
        # Best-effort: clear other in-flight sessions for this DB user to reduce lock contention.
        # We don't hard-fail if there are sessions; pg_restore will fail fast via lock_timeout.
        try:
            terminate_other_sessions(only_non_idle=True)
        except Exception:
            # Keep going; restore may still work if the DB isn't busy.
            pass

        pg_env, dbname = _conninfo_to_pg_env()
        env = os.environ.copy()
        env.update(pg_env)
        extra_opts = []
        if _RESTORE_LOCK_TIMEOUT_SECONDS > 0:
            extra_opts.append(f"-c lock_timeout={_RESTORE_LOCK_TIMEOUT_SECONDS}s")
        # Avoid inheriting low statement timeouts from DB/user config; subprocess timeout is the guardrail.
        extra_opts.append("-c statement_timeout=0")
        if extra_opts:
            current = (env.get("PGOPTIONS") or "").strip()
            env["PGOPTIONS"] = (current + " " + " ".join(extra_opts)).strip() if current else " ".join(extra_opts)

        cmd = [
            pg_restore,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "--exit-on-error",
            "--dbname", dbname,
            str(backup_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=max(PANEL_BACKUP_TIMEOUT_SECONDS * 3, 300),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("pg_restore timed out") from exc

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            err = " ".join(err.split())
            if _is_transaction_timeout_guc_error(err):
                _restore_dump_via_psql(
                    backup_path=backup_path,
                    env=env,
                    dbname=dbname,
                    data_only=False,
                    timeout_seconds=max(PANEL_BACKUP_TIMEOUT_SECONDS * 3, 300),
                )
                return {
                    "filename": backup_path.name,
                    "size_bytes": int(backup_path.stat().st_size),
                }
            lower = err.lower()
            if "lock timeout" in lower:
                err = (
                    "database is busy (lock timeout). Enable Maintenance Mode, stop bot/webhook workers, "
                    f"and retry. Detail: {err}"
                )
            elif "permission denied" in lower or "must be owner" in lower:
                err = (
                    "database user lacks privileges to restore (ownership/DDL required). "
                    f"Use a DB owner role in DATABASE_URL or grant required privileges. Detail: {err}"
                )
            elif err in {"pg_restore: error:", "pg_restore: error"}:
                err = "no error details available (check permissions/active connections)"
            if len(err) > 500:
                err = err[:500] + "..."
            raise RuntimeError(f"pg_restore failed: {err}")

        return {
            "filename": backup_path.name,
            "size_bytes": int(backup_path.stat().st_size),
        }
    finally:
        _RESTORE_LOCK.release()


def restore_pg_backup_data_only(filename: str) -> dict:
    """
    Restore data only (no DROP/CREATE).

    Useful when the DB role cannot run a full --clean restore, but can INSERT/UPDATE.
    Callers should wipe tables first (reset) to avoid duplicate rows.
    """
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("pg_restore is not installed or not in PATH")

    backup_path = _resolve_backup_path(filename)
    if not filename.endswith(".dump"):
        raise RuntimeError("Restore currently supports only .dump backups")

    if not _RESTORE_LOCK.acquire(blocking=False):
        raise RuntimeError("Another restore operation is already running")
    try:
        pg_env, dbname = _conninfo_to_pg_env()
        env = os.environ.copy()
        env.update(pg_env)
        extra_opts = []
        if _RESTORE_LOCK_TIMEOUT_SECONDS > 0:
            extra_opts.append(f"-c lock_timeout={_RESTORE_LOCK_TIMEOUT_SECONDS}s")
        extra_opts.append("-c statement_timeout=0")
        if extra_opts:
            current = (env.get("PGOPTIONS") or "").strip()
            env["PGOPTIONS"] = (current + " " + " ".join(extra_opts)).strip() if current else " ".join(extra_opts)

        cmd = [
            pg_restore,
            "--data-only",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "--exit-on-error",
            "--dbname", dbname,
            str(backup_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=max(PANEL_BACKUP_TIMEOUT_SECONDS * 3, 300),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("pg_restore (data-only) timed out") from exc

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            err = " ".join(err.split())
            if _is_transaction_timeout_guc_error(err):
                _restore_dump_via_psql(
                    backup_path=backup_path,
                    env=env,
                    dbname=dbname,
                    data_only=True,
                    timeout_seconds=max(PANEL_BACKUP_TIMEOUT_SECONDS * 3, 300),
                )
                return {
                    "filename": backup_path.name,
                    "size_bytes": int(backup_path.stat().st_size),
                }
            lower = err.lower()
            if "lock timeout" in lower:
                err = (
                    "database is busy (lock timeout). Enable Maintenance Mode, stop bot/webhook workers, "
                    f"and retry. Detail: {err}"
                )
            if len(err) > 500:
                err = err[:500] + "..."
            raise RuntimeError(f"pg_restore (data-only) failed: {err}")

        return {
            "filename": backup_path.name,
            "size_bytes": int(backup_path.stat().st_size),
        }
    finally:
        _RESTORE_LOCK.release()


def delete_backup_file(filename: str) -> bool:
    try:
        path = _resolve_backup_path(filename)
    except FileNotFoundError:
        return False
    except ValueError:
        return False

    path.unlink(missing_ok=False)
    return True


def backup_download_path(filename: str) -> Path:
    return _resolve_backup_path(filename)


def backup_dir_path() -> Path:
    return _backup_dir()
