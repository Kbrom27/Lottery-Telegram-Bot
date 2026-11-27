"""Lifecycle manager for the database, Telegram bot polling, worker, and live logs."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from sqlalchemy import func, select, update

from .bot import build_dispatcher, make_review_notifier, safe_send
from .config import Settings, load_desktop_settings, resolve_data_dir, save_desktop_settings
from .db import init_db, is_sqlite_url, make_engine, make_sessionmaker
from .extract import find_tesseract_cmd, make_extractor
from .models import Counter, Ticket
from .worker import Worker

log = logging.getLogger("service_manager")


class RingLogHandler(logging.Handler):
    """Stores recent log lines in memory for display in the Web Dashboard."""

    def __init__(self, maxlen: int = 300):
        super().__init__()
        self.records: deque[dict[str, str]] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.records.append(
                {
                    "time": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S"),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "line": msg,
                }
            )
        except Exception:
            pass


def _check_qr_available() -> bool:
    try:
        import zxingcpp  # noqa: F401
        return True
    except Exception:
        return False


class BotServiceManager:
    """Coordinates persistent DB access and controllable Start/Stop of the Telegram bot + worker."""

    def __init__(self, data_dir: Path | None = None, settings: Settings | None = None):
        self.data_dir = data_dir or resolve_data_dir()
        self.settings = settings or load_desktop_settings(self.data_dir)
        self.engine = None
        self.sm = None

        self.bot: Bot | None = None
        self.dp = None
        self.tasks: list[asyncio.Task] = []
        self._op_lock = asyncio.Lock()

        self.status: str = "stopped" if self.settings.is_configured else "unconfigured"
        self.last_error: str | None = None
        self.bot_username: str | None = None
        self.bot_name: str | None = None
        self.started_at: datetime | None = None

        self.log_handler = RingLogHandler(maxlen=300)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger = logging.getLogger()
        if not any(isinstance(h, RingLogHandler) for h in root_logger.handlers):
            root_logger.addHandler(self.log_handler)
        if root_logger.level > logging.INFO or root_logger.level == logging.NOTSET:
            root_logger.setLevel(logging.INFO)

    async def init_database(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
        self.engine = make_engine(self.settings.database_url)
        self.sm = make_sessionmaker(self.engine)
        await init_db(self.engine, self.settings)

    async def shutdown(self) -> None:
        await self.stop()
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.sm = None

    async def start(self) -> dict[str, Any]:
        async with self._op_lock:
            return await self._start_unlocked()

    async def _start_unlocked(self) -> dict[str, Any]:
        if self.status == "running" and self.tasks:
            return self.status_dict()

        if not self.settings.is_configured:
            self.status = "unconfigured"
            self.last_error = "Please configure Bot Token and Admin Telegram ID first."
            return self.status_dict()

        self.status = "starting"
        self.last_error = None
        try:
            if self.engine is None or self.sm is None:
                await self.init_database()

            bot = Bot(self.settings.bot_token)
            try:
                me = await bot.get_me()
            except Exception as e:
                await bot.session.close()
                raise RuntimeError(f"Telegram connection failed (check Bot Token / internet): {e}") from e

            self.bot = bot
            self.bot_username = me.username
            self.bot_name = me.first_name
            self.dp = build_dispatcher(self.sm, self.settings)

            async def fetch_file(file_id: str) -> bytes:
                f = await bot.get_file(file_id)
                buf = await bot.download_file(f.file_path)
                return buf.read()

            async def notify(tg_id: int, text: str) -> None:
                await safe_send(bot, tg_id, text)

            worker = Worker(
                self.sm,
                self.settings,
                make_extractor(self.settings),
                fetch_file,
                notify,
                make_review_notifier(bot, self.sm, self.settings),
            )

            await bot.delete_webhook()
            worker_task = asyncio.create_task(worker.run_forever(), name="lottery-worker")
            polling_task = asyncio.create_task(
                self.dp.start_polling(bot, handle_signals=False), name="lottery-polling"
            )
            self.tasks = [worker_task, polling_task]
            for t in self.tasks:
                t.add_done_callback(self._on_task_done)

            self.status = "running"
            self.started_at = datetime.now(timezone.utc)
            log.info("Bot service started for @%s (%s)", self.bot_username, self.bot_name)
        except Exception as e:
            log.exception("Failed to start bot service")
            self.status = "error"
            self.last_error = str(e)
            await self._cleanup_bot_tasks()

        return self.status_dict()

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self.status == "running":
            log.error("Background task %s crashed: %s", task.get_name(), exc)
            self.status = "error"
            self.last_error = f"{task.get_name()} stopped: {exc}"
            self.started_at = None
            for sibling in list(self.tasks):
                if sibling is not task and not sibling.done():
                    sibling.cancel()

    async def stop(self) -> dict[str, Any]:
        async with self._op_lock:
            return await self._stop_unlocked()

    async def _stop_unlocked(self) -> dict[str, Any]:
        await self._cleanup_bot_tasks()
        self.status = "stopped" if self.settings.is_configured else "unconfigured"
        self.started_at = None
        log.info("Bot service stopped")
        return self.status_dict()

    async def _cleanup_bot_tasks(self) -> None:
        tasks = list(self.tasks)
        self.tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.bot is not None:
            try:
                await self.bot.session.close()
            except Exception:
                pass
            self.bot = None
        self.dp = None

    async def restart(self) -> dict[str, Any]:
        async with self._op_lock:
            await self._stop_unlocked()
            return await self._start_unlocked()

    async def apply_settings(self, payload: dict[str, Any], start_after: bool = False) -> dict[str, Any]:
        async with self._op_lock:
            was_running = self.status == "running"
            old_db = self.settings.database_url
            if was_running:
                await self._stop_unlocked()

            self.settings = save_desktop_settings(payload, self.data_dir)
            if self.engine is None or self.settings.database_url != old_db:
                await self.init_database()
            else:
                await init_db(self.engine, self.settings)

            if self.sm is not None:
                async with self.sm() as s, s.begin():
                    issued = await s.scalar(select(func.count(Ticket.number))) or 0
                    if issued == 0:
                        await s.execute(
                            update(Counter)
                            .where(Counter.id == 1)
                            .values(next_number=self.settings.first_ticket_number)
                        )

            self.last_error = None
            if start_after or was_running:
                return await self._start_unlocked()
            self.status = "stopped" if self.settings.is_configured else "unconfigured"
            return self.status_dict()

    def status_dict(self) -> dict[str, Any]:
        uptime = None
        if self.started_at and self.status == "running":
            uptime = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
        tess = find_tesseract_cmd(self.settings.tesseract_cmd)
        return {
            "status": self.status,
            "configured": self.settings.is_configured,
            "campaign_name": self.settings.campaign_name,
            "bot_username": self.bot_username,
            "bot_name": self.bot_name,
            "error": self.last_error,
            "uptime_seconds": uptime,
            "data_dir": str(self.data_dir),
            "db_type": "SQLite (Embedded)" if is_sqlite_url(self.settings.database_url) else "PostgreSQL",
            "qr_available": _check_qr_available(),
            "tesseract_available": bool(tess),
            "tesseract_path": tess or "",
            "sms_secret": self.settings.sms_secret,
            "web_host": self.settings.web_host,
            "web_port": self.settings.web_port,
            "web_password_set": bool(self.settings.web_password),
            "max_total_tickets": self.settings.max_total_tickets,
            "allow_split_payments": self.settings.allow_split_payments,
        }
