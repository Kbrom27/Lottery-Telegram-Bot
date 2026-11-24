"""Settings management: supports environment variables (.env / Docker) and JSON config (Desktop/Web UI)."""
from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

EAT = timezone(timedelta(hours=3))  # Ethiopia: UTC+3, no daylight saving


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(value)
    return d if d.tzinfo else d.replace(tzinfo=EAT)


def resolve_data_dir() -> Path:
    """Choose a writable persistent data directory for SQLite DB, config, and screenshots."""
    env_dir = os.environ.get("LOTTERY_DATA_DIR")
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent / "lottery_data"
        try:
            exe_dir.mkdir(parents=True, exist_ok=True)
            probe = exe_dir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return exe_dir
        except OSError:
            local_app = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LotteryBot"
            local_app.mkdir(parents=True, exist_ok=True)
            return local_app

    return Path.cwd() / "data"


def build_payment_instructions(
    ticket_price: Decimal | str,
    cbe_account: str = "",
    cbe_account_name: str = "",
    telebirr_number: str = "",
    telebirr_name: str = "",
    extra_instructions: str = "",
) -> str:
    lines = [f"Pay {ticket_price} ETB per ticket to:"]
    if telebirr_number.strip():
        holder = f" ({telebirr_name.strip()})" if telebirr_name.strip() else ""
        lines.append(f"Telebirr: {telebirr_number.strip()}{holder}")
    if cbe_account.strip():
        holder = f" ({cbe_account_name.strip()})" if cbe_account_name.strip() else ""
        lines.append(f"CBE: {cbe_account.strip()}{holder}")
    if extra_instructions.strip():
        lines.append(extra_instructions.strip())
    if len(lines) == 1:
        return "Send your payment to the organizer's receiving account."
    return "\n".join(lines)


