import os
import signal
import subprocess
import sys
import time


def _env_truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _wait_for_db() -> None:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return

    timeout_s = float(os.getenv("DOCKER_DB_WAIT_TIMEOUT_SECONDS", "60"))
    deadline = time.monotonic() + max(1.0, timeout_s)

    while True:
        try:
            import psycopg

            conn = psycopg.connect(url, connect_timeout=3)
            conn.close()
            print("Database is ready.", flush=True)
            return
        except Exception as exc:
            if time.monotonic() >= deadline:
                print(f"Database is not ready: {exc}", file=sys.stderr, flush=True)
                raise SystemExit(1)
            print("Waiting for database...", flush=True)
            time.sleep(1.0)


def _exec(argv: list[str], *, env: dict[str, str] | None = None) -> None:
    os.execvpe(argv[0], argv, env or os.environ.copy())


def _run_panel() -> None:
    _exec(["python", "-m", "panel.cli", "run"])


def _run_bot() -> None:
    mode = (os.getenv("BOT_MODE") or "polling").strip().lower()
    workers_raw = (os.getenv("BOT_WEBHOOK_WORKERS") or "1").strip()
    try:
        workers = max(1, int(workers_raw))
    except ValueError:
        workers = 1

    if mode != "webhook" or workers <= 1:
        _exec(["python", "bot.py"])

    reuse_port = _env_truthy(os.getenv("WEBHOOK_REUSE_PORT"), default=True)
    if not reuse_port:
        print(
            "WEBHOOK_REUSE_PORT must be true when BOT_WEBHOOK_WORKERS > 1.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)

    children: list[subprocess.Popen] = []
    stopping = False

    def _spawn(idx: int, *, register_on_start: bool) -> None:
        env = os.environ.copy()
        env["WEBHOOK_REGISTER_ON_START"] = "true" if register_on_start else "false"
        p = subprocess.Popen(["python", "bot.py"], env=env)
        children.append(p)
        print(
            f"Started bot worker {idx}/{workers} pid={p.pid} register_on_start={env['WEBHOOK_REGISTER_ON_START']}",
            flush=True,
        )

    first_register = _env_truthy(os.getenv("WEBHOOK_REGISTER_ON_START"), default=True)
    _spawn(1, register_on_start=first_register)
    for i in range(2, workers + 1):
        _spawn(i, register_on_start=False)

    def _shutdown(_sig: int, _frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("Stopping bot workers...", flush=True)
        for p in children:
            try:
                p.terminate()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        for p in children:
            rc = p.poll()
            if rc is None:
                continue

            if not stopping:
                print(f"Bot worker pid={p.pid} exited rc={rc}; stopping others.", flush=True)
                _shutdown(signal.SIGTERM)

            # Reap all children.
            for p2 in children:
                try:
                    p2.wait(timeout=10)
                except Exception:
                    try:
                        p2.kill()
                    except Exception:
                        pass
            raise SystemExit(rc if rc != 0 else 1)

        time.sleep(0.5)


def main(argv: list[str]) -> int:
    role = (argv[1] if len(argv) > 1 else "bot").strip().lower()

    _wait_for_db()

    if role == "panel":
        _run_panel()
        return 0
    if role == "bot":
        _run_bot()
        return 0

    # Arbitrary command passthrough.
    _exec(argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

