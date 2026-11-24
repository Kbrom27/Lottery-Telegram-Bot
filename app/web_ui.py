"""Web-based Control Panel, Setup Wizard, Live Dashboard, and Report Exports."""
from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import os
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import desc, select

import draw as draw_lib

from . import messages as M
from .bot import safe_send
from .config import EAT, build_payment_instructions
from .extract import normalize_ref
from .models import BankCredit, Payment, Ticket, User
from .reports import (
    build_holders_excel,
    build_summary_report_excel,
    build_tickets_csv,
    get_dashboard_summary,
    get_holders_data,
)
from .service import CreditAlreadyClaimed, InvalidState, approve, reject
from .service_manager import BotServiceManager
from .sms import add_credit, ingest_sms
from .verify import find_credit

log = logging.getLogger("web_ui")


def _template_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / "app" / "web_dashboard.html"
        if p.exists():
            return p
    return Path(__file__).resolve().parent / "web_dashboard.html"


def mount_web_routes(app: FastAPI, manager: BotServiceManager) -> None:
    """Attach the Web Dashboard, Setup/Settings, Review Queue, Draw, and Excel Export routes."""

    def _sm():
        return manager.sm or getattr(app.state, "sm", None)

    def _bot():
        return manager.bot or getattr(app.state, "bot", None)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(_template_path().read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def api_status():
        return manager.status_dict()

    @app.get("/api/settings")
    async def api_get_settings():
        data = manager.settings.to_ui_dict()
        data["is_windows"] = sys.platform == "win32"
        return data

    @app.post("/api/settings")
    async def api_save_settings(request: Request):
        body = await request.json()
        start_after = bool(body.pop("start_after_save", False))
        # Preserve Docker container database/storage overrides if explicitly set in env
        if os.environ.get("DATABASE_URL"):
            body["database_url"] = os.environ["DATABASE_URL"]
        if os.environ.get("STORAGE_DIR"):
            body["storage_dir"] = os.environ["STORAGE_DIR"]
        if body.pop("auto_build_instructions", False):
            body["payment_instructions"] = build_payment_instructions(
                body.get("ticket_price", "100"),
                body.get("cbe_account", ""),
                body.get("cbe_account_name", ""),
                body.get("telebirr_number", ""),
                body.get("telebirr_name", ""),
                body.get("extra_instructions", ""),
            )
        try:
            st = await manager.apply_settings(body, start_after=start_after)
            app.state.sm = manager.sm
            return {"ok": True, "status": st, "settings": manager.settings.to_ui_dict()}
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:
            log.exception("Failed saving settings")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/verify-token")
    async def api_verify_token(request: Request):
        body = await request.json()
        token = str(body.get("bot_token", "")).strip()
        if not token:
            return JSONResponse({"ok": False, "error": "Please enter a Bot Token."}, status_code=400)
        bot = Bot(token)
        try:
            me = await bot.get_me()
            return {"ok": True, "id": me.id, "username": me.username, "first_name": me.first_name}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Token check failed: {e}"}, status_code=400)
        finally:
            await bot.session.close()

    @app.post("/api/service/start")
    async def api_start():
        st = await manager.start()
        app.state.sm = manager.sm
        return st

    @app.post("/api/service/stop")
    async def api_stop():
        return await manager.stop()

    @app.post("/api/service/restart")
    async def api_restart():
        st = await manager.restart()
        app.state.sm = manager.sm
        return st

    @app.get("/api/dashboard")
    async def api_dashboard():
        async with _sm()() as s:
            summary = await get_dashboard_summary(s, manager.settings)
        summary["service"] = manager.status_dict()
        return summary

    @app.get("/api/holders")
    async def api_holders(search: str = ""):
        async with _sm()() as s:
            return await get_holders_data(s, manager.settings, search=search)

    @app.get("/api/reviews")
    async def api_reviews():
        async with _sm()() as s:
            rows = (
                await s.execute(
                    select(Payment, User)
                    .join(User, User.id == Payment.user_id)
                    .where(Payment.status == "review")
                    .order_by(Payment.id)
                )
            ).all()
            out = []
            for p, u in rows:
                credit = await find_credit(s, p.transaction_id) if p.transaction_id else None
                if credit:
                    n = int(credit.amount // manager.settings.ticket_price)
                elif p.amount:
                    n = int(p.amount // manager.settings.ticket_price)
                else:
                    n = 1
                n = max(1, min(n, manager.settings.max_tickets_per_payment))
                ex = p.extracted or {}
                out.append(
                    {
                        "id": p.id,
                        "user_name": u.full_name or "",
                        "username": f"@{u.username}" if u.username else "",
                        "telegram_id": u.telegram_id,
                        "reason": p.reject_reason or "",
                        "transaction_id": p.transaction_id or "",
                        "screenshot_amount": ex.get("amount"),
                        "screenshot_sender": ex.get("sender"),
                        "credit_amount": float(credit.amount) if credit else None,
                        "credit_sender": credit.sender if credit else None,
                        "credit_claimed": bool(credit and credit.claimed_by_payment_id not in (None, p.id)),
                        "suggested_tickets": n,
                        "created_at": p.created_at.astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "",
                    }
                )
            return {"reviews": out}

    @app.get("/api/payments/{pid}/image")
    async def api_payment_image(pid: int):
        async with _sm()() as s:
            p = await s.get(Payment, pid)
        if not p:
            raise HTTPException(status_code=404)
        if p.image_path and os.path.exists(p.image_path):
            with open(p.image_path, "rb") as f:
                return Response(content=f.read(), media_type="image/jpeg")
        bot = _bot()
        if bot is not None and p.tg_file_id:
            try:
                f_info = await bot.get_file(p.tg_file_id)
                buf = await bot.download_file(f_info.file_path)
                data = buf.read()
                os.makedirs(manager.settings.storage_dir, exist_ok=True)
                path = os.path.join(manager.settings.storage_dir, f"{p.id}.img")
                with open(path, "wb") as out_f:
                    out_f.write(data)
                async with _sm()() as s, s.begin():
                    (await s.get(Payment, p.id)).image_path = path
                return Response(content=data, media_type="image/jpeg")
            except Exception:
                pass
        raise HTTPException(status_code=404, detail="Image not found")

    @app.post("/api/reviews/{pid}/approve")
    async def api_approve_review(pid: int, request: Request):
        body = await request.json()
        n = max(1, int(body.get("tickets", 1)))
        try:
            async with _sm()() as s, s.begin():
                p = await s.get(Payment, pid)
                if not p:
                    return JSONResponse({"ok": False, "error": "Payment not found"}, status_code=404)
                credit = await find_credit(s, p.transaction_id) if p.transaction_id else None
                cid = credit.id if credit and credit.claimed_by_payment_id in (None, pid) else None
                numbers = await approve(s, pid, n, "web_admin", cid)
                if cid is None and p.amount is None:
                    p.amount = Decimal(n) * manager.settings.ticket_price
                tg_id = (await s.get(User, p.user_id)).telegram_id
        except (InvalidState, CreditAlreadyClaimed) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

        formatted = [manager.settings.fmt(x) for x in numbers]
        bot = _bot()
        if bot is not None:
            await safe_send(bot, tg_id, M.APPROVED.format(numbers=", ".join(formatted)))
        return {"ok": True, "numbers": formatted}

    @app.post("/api/reviews/{pid}/reject")
    async def api_reject_review(pid: int, request: Request):
        body = await request.json()
        reason = str(body.get("reason") or M.ADMIN_REJECTED).strip()
        try:
            async with _sm()() as s, s.begin():
                p = await reject(s, pid, reason, "web_admin")
                tg_id = (await s.get(User, p.user_id)).telegram_id
        except InvalidState as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        bot = _bot()
        if bot is not None:
            await safe_send(bot, tg_id, reason)
        return {"ok": True}

    @app.get("/api/credits")
    async def api_credits():
        async with _sm()() as s:
            rows = (await s.execute(select(BankCredit).order_by(desc(BankCredit.id)).limit(200))).scalars().all()
        return {
            "credits": [
                {
                    "id": c.id,
                    "reference": c.reference,
                    "amount": float(c.amount),
                    "sender": c.sender or "",
                    "claimed_by_payment_id": c.claimed_by_payment_id,
                    "received_at": c.received_at.astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S") if c.received_at else "",
                }
                for c in rows
            ]
        }

    @app.post("/api/credits/import-csv")
    async def api_import_csv(request: Request):
        body = await request.json()
        csv_text = str(body.get("csv_text") or "")
        if not csv_text.strip():
            return JSONResponse({"ok": False, "error": "Empty CSV content"}, status_code=400)
        created = skipped = 0
        try:
            reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
            async with _sm()() as s, s.begin():
                for row in reader:
                    ref = normalize_ref(row["reference"])
                    amt = Decimal(str(row["amount"]).replace(",", ""))
                    credit = await add_credit(s, ref, amt, row.get("sender") or None, None, "web csv import")
                    created += bool(credit)
                    skipped += not credit
            return {"ok": True, "created": created, "skipped": skipped}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CSV parse error: {e}"}, status_code=400)

    @app.post("/api/credits/sms")
    async def api_manual_sms(request: Request):
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "Please enter SMS text"}, status_code=400)
        async with _sm()() as s, s.begin():
            status, credit = await ingest_sms(s, text)
        return {
            "ok": True,
            "status": status,
            "reference": credit.reference if credit else None,
            "amount": float(credit.amount) if credit else None,
        }

    @app.get("/api/export/excel")
    async def api_export_excel():
        async with _sm()() as s:
            data, _ = await build_holders_excel(s, manager.settings)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="lottery_tickets.xlsx"'},
        )

    @app.get("/api/export/report")
    async def api_export_report():
        async with _sm()() as s:
            data = await build_summary_report_excel(s, manager.settings)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="lottery_summary_report.xlsx"'},
        )

    @app.get("/api/export/csv")
    async def api_export_csv(public: int = 0):
        is_pub = bool(public)
        async with _sm()() as s:
            data, _ = await build_tickets_csv(s, manager.settings, public=is_pub)
        name = "tickets_public.csv" if is_pub else "tickets_full.csv"
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/draw/status")
    async def api_draw_status():
        seed_path = manager.data_dir / "draw_seed.txt"
        if not seed_path.exists():
            return {"committed": False, "commitment": None}
        seed = seed_path.read_text(encoding="utf-8").strip()
        return {"committed": True, "commitment": draw_lib.commitment(seed)}

    @app.post("/api/draw/commit")
    async def api_draw_commit():
        seed_path = manager.data_dir / "draw_seed.txt"
        manager.data_dir.mkdir(parents=True, exist_ok=True)
        if not seed_path.exists():
            seed = secrets.token_hex(32)
            seed_path.write_text(seed, encoding="utf-8")
        else:
            seed = seed_path.read_text(encoding="utf-8").strip()
        return {"ok": True, "commitment": draw_lib.commitment(seed)}

    @app.post("/api/draw/run")
    async def api_draw_run(request: Request):
        seed_path = manager.data_dir / "draw_seed.txt"
        if not seed_path.exists():
            return JSONResponse(
                {"ok": False, "error": "Please click 'Generate Commitment' before running the draw."}, status_code=400
            )
        body = await request.json()
        entropy = str(body.get("entropy") or "").strip()
        if not entropy:
            return JSONResponse({"ok": False, "error": "Please enter a public entropy value."}, status_code=400)
        winners_n = max(1, int(body.get("winners", 1)))
        one_per_person = bool(body.get("one_per_person", True))

        async with _sm()() as s:
            rows = (
                await s.execute(
                    select(Ticket.number, User.telegram_id, User.full_name, User.username)
                    .join(User, User.id == Ticket.user_id)
                    .order_by(Ticket.number)
                )
            ).all()
        if not rows:
            return JSONResponse({"ok": False, "error": "No tickets have been issued yet."}, status_code=400)

        by_num = {
            int(n): {"full_name": fn or "", "username": f"@{un}" if un else "", "telegram_id": int(tg)}
            for n, tg, fn, un in rows
        }
        pool = [(int(n), str(tg)) for n, tg, _fn, _un in rows]
        seed = seed_path.read_text(encoding="utf-8").strip()
        picked = draw_lib.pick_winners(seed, entropy, pool, winners_n, one_per_person)

        out = []
        for rank, (num, _holder) in enumerate(picked, 1):
            info = by_num[num]
            out.append(
                {
                    "rank": rank,
                    "number": num,
                    "formatted": manager.settings.fmt(num),
                    "full_name": info["full_name"],
                    "username": info["username"],
                    "telegram_id": info["telegram_id"],
                }
            )
        return {
            "ok": True,
            "commitment": draw_lib.commitment(seed),
            "seed": seed,
            "entropy": entropy,
            "total_tickets": len(pool),
            "winners": out,
        }

    @app.get("/api/logs")
    async def api_logs():
        return {"logs": list(manager.log_handler.records)}

    @app.post("/api/windows/shortcut")
    async def api_windows_shortcut():
        if sys.platform != "win32":
            return JSONResponse({"ok": False, "error": "Desktop shortcut creation is only supported on Windows."}, status_code=400)
        try:
            target = sys.executable.replace("'", "''")
            workdir = str(Path(sys.executable).resolve().parent).replace("'", "''")
            ps = (
                "$desktop = [Environment]::GetFolderPath('Desktop');"
                "if (-not $desktop) { $desktop = Join-Path $env:USERPROFILE 'Desktop' };"
                "$lnk = Join-Path $desktop 'LotteryBot.lnk';"
                "$s = (New-Object -COM WScript.Shell).CreateShortcut($lnk);"
                f"$s.TargetPath = '{target}';"
                f"$s.WorkingDirectory = '{workdir}';"
                "$s.Save();"
                "Write-Output $lnk"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                check=True,
                capture_output=True,
                text=True,
            )
            stdout_lines = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
            lnk_path = stdout_lines[-1] if stdout_lines else "Desktop\\LotteryBot.lnk"
            return {"ok": True, "path": lnk_path}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def create_web_app(manager: BotServiceManager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await manager.init_database()
        if manager.settings.is_configured and manager.settings.auto_start_bot:
            await manager.start()
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(title="LotteryBot Control Center", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.manager = manager
    mount_web_routes(app, manager)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "app": "lotterybot", "status": manager.status}

    @app.post("/sms")
    async def sms_webhook(request: Request, x_secret: str = Header(default=""), key: str = ""):
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
        async with manager.sm() as s, s.begin():
            status, _ = await ingest_sms(s, text)
        log.info("sms %s", status)
        return {"status": status}

    return app