@dataclass(frozen=True)
class Settings:
    campaign_name: str = "Telegram Lottery"
    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://lottery:lottery@localhost:5432/lottery"
    admin_ids: frozenset[int] = frozenset()

    # Campaign rules
    ticket_price: Decimal = Decimal("100")
    max_tickets_per_payment: int = 50
    first_ticket_number: int = 1
    ticket_width: int = 5  # #00001 ... #10000
    campaign_unbounded: bool = True
    campaign_start: datetime = datetime(2000, 1, 1, tzinfo=timezone.utc)
    campaign_end: datetime = datetime(2100, 1, 1, tzinfo=timezone.utc)

    # Payment accounts & instructions
    cbe_account: str = ""
    cbe_account_name: str = ""
    telebirr_number: str = ""
    telebirr_name: str = ""
    payment_instructions: str = "Send your payment to: <ACCOUNT DETAILS HERE>"

    # Verification
    sms_secret: str = ""  # shared secret for the /sms webhook
    credit_wait_minutes: int = 2
    retry_interval_seconds: int = 15
    max_open_payments_per_user: int = 3

    # Extraction
    storage_dir: str = "./data/screenshots"
    extractor: str = "tesseract"  # "tesseract" | "claude"
    ocr_langs: str = "eng"
    tesseract_cmd: str = ""
    claude_model: str = "claude-sonnet-5-5"
    anthropic_api_key: str = ""

    # Telegram transport. Empty webhook_base_url => long polling.
    webhook_base_url: str = ""
    webhook_secret: str = ""

    # Desktop / Web UI
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    auto_start_bot: bool = False

    def fmt(self, number: int) -> str:
        return f"#{number:0{self.ticket_width}d}"

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token.strip() and self.admin_ids and self.ticket_price > 0)

    def to_ui_dict(self) -> dict[str, Any]:
        return {
            "campaign_name": self.campaign_name,
            "bot_token": self.bot_token,
            "admin_ids": ", ".join(str(x) for x in sorted(self.admin_ids)),
            "ticket_price": f"{self.ticket_price.normalize():f}",
            "max_tickets_per_payment": self.max_tickets_per_payment,
            "first_ticket_number": self.first_ticket_number,
            "ticket_width": self.ticket_width,
            "campaign_unbounded": self.campaign_unbounded,
            "campaign_start": self.campaign_start.astimezone(EAT).strftime("%Y-%m-%dT%H:%M"),
            "campaign_end": self.campaign_end.astimezone(EAT).strftime("%Y-%m-%dT%H:%M"),
            "cbe_account": self.cbe_account,
            "cbe_account_name": self.cbe_account_name,
            "telebirr_number": self.telebirr_number,
            "telebirr_name": self.telebirr_name,
            "payment_instructions": self.payment_instructions,
            "sms_secret": self.sms_secret,
            "credit_wait_minutes": self.credit_wait_minutes,
            "retry_interval_seconds": self.retry_interval_seconds,
            "max_open_payments_per_user": self.max_open_payments_per_user,
            "extractor": self.extractor,
            "ocr_langs": self.ocr_langs,
            "tesseract_cmd": self.tesseract_cmd,
            "claude_model": self.claude_model,
            "anthropic_api_key": self.anthropic_api_key,
            "database_url": self.database_url,
            "storage_dir": self.storage_dir,
            "web_host": self.web_host,
            "web_port": self.web_port,
            "auto_start_bot": self.auto_start_bot,
            "is_configured": self.is_configured,
        }

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        d = cls()

        def get(key: str, default: str) -> str:
            return env.get(key, default)

        start_raw = get("CAMPAIGN_START", "")
        end_raw = get("CAMPAIGN_END", "")
        unbounded = not (start_raw or end_raw)

        return cls(
            campaign_name=get("CAMPAIGN_NAME", d.campaign_name),
            bot_token=get("BOT_TOKEN", ""),
            database_url=get("DATABASE_URL", d.database_url),
            admin_ids=frozenset(
                int(x) for x in get("ADMIN_IDS", "").replace(" ", "").split(",") if x
            ),
            ticket_price=Decimal(get("TICKET_PRICE", str(d.ticket_price))),
            max_tickets_per_payment=int(get("MAX_TICKETS_PER_PAYMENT", str(d.max_tickets_per_payment))),
            first_ticket_number=int(get("FIRST_TICKET_NUMBER", str(d.first_ticket_number))),
            ticket_width=int(get("TICKET_WIDTH", str(d.ticket_width))),
            campaign_unbounded=unbounded,
            campaign_start=_dt(start_raw) if start_raw else d.campaign_start,
            campaign_end=_dt(end_raw) if end_raw else d.campaign_end,
            cbe_account=get("CBE_ACCOUNT", d.cbe_account),
            cbe_account_name=get("CBE_ACCOUNT_NAME", d.cbe_account_name),
            telebirr_number=get("TELEBIRR_NUMBER", d.telebirr_number),
            telebirr_name=get("TELEBIRR_NAME", d.telebirr_name),
            payment_instructions=get("PAYMENT_INSTRUCTIONS", d.payment_instructions).replace("\\n", "\n"),
            sms_secret=get("SMS_SECRET", ""),
            credit_wait_minutes=int(get("CREDIT_WAIT_MINUTES", str(d.credit_wait_minutes))),
            retry_interval_seconds=int(get("RETRY_INTERVAL_SECONDS", str(d.retry_interval_seconds))),
            max_open_payments_per_user=int(get("MAX_OPEN_PAYMENTS_PER_USER", str(d.max_open_payments_per_user))),
            storage_dir=get("STORAGE_DIR", d.storage_dir),
            extractor=get("EXTRACTOR", d.extractor),
            ocr_langs=get("OCR_LANGS", d.ocr_langs),
            tesseract_cmd=get("TESSERACT_CMD", d.tesseract_cmd),
            claude_model=get("CLAUDE_MODEL", d.claude_model),
            anthropic_api_key=get("ANTHROPIC_API_KEY", d.anthropic_api_key),
            webhook_base_url=get("WEBHOOK_BASE_URL", "").rstrip("/"),
            webhook_secret=get("WEBHOOK_SECRET", ""),
            web_host=get("WEB_HOST", d.web_host),
            web_port=int(get("WEB_PORT", str(d.web_port))),
            auto_start_bot=get("AUTO_START_BOT", "").lower() in ("1", "true", "yes"),
        )


