import asyncio
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher

from config import (
    TELEGRAM_TOKEN,
    ADMIN_IDS,
    RATE_LIMIT_SECONDS,
    LOGIC_RATE_LIMIT_WINDOW_SECONDS,
    FLAG_KEYWORD,
    BOT_MODE,
    WEBHOOK_URL,
    WEBHOOK_PATH,
    WEBHOOK_HOST,
    WEBHOOK_PORT,
    WEBHOOK_REUSE_PORT,
    WEBHOOK_SECRET_TOKEN,
    WEBHOOK_MAX_CONNECTIONS,
    WEBHOOK_REGISTER_ON_START,
    METRICS_EXPORT_DIR,
    METRICS_EXPORT_INTERVAL_SECONDS,
    METRICS_EXPORT_MAX_AGE_SECONDS,
    BOT_THREADPOOL_MAX_WORKERS,
    LOG_LEVEL,
    LOG_RETENTION_DAYS,
    BOT_LOG_FILE,
    validate_required_config,
)
from database import init_database
from middleware import (
    MaintenanceMiddleware,
    ModerationMiddleware,
    MessageLogMiddleware,
    start_message_log_worker,
    stop_message_log_worker,
)
from handlers.user import user_router
from handlers.admin import admin_router
from handlers.admin_files import admin_files_router
from handlers.admin_modes import admin_modes_router
from handlers.admin_comms import admin_comms_router
from handlers.admin_moderation import admin_moderation_router
from handlers.competition import competition_router
from handlers.message import message_router


ALL_ROUTERS = [
    user_router,
    admin_router,
    admin_files_router,
    admin_modes_router,
    admin_comms_router,
    admin_moderation_router,
    competition_router,
    message_router,  # catch-all must be registered last
]

# Background task handles (kept alive to prevent GC)
_background_tasks: set[asyncio.Task] = set()
_shutdown_started = False


def _setup_logging():
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    if BOT_LOG_FILE:
        path = BOT_LOG_FILE
        if "{pid}" in path:
            path = path.format(pid=os.getpid())
        else:
            root, ext = os.path.splitext(path)
            ext = ext or ".log"
            path = f"{root}-{os.getpid()}{ext}"
        log_dir = os.path.dirname(path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3)
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler])


async def _graceful_shutdown(bot: Bot, dp: Dispatcher):
    """Graceful shutdown: stop polling, flush DB, close HTTP clients."""
    print("\nShutting down gracefully...")
    try:
        await dp.stop_polling()
    except Exception:
        pass
    try:
        await stop_message_log_worker()
    except Exception:
        pass
    try:
        from notifications import close_client
        await close_client()
    except Exception:
        pass
    try:
        from services import close_engine_if_initialized
        await close_engine_if_initialized()
    except Exception:
        pass
    try:
        from metrics import stop_metrics_exporter
        await stop_metrics_exporter()
    except Exception:
        pass
    try:
        await bot.session.close()
    except Exception:
        pass
    # Cancel background tasks
    for task in list(_background_tasks):
        task.cancel()
    print("Shutdown complete.")


async def _request_shutdown(bot: Bot, dp: Dispatcher, shutdown_event: asyncio.Event):
    global _shutdown_started
    if _shutdown_started:
        return
    _shutdown_started = True
    shutdown_event.set()
    await _graceful_shutdown(bot, dp)


async def _rate_limit_cleanup_loop():
    """Run periodic rate limiter cleanup outside the message hot path."""
    from services import rate_limiter

    interval = max(30, LOGIC_RATE_LIMIT_WINDOW_SECONDS)
    window = LOGIC_RATE_LIMIT_WINDOW_SECONDS * 2

    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(rate_limiter.cleanup, window)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Rate limiter cleanup loop failed")


async def _competition_deadline_loop():
    """Periodically check if competition deadline has passed and auto-end it."""
    from services import competition_repo, user_repo
    from notifications import start_background_broadcast

    while True:
        try:
            await asyncio.sleep(30)
            # Atomic: only one process/call gets ended=True; winners are captured
            # and cleared in the same transaction.
            ended, winners, max_winners = await asyncio.to_thread(
                competition_repo.try_end_by_deadline
            )
            if ended:
                user_ids = await asyncio.to_thread(user_repo.get_all_known_ids)
                announcement = (
                    f"🛑 <b>YARIŞMA SONA ERDİ!</b> (Süre doldu)\n\n"
                    f"🏆 Toplam kazanan: {len(winners)}/{max_winners}\n\n"
                    f"Katılımınız için teşekkürler!"
                )
                await start_background_broadcast(
                    user_ids,
                    announcement,
                    initiated_by="competition_deadline_auto_end",
                )
                logging.info("Competition auto-ended by deadline. Winners: %d/%d", len(winners), max_winners)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Competition deadline check loop failed")


