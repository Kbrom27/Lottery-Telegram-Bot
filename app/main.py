"""Entry point: FastAPI app that runs the Web Dashboard, Telegram bot, and verification worker.

    uvicorn app.main:app --host 0.0.0.0 --port 8000        (run ONE process; scale workers later)

Routes:
    GET  /                    Web Dashboard, Reports, and Excel Exports
    POST /sms                 receives "money received" SMS from the SMS-forwarder app
    POST /telegram/{secret}   Telegram webhook (only if WEBHOOK_BASE_URL is set; else long polling)
    GET  /healthz
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from aiogram import Bot
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request

from .bot import build_dispatcher, make_review_notifier, safe_send
from .config import Settings, get_settings, load_desktop_settings, resolve_data_dir
from .extract import make_extractor
from .service_manager import BotServiceManager
from .sms import ingest_sms
from .web_ui import mount_web_routes
from .worker import Worker

log = logging.getLogger("main")


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        env_settings = get_settings()
        data_dir = Path(env_settings.storage_dir).parent if env_settings.storage_dir else resolve_data_dir()
        if (data_dir / "config.json").exists():
            settings = load_desktop_settings(data_dir)
            if os.environ.get("DATABASE_URL"):
                settings = replace(settings, database_url=env_settings.database_url)
            if os.environ.get("STORAGE_DIR"):
                settings = replace(settings, storage_dir=env_settings.storage_dir)
        else:
            settings = env_settings
    else:
        data_dir = Path(settings.storage_dir).parent if settings.storage_dir else resolve_data_dir()
    manager = BotServiceManager(data_dir=data_dir, settings=settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if manager.settings.webhook_base_url and not manager.settings.webhook_secret:
            raise RuntimeError("WEBHOOK_SECRET is required when WEBHOOK_BASE_URL is set")
        await manager.init_database()
        app.state.sm = manager.sm

        webhook_tasks: list[asyncio.Task] = []
        if manager.settings.webhook_base_url:
            bot = Bot(manager.settings.bot_token)
            dp = build_dispatcher(manager.sm, manager.settings)

            async def fetch_file(file_id: str) -> bytes:
                f = await bot.get_file(file_id)
                buf = await bot.download_file(f.file_path)
                return buf.read()

            async def notify(tg_id: int, text: str) -> None:
                await safe_send(bot, tg_id, text)

            worker = Worker(
                manager.sm,
                manager.settings,
                make_extractor(manager.settings),
                fetch_file,
                notify,
                make_review_notifier(bot, manager.sm, manager.settings),
            )
            webhook_tasks = [asyncio.create_task(worker.run_forever())]
            await bot.set_webhook(
                f"{manager.settings.webhook_base_url}/telegram/{manager.settings.webhook_secret}",
                secret_token=manager.settings.webhook_secret,
            )
            app.state.bot, app.state.dp = bot, dp
            manager.bot, manager.dp, manager.status = bot, dp, "running"
        elif manager.settings.is_configured:
            await manager.start()
            app.state.bot, app.state.dp = manager.bot, manager.dp

        try:
            yield
        finally:
            for t in webhook_tasks:
                t.cancel()
            if webhook_tasks:
                await asyncio.gather(*webhook_tasks, return_exceptions=True)
            await manager.shutdown()

    app = FastAPI(title="Lottery bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    mount_web_routes(app, manager)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/sms")
    async def sms(request: Request, x_secret: str = Header(default=""), key: str = ""):
        secret = manager.settings.sms_secret
        supplied = (x_secret or key).encode()
        if not secret or not hmac.compare_digest(supplied, secret.encode()):
            raise HTTPException(status_code=401)
        raw = await request.body()
        try:
            body = json.loads(raw)
            text = (body.get("text") or body.get("message") or body.get("body") or "") if isinstance(body, dict) else ""
        except ValueError:
            text = raw.decode(errors="ignore")
        if not text:
            raise HTTPException(status_code=400, detail="no text")
        async with app.state.sm() as s, s.begin():
            status, _ = await ingest_sms(s, text)
        log.info("sms %s", status)
        return {"status": status}

    @app.post("/telegram/{secret}")
    async def telegram_webhook(
        secret: str, request: Request, x_telegram_bot_api_secret_token: str = Header(default="")
    ):
        if not manager.settings.webhook_secret or not (
            hmac.compare_digest(secret.encode(), manager.settings.webhook_secret.encode())
            and hmac.compare_digest(x_telegram_bot_api_secret_token.encode(), manager.settings.webhook_secret.encode())
        ):
            raise HTTPException(status_code=401)
        if not getattr(app.state, "bot", None) or not getattr(app.state, "dp", None):
            raise HTTPException(status_code=503, detail="Bot webhook handler not active")
        try:
            payload = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail="Invalid JSON") from e
        update = Update.model_validate(payload, context={"bot": app.state.bot})
        await app.state.dp.feed_update(app.state.bot, update)
        return {"ok": True}

    return app


app = create_app()  # `uvicorn app.main:app` (nothing connects until the server starts)