def parse_admin_ids(raw: str | list[int] | set[int] | frozenset[int]) -> frozenset[int]:
    if isinstance(raw, (list, set, frozenset)):
        return frozenset(int(x) for x in raw)
    parts = [x.strip() for x in str(raw).replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    out = []
    for p in parts:
        out.append(int(p))
    return frozenset(out)


def load_desktop_settings(data_dir: Path | None = None) -> Settings:
    """Load settings for the Windows/Web desktop controller from `<data_dir>/config.json`."""
    d_dir = data_dir or resolve_data_dir()
    cfg_path = d_dir / "config.json"
    default_db = f"sqlite+aiosqlite:///{(d_dir / 'lottery.db').as_posix()}"
    default_screenshots = str(d_dir / "screenshots")

    if not cfg_path.exists():
        return Settings(
            database_url=default_db,
            storage_dir=default_screenshots,
            sms_secret=secrets.token_urlsafe(24),
        )

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    unbounded = bool(raw.get("campaign_unbounded", True))
    if unbounded:
        c_start = Settings.campaign_start
        c_end = Settings.campaign_end
    else:
        c_start = _dt(raw["campaign_start"]) if raw.get("campaign_start") else Settings.campaign_start
        c_end = _dt(raw["campaign_end"]) if raw.get("campaign_end") else Settings.campaign_end

    price = Decimal(str(raw.get("ticket_price", "100")))
    cbe_acc = str(raw.get("cbe_account", "")).strip()
    cbe_name = str(raw.get("cbe_account_name", "")).strip()
    tb_num = str(raw.get("telebirr_number", "")).strip()
    tb_name = str(raw.get("telebirr_name", "")).strip()
    instructions = str(raw.get("payment_instructions", "")).strip()
    if not instructions:
        instructions = build_payment_instructions(price, cbe_acc, cbe_name, tb_num, tb_name)

    return Settings(
        campaign_name=str(raw.get("campaign_name") or "Telegram Lottery"),
        bot_token=str(raw.get("bot_token", "")).strip(),
        database_url=str(raw.get("database_url") or default_db),
        admin_ids=parse_admin_ids(raw.get("admin_ids", "")),
        ticket_price=price,
        max_tickets_per_payment=int(raw.get("max_tickets_per_payment", 50)),
        first_ticket_number=int(raw.get("first_ticket_number", 1)),
        ticket_width=int(raw.get("ticket_width", 5)),
        campaign_unbounded=unbounded,
        campaign_start=c_start,
        campaign_end=c_end,
        cbe_account=cbe_acc,
        cbe_account_name=cbe_name,
        telebirr_number=tb_num,
        telebirr_name=tb_name,
        payment_instructions=instructions,
        sms_secret=str(raw.get("sms_secret") or secrets.token_urlsafe(24)),
        credit_wait_minutes=int(raw.get("credit_wait_minutes", 2)),
        retry_interval_seconds=int(raw.get("retry_interval_seconds", 15)),
        max_open_payments_per_user=int(raw.get("max_open_payments_per_user", 3)),
        storage_dir=str(raw.get("storage_dir") or default_screenshots),
        extractor=str(raw.get("extractor") or "tesseract"),
        ocr_langs=str(raw.get("ocr_langs") or "eng"),
        tesseract_cmd=str(raw.get("tesseract_cmd") or ""),
        claude_model=str(raw.get("claude_model") or "claude-sonnet-5-5"),
        anthropic_api_key=str(raw.get("anthropic_api_key") or ""),
        web_host=str(raw.get("web_host") or "127.0.0.1"),
        web_port=int(raw.get("web_port") or 8000),
        auto_start_bot=bool(raw.get("auto_start_bot", False)),
    )


def save_desktop_settings(payload: dict[str, Any], data_dir: Path | None = None) -> Settings:
    """Validate, persist to `<data_dir>/config.json`, and return the resulting Settings."""
    d_dir = data_dir or resolve_data_dir()
    d_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = d_dir / "config.json"

    existing: dict[str, Any] = {}
    if cfg_path.exists():
        existing = json.loads(cfg_path.read_text(encoding="utf-8"))

    merged = {**existing, **payload}
    # Normalize and validate before saving
    price = Decimal(str(merged.get("ticket_price", "100")))
    if price <= 0:
        raise ValueError("Ticket price must be greater than 0")
    admin_ids = parse_admin_ids(merged.get("admin_ids", ""))
    if not merged.get("bot_token", "").strip():
        raise ValueError("Telegram Bot Token is required")
    if not admin_ids:
        raise ValueError("At least one numeric Admin Telegram ID is required")

    if not merged.get("sms_secret"):
        merged["sms_secret"] = secrets.token_urlsafe(24)

    cbe_acc = str(merged.get("cbe_account", "")).strip()
    cbe_name = str(merged.get("cbe_account_name", "")).strip()
    tb_num = str(merged.get("telebirr_number", "")).strip()
    tb_name = str(merged.get("telebirr_name", "")).strip()
    if not str(merged.get("payment_instructions", "")).strip():
        merged["payment_instructions"] = build_payment_instructions(price, cbe_acc, cbe_name, tb_num, tb_name)

    tmp = cfg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cfg_path)

    # Set Anthropic API key env if using Claude
    if merged.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = str(merged["anthropic_api_key"])

    return load_desktop_settings(d_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