async def _log_retention_cleanup_loop():
    """Periodically delete old conversation_logs and message_log rows."""
    from database import get_db
    from time_utils import db_ago

    interval = 6 * 3600  # every 6 hours
    while True:
        try:
            await asyncio.sleep(interval)
            threshold = db_ago(days=LOG_RETENTION_DAYS)

            def _cleanup():
                with get_db() as conn:
                    conn.execute(
                        "DELETE FROM conversation_logs WHERE timestamp < ?",
                        (threshold,),
                    )
                    conn.execute(
                        "DELETE FROM message_log WHERE timestamp < ?",
                        (threshold,),
                    )

            await asyncio.to_thread(_cleanup)
            logging.info("Log retention cleanup completed (older than %d days)", LOG_RETENTION_DAYS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Log retention cleanup loop failed")


async def main():
    _setup_logging()
    validate_required_config()
    init_database()

    # Thread pool for occasional blocking offloads (DB/file/network wrappers).
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=BOT_THREADPOOL_MAX_WORKERS))

    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher()
    shutdown_event = asyncio.Event()

    # Start async batched message-log writer (keeps DB writes off the hot path).
    await start_message_log_worker()

    # Export metrics snapshots for cross-worker aggregation.
    from metrics import start_metrics_exporter
    await start_metrics_exporter(
        role="bot",
        export_dir=METRICS_EXPORT_DIR,
        interval_seconds=METRICS_EXPORT_INTERVAL_SECONDS,
    )

    # Register middleware (order matters: logging, moderation, then maintenance check)
    dp.message.middleware(MessageLogMiddleware())
    dp.message.middleware(ModerationMiddleware())
    dp.message.middleware(MaintenanceMiddleware())

    # Register all handler routers
    for router in ALL_ROUTERS:
        dp.include_router(router)

    # Start durable broadcast worker loop.
    import notifications
    await notifications.start_broadcast_worker("bot")

    # Keep database-backed limiter cleanup off request path.
    cleanup_task = asyncio.create_task(_rate_limit_cleanup_loop())
    _background_tasks.add(cleanup_task)
    cleanup_task.add_done_callback(_background_tasks.discard)

    # Competition deadline auto-end checker.
    deadline_task = asyncio.create_task(_competition_deadline_loop())
    _background_tasks.add(deadline_task)
    deadline_task.add_done_callback(_background_tasks.discard)

    # Periodic log retention cleanup (delete old conversation/message logs).
    retention_task = asyncio.create_task(_log_retention_cleanup_loop())
    _background_tasks.add(retention_task)
    retention_task.add_done_callback(_background_tasks.discard)

    print("=" * 60)
    import config as _cfg
    print(f"🚀 {_cfg.BOT_NAME} Telegram Bot is starting")
    print("=" * 60)
    print(f"👑 Loaded admin IDs: {ADMIN_IDS}")
    print(f"⚡ In-memory cooldown (per user): {RATE_LIMIT_SECONDS} seconds")
    print(f"🔐 Flag prefix protection active for keyword: {FLAG_KEYWORD}")
    print(f"🌐 Bot mode: {BOT_MODE}")
    print("=" * 60)
    print("✅ Initialization complete.\n")

    # Register graceful shutdown on SIGTERM/SIGINT
    def _signal_handler():
        asyncio.create_task(_request_shutdown(bot, dp, shutdown_event))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # add_signal_handler is unavailable on some platforms (e.g., Windows)
            pass

    try:
        if BOT_MODE == "webhook":
            await _run_webhook(bot, dp, shutdown_event)
        else:
            await _run_polling(bot, dp)
    except asyncio.CancelledError:
        logging.info("Main loop cancelled; proceeding to shutdown")
    finally:
        await _request_shutdown(bot, dp, shutdown_event)


async def _run_polling(bot: Bot, dp: Dispatcher):
    print("Starting Telegram polling...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        # Ctrl+C can interrupt startup network calls; shutdown is handled by caller.
        return


async def _run_webhook(bot: Bot, dp: Dispatcher, shutdown_event: asyncio.Event):
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    secret = WEBHOOK_SECRET_TOKEN or None
    if WEBHOOK_REGISTER_ON_START:
        print(f"Setting webhook: {webhook_url}")
        await bot.set_webhook(
            webhook_url,
            secret_token=secret,
            max_connections=WEBHOOK_MAX_CONNECTIONS,
        )
    else:
        print(f"Webhook registration disabled; serving on {WEBHOOK_HOST}:{WEBHOOK_PORT}{WEBHOOK_PATH}")

    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret)
    handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    async def _health(_request: web.Request) -> web.Response:
        # Lightweight DB connectivity check
        try:
            from database import get_db
            def _ping():
                with get_db() as conn:
                    conn.execute("SELECT 1")
            await asyncio.to_thread(_ping)
            return web.json_response({"status": "ok"})
        except Exception as e:
            logging.error("Health check DB ping failed: %s", e)
            return web.json_response({"status": "degraded", "db": "connection_error"}, status=503)

    async def _metrics(_request: web.Request) -> web.Response:
        from metrics import render_prometheus

        body = render_prometheus(
            export_dir=METRICS_EXPORT_DIR,
            role="bot",
            max_age_seconds=METRICS_EXPORT_MAX_AGE_SECONDS,
        )
        return web.Response(
            text=body,
            headers={"Content-Type": "text/plain; version=0.0.4"},
        )

    app.router.add_get("/health", _health)
    app.router.add_get("/metrics", _metrics)

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT, reuse_port=WEBHOOK_REUSE_PORT)
        await site.start()
    except OSError:
        if WEBHOOK_REUSE_PORT:
            logging.warning("WEBHOOK_REUSE_PORT unsupported; retrying with reuse_port=False")
            site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT, reuse_port=False)
            await site.start()
        else:
            raise
    print(f"Webhook server running on {WEBHOOK_HOST}:{WEBHOOK_PORT}")

    # Keep running until a shutdown signal arrives
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
