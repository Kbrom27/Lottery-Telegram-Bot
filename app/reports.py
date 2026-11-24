"""Shared dashboard statistics, ticket queries, and Excel/CSV report builders."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
from collections import defaultdict
from decimal import Decimal

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import EAT, Settings
from .models import BankCredit, Counter, Payment, Ticket, User, utcnow


def _mask(name: str | None) -> str:
    if not name:
        return ""
    return name[:2] + "***" + name[-1:] if len(name) > 4 else name[:1] + "***"


async def fetch_ticket_rows(session: AsyncSession):
    """Fetch all issued tickets joined with holder, payment, and bank credit details."""
    return (
        await session.execute(
            select(
                Ticket.number,
                User.full_name,
                User.username,
                User.telegram_id,
                BankCredit.sender.label("bank_sender"),
                Payment.transaction_id,
                Payment.amount,
                Payment.provider,
                Ticket.payment_id,
                Ticket.issued_at,
            )
            .join(User, User.id == Ticket.user_id)
            .join(Payment, Payment.id == Ticket.payment_id)
            .outerjoin(BankCredit, BankCredit.id == Payment.credit_id)
            .order_by(Ticket.number)
        )
    ).all()


async def get_dashboard_summary(session: AsyncSession, settings: Settings) -> dict:
    """Compute real-time financial & ticket metrics for the web dashboard and reports."""
    by_status = dict(
        (await session.execute(select(Payment.status, func.count()).group_by(Payment.status))).all()
    )
    n_tickets = (await session.execute(select(func.count()).select_from(Ticket))).scalar_one()
    n_people = (await session.execute(select(func.count(func.distinct(Ticket.user_id))))).scalar_one()
    nxt = (await session.execute(select(Counter.next_number))).scalar_one_or_none() or settings.first_ticket_number

    approved_rows = (
        await session.execute(
            select(Payment.id, Payment.amount, Payment.provider, Payment.updated_at)
            .where(Payment.status == "approved")
        )
    ).all()

    ticket_counts_by_pid = dict(
        (
            await session.execute(
                select(Ticket.payment_id, func.count()).group_by(Ticket.payment_id)
            )
        ).all()
    )

    total_birr = Decimal("0")
    provider_stats: dict[str, dict] = defaultdict(lambda: {"payments": 0, "tickets": 0, "birr": Decimal("0")})
    daily_stats: dict[str, dict] = defaultdict(lambda: {"payments": 0, "tickets": 0, "birr": Decimal("0")})

    for pid, amt, provider, updated_at in approved_rows:
        t_cnt = int(ticket_counts_by_pid.get(pid, 0))
        eff_amt = Decimal(str(amt)) if amt is not None else (Decimal(t_cnt) * settings.ticket_price)
        total_birr += eff_amt

        prov_key = (provider or "direct/manual").upper()
        provider_stats[prov_key]["payments"] += 1
        provider_stats[prov_key]["tickets"] += t_cnt
        provider_stats[prov_key]["birr"] += eff_amt

        day_key = updated_at.astimezone(EAT).strftime("%Y-%m-%d") if updated_at else "Unknown"
        daily_stats[day_key]["payments"] += 1
        daily_stats[day_key]["tickets"] += t_cnt
        daily_stats[day_key]["birr"] += eff_amt

    credits_row = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(BankCredit.amount), 0)).select_from(BankCredit)
        )
    ).one()

    return {
        "total_birr_received": float(total_birr),
        "total_birr_formatted": f"{total_birr:,.2f}",
        "total_tickets_issued": int(n_tickets),
        "total_participants": int(n_people),
        "next_ticket_number": int(nxt),
        "next_ticket_formatted": settings.fmt(int(nxt)),
        "pending_review_count": int(by_status.get("review", 0)),
        "waiting_count": int(by_status.get("received", 0) + by_status.get("verifying", 0)),
        "approved_payments_count": int(by_status.get("approved", 0)),
        "rejected_payments_count": int(by_status.get("rejected", 0)),
        "status_counts": {str(k): int(v) for k, v in by_status.items()},
        "bank_credits_count": int(credits_row[0] or 0),
        "bank_credits_total_birr": float(credits_row[1] or 0),
        "by_provider": [
            {"provider": k, "payments": v["payments"], "tickets": v["tickets"], "birr": float(v["birr"])}
            for k, v in sorted(provider_stats.items())
        ],
        "daily_breakdown": [
            {"date": k, "payments": v["payments"], "tickets": v["tickets"], "birr": float(v["birr"])}
            for k, v in sorted(daily_stats.items(), reverse=True)
        ],
    }


async def get_holders_data(session: AsyncSession, settings: Settings, search: str = "") -> dict:
    """Return both per-ticket rows and per-holder grouped summaries, optionally filtered."""
    raw_rows = await fetch_ticket_rows(session)
    q = search.strip().lower()

    tickets_out = []
    by_user: dict[int, dict] = {}

    # Track unique payment amounts per user to compute total birr per holder accurately
    user_payment_amounts: dict[int, dict[int, Decimal]] = defaultdict(dict)

    for number, full_name, username, tg_id, bank_sender, tx_id, amt, provider, pid, issued_at in raw_rows:
        fmt_num = settings.fmt(number)
        eat_dt = issued_at.astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S") if issued_at else ""
        eff_amt = float(amt) if amt is not None else float(settings.ticket_price)

        item = {
            "number": int(number),
            "formatted": fmt_num,
            "full_name": full_name or "(No Telegram Name)",
            "bank_sender": bank_sender or "",
            "username": f"@{username}" if username else "",
            "telegram_id": int(tg_id),
            "transaction_id": tx_id or "",
            "amount": eff_amt,
            "provider": (provider or "").upper(),
            "payment_id": int(pid),
            "issued_at": eat_dt,
        }

        if tg_id not in by_user:
            by_user[tg_id] = {
                "full_name": full_name or "(No Telegram Name)",
                "username": f"@{username}" if username else "",
                "telegram_id": int(tg_id),
                "bank_names": [],
                "ticket_numbers": [],
                "formatted_numbers": [],
                "ticket_count": 0,
                "total_birr": 0.0,
                "last_issued_at": eat_dt,
            }
        u_entry = by_user[tg_id]
        if bank_sender and bank_sender not in u_entry["bank_names"]:
            u_entry["bank_names"].append(bank_sender)
        u_entry["ticket_numbers"].append(int(number))
        u_entry["formatted_numbers"].append(fmt_num)
        u_entry["ticket_count"] += 1
        u_entry["last_issued_at"] = eat_dt
        if pid not in user_payment_amounts[tg_id]:
            user_payment_amounts[tg_id][pid] = Decimal(str(amt)) if amt is not None else settings.ticket_price

        if not q or any(
            q in str(field).lower()
            for field in (number, fmt_num, full_name or "", bank_sender or "", username or "", tg_id, tx_id or "")
        ):
            tickets_out.append(item)

    holders_out = []
    for tg_id, u_entry in by_user.items():
        u_entry["total_birr"] = float(sum(user_payment_amounts[tg_id].values(), Decimal("0")))
        u_entry["bank_names_str"] = ", ".join(u_entry["bank_names"])
        u_entry["numbers_str"] = ", ".join(u_entry["formatted_numbers"])
        if not q or any(
            q in str(field).lower()
            for field in (
                u_entry["full_name"],
                u_entry["username"],
                tg_id,
                u_entry["bank_names_str"],
                u_entry["numbers_str"],
            )
        ):
            holders_out.append(u_entry)

    return {"tickets": tickets_out, "holders": holders_out, "total_tickets": len(raw_rows)}


def _style_header(ws, row_idx: int = 1, fill_hex: str = "1B365D") -> None:
    font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    fill = PatternFill(start_color=fill_hex, end_color=fill_hex, fill_type="solid")
    for cell in ws[row_idx]:
        cell.font = font
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _autofit(ws) -> None:
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(max_len + 4, 12), 60)


async def build_holders_excel(session: AsyncSession, settings: Settings) -> tuple[bytes, int]:
    """Build multi-sheet Excel (.xlsx) workbook with every ticket and summary per holder."""
    data = await get_holders_data(session, settings)
    wb = openpyxl.Workbook()

    # Sheet 1: Every Lottery Ticket
    ws1 = wb.active
    ws1.title = "Lottery Holders"
    ws1.append([
        "Ticket #",
        "Formatted Ticket",
        "Full Name",
        "Bank Payer Name",
        "Telegram Username",
        "Telegram ID",
        "Transaction Ref",
        "Payment Amount (ETB)",
        "Provider",
        "Date Issued (EAT)",
    ])
    _style_header(ws1)

    for t in data["tickets"]:
        ws1.append([
            t["number"],
            t["formatted"],
            t["full_name"],
            t["bank_sender"],
            t["username"],
            str(t["telegram_id"]),
            t["transaction_id"],
            t["amount"],
            t["provider"],
            t["issued_at"],
        ])
    _autofit(ws1)

    # Sheet 2: Grouped by Participant
    ws2 = wb.create_sheet("Summary by Holder")
    ws2.append([
        "Holder Full Name",
        "Bank Payer Name(s)",
        "Telegram Username",
        "Telegram ID",
        "Total Tickets",
        "Total Paid (ETB)",
        "Lottery Numbers Assigned",
        "Last Issued (EAT)",
    ])
    _style_header(ws2, fill_hex="0F5132")

    for h in data["holders"]:
        ws2.append([
            h["full_name"],
            h["bank_names_str"],
            h["username"],
            str(h["telegram_id"]),
            h["ticket_count"],
            h["total_birr"],
            h["numbers_str"],
            h["last_issued_at"],
        ])
    _autofit(ws2)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), data["total_tickets"]


async def build_summary_report_excel(session: AsyncSession, settings: Settings) -> bytes:
    """Build Campaign Financial & Operational Report (.xlsx)."""
    summary = await get_dashboard_summary(session, settings)
    holders_data = await get_holders_data(session, settings)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Financial Summary"

    ws.append(["Campaign Financial & Lottery Report", ""])
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color="1B365D")
    ws.append(["Campaign Name", settings.campaign_name])
    ws.append(["Generated At (EAT)", utcnow().astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S")])
    ws.append(["Ticket Price (ETB)", float(settings.ticket_price)])
    ws.append([])

    ws.append(["Metric", "Value"])
    _style_header(ws, row_idx=6)
    ws.append(["Total Birr Received (ETB)", summary["total_birr_received"]])
    ws.append(["Total Lottery Numbers Issued", summary["total_tickets_issued"]])
    ws.append(["Unique Ticket Holders", summary["total_participants"]])
    ws.append(["Next Ticket Number", summary["next_ticket_formatted"]])
    ws.append(["Approved Payments", summary["approved_payments_count"]])
    ws.append(["Pending Manual Review", summary["pending_review_count"]])
    ws.append(["Waiting / Retrying", summary["waiting_count"]])
    ws.append(["Rejected Payments", summary["rejected_payments_count"]])
    ws.append(["Recorded Bank Credits", summary["bank_credits_count"]])

    ws.append([])
    start_prov = ws.max_row + 1
    ws.append(["Provider Breakdown", "Approved Payments", "Tickets Issued", "Total Birr (ETB)"])
    _style_header(ws, row_idx=start_prov, fill_hex="0F5132")
    for p in summary["by_provider"]:
        ws.append([p["provider"], p["payments"], p["tickets"], p["birr"]])
    _autofit(ws)

    # Sheet 2: Daily Breakdown
    ws2 = wb.create_sheet("Daily Sales")
    ws2.append(["Date (EAT)", "Approved Payments", "Tickets Issued", "Birr Received (ETB)"])
    _style_header(ws2)
    for d in summary["daily_breakdown"]:
        ws2.append([d["date"], d["payments"], d["tickets"], d["birr"]])
    _autofit(ws2)

    # Sheet 3: Holders
    ws3 = wb.create_sheet("Holders Summary")
    ws3.append(["Full Name", "Username", "Telegram ID", "Tickets Count", "Total Paid (ETB)", "Numbers Assigned"])
    _style_header(ws3)
    for h in holders_data["holders"]:
        ws3.append([h["full_name"], h["username"], str(h["telegram_id"]), h["ticket_count"], h["total_birr"], h["numbers_str"]])
    _autofit(ws3)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def build_tickets_csv(session: AsyncSession, settings: Settings, public: bool = False) -> tuple[bytes, int]:
    """Build CSV export (full list for draw.py / Excel, or masked public list)."""
    rows = await fetch_ticket_rows(session)
    buf = io.StringIO()
    w = csv.writer(buf)
    key = (settings.bot_token or "").encode()
    if public:
        w.writerow(["number", "holder", "name", "handle"])
        for number, full_name, username, tg, _bs, _tx, _amt, _prov, _pid, _at in rows:
            holder = hmac.new(key, str(tg).encode(), hashlib.sha256).hexdigest()[:8]
            w.writerow([number, holder, _mask(full_name), _mask(username)])
    else:
        w.writerow(["number", "full_name", "telegram_id", "username", "bank_sender", "transaction_id", "amount", "payment_id", "issued_at"])
        for number, full_name, username, tg, bank_sender, tx_id, amt, _prov, pid, issued_at in rows:
            w.writerow([number, full_name or "", tg, username or "", bank_sender or "", tx_id or "", amt or "", pid, issued_at])
    return buf.getvalue().encode("utf-8-sig"), len(rows)
