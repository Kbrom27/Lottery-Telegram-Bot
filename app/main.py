"""Entry point: FastAPI app that also runs the Telegram bot and the verification worker.

    uvicorn app.main:app --host 0.0.0.0 --port 8000        (run ONE process; scale workers later)

Routes:
    POST /sms                 receives "money received" SMS from the SMS-forwarder app
    POST /telegram/{secret}   Telegram webhook (only if WEBHOOK_BASE_URL is set; else long polling)
    GET  /healthz
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager

from aiogram import Bot
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request

from .bot import build_dispatcher, make_review_notifier, safe_send
from .config import Settings, get_settings
from .db import init_db, make_engine, make_sessionmaker
from .extract import make_extractor
from .sms import ingest_sms
from .worker import Worker

log = logging.getLogger("main")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if settings.webhook_base_url and not settings.webhook_secret:
            raise RuntimeError("WEBHOOK_SECRET is required when WEBHOOK_BASE_URL is set")
        engine = make_engine(settings.database_url)
        sm = make_sessionmaker(engine)
        await init_db(engine, settings)

        bot = Bot(settings.bot_token)
        dp = build_dispatcher(sm, settings)

        async def fetch_file(file_id: str) -> bytes:
            f = await bot.get_file(file_id)
            buf = await bot.download_file(f.file_path)
            return buf.read()

        async def notify(tg_id: int, text: str) -> None:
            await safe_send(bot, tg_id, text)

        worker = Worker(sm, settings, make_extractor(settings), fetch_file, notify, make_review_notifier(bot, sm, settings))
        tasks = [asyncio.create_task(worker.run_forever())]
        if settings.webhook_base_url:
            await bot.set_webhook(
                f"{settings.webhook_base_url}/telegram/{settings.webhook_secret}",
                secret_token=settings.webhook_secret,
            )
        else:
            await bot.delete_webhook()
            tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))

        app.state.sm, app.state.bot, app.state.dp = sm, bot, dp
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await bot.session.close()
            await engine.dispose()

    app = FastAPI(title="Lottery bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/sms")
    async def sms(request: Request, x_secret: str = Header(default=""), key: str = ""):
        supplied = (x_secret or key).encode()
        if not settings.sms_secret or not hmac.compare_digest(supplied, settings.sms_secret.encode()):
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
            status, _ = await ingest_sms(s, text)  # arrival time = now (never trust the sender's clock)
        log.info("sms %s", status)
        return {"status": status}

    @app.post("/telegram/{secret}")
    async def telegram_webhook(
        secret: str, request: Request, x_telegram_bot_api_secret_token: str = Header(default="")
    ):
        if not settings.webhook_secret or not (
            hmac.compare_digest(secret.encode(), settings.webhook_secret.encode())
            and hmac.compare_digest(x_telegram_bot_api_secret_token.encode(), settings.webhook_secret.encode())
        ):
            raise HTTPException(status_code=401)
        update = Update.model_validate(await request.json(), context={"bot": app.state.bot})
        await app.state.dp.feed_update(app.state.bot, update)
        return {"ok": True}

    return app


app = create_app()  # `uvicorn app.main:app` (nothing connects until the server starts)
