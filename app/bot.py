"""Telegram handlers (aiogram 3): participant commands + admin review tools."""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import messages as M
from .config import EAT, Settings
from .db import dialect_insert
from .extract import normalize_ref
from .models import Counter, Payment, Ticket, User, utcnow
from .reports import build_holders_excel, build_tickets_csv
from .service import CreditAlreadyClaimed, InvalidState, approve, audit, reject
from .verify import find_credit

log = logging.getLogger("bot")


async def safe_send(bot: Bot, chat_id: int, text: str, **kw):
    """Send with flood-wait handling; a user who blocked the bot must not break the worker."""
    for _ in range(3):
        try:
            return await bot.send_message(chat_id, text, **kw)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramForbiddenError:
            log.info("chat %s blocked the bot", chat_id)
            return None
        except Exception:
            log.exception("send to %s failed", chat_id)
            return None
    return None


class IsAdmin(Filter):
    def __init__(self, ids: frozenset[int]):
        self.ids = ids

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in self.ids


async def upsert_user(s, tg_user) -> User:
    await s.execute(
        dialect_insert(s, User)
        .values(telegram_id=tg_user.id, username=tg_user.username, full_name=tg_user.full_name)
        .on_conflict_do_update(
            index_elements=["telegram_id"],
            set_={"username": tg_user.username, "full_name": tg_user.full_name},
        )
    )
    return (await s.execute(select(User).where(User.telegram_id == tg_user.id))).scalar_one()


def _mask(name: str | None) -> str:
    if not name:
        return ""
    return name[:2] + "***" + name[-1:] if len(name) > 4 else name[:1] + "***"


async def review_card(s, settings: Settings, payment_id: int):
    p = await s.get(Payment, payment_id)
    u = await s.get(User, p.user_id)
    credit = await find_credit(s, p.transaction_id) if p.transaction_id else None
    if credit:
        n = int(credit.amount // settings.ticket_price)
    elif p.amount:
        n = int(p.amount // settings.ticket_price)
    else:
        n = 1
    n = max(1, min(n, settings.max_tickets_per_payment))
    ex = p.extracted or {}
    caption = (
        f"🔎 Review #{p.id}\n"
        f"User: {u.full_name or ''} @{u.username or '-'} ({u.telegram_id})\n"
        f"Why: {p.reject_reason}\n"
        f"Ref: {p.transaction_id or '-'} | Screenshot amount: {ex.get('amount') or '-'}\n"
        f"Bank credit: {('%s ETB from %s' % (credit.amount, credit.sender or '?')) if credit else 'none found'}"
        f"{' (already claimed!)' if credit and credit.claimed_by_payment_id not in (None, p.id) else ''}"
    )[:1000]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"✅ Approve ({n})", callback_data=f"rv:a:{p.id}:{n}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"rv:r:{p.id}"),
            ]
        ]
    )
    return caption, kb, p.tg_file_id


def make_review_notifier(bot: Bot, sm: async_sessionmaker, settings: Settings):
    async def on_review(payment_id: int) -> None:
        async with sm() as s:
            caption, kb, file_id = await review_card(s, settings, payment_id)
        for admin_id in settings.admin_ids:
            try:
                try:
                    await bot.send_photo(admin_id, file_id, caption=caption, reply_markup=kb)
                except TelegramBadRequest:  # was sent as a document
                    await bot.send_document(admin_id, file_id, caption=caption, reply_markup=kb)
            except Exception:
                log.exception("could not notify admin %s", admin_id)

    return on_review


def build_dispatcher(sm: async_sessionmaker, settings: Settings) -> Dispatcher:
    admin = Router(name="admin")
    user = Router(name="user")
    admin.message.filter(IsAdmin(settings.admin_ids))
    admin.callback_query.filter(IsAdmin(settings.admin_ids))

    # ------------------------------------------------------------------ participants

    @user.message(CommandStart())
    async def start(msg: Message):
        fmt = lambda d: d.astimezone(EAT).strftime("%d %b %Y %H:%M")  # noqa: E731
        bounded = settings.campaign_start.year > 2000 or settings.campaign_end.year < 2100
        await msg.answer(
            M.START.format(
                campaign_name=settings.campaign_name,
                price=f"{settings.ticket_price.normalize():f}",  # "100", not "1E+2"
                max_tickets=settings.max_tickets_per_payment,
                campaign=M.CAMPAIGN_LINE.format(start=fmt(settings.campaign_start), end=fmt(settings.campaign_end))
                if bounded
                else "",
                instructions=settings.payment_instructions,
            )
        )

    @user.message(Command("mytickets"))
    async def mytickets(msg: Message):
        async with sm() as s:
            rows = (
                await s.execute(
                    select(Ticket.number)
                    .join(User, User.id == Ticket.user_id)
                    .where(User.telegram_id == msg.from_user.id)
                    .order_by(Ticket.number)
                )
            ).scalars().all()
        if not rows:
            return await msg.answer(M.NO_TICKETS)
        await msg.answer(M.MY_TICKETS.format(n=len(rows), numbers=", ".join(settings.fmt(n) for n in rows)))

    @user.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
    async def on_screenshot(msg: Message):
        now = utcnow()
        if now < settings.campaign_start:
            return await msg.answer(M.NOT_STARTED)
        if now > settings.campaign_end:
            return await msg.answer(M.ENDED)
        file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        async with sm() as s, s.begin():
            u = await upsert_user(s, msg.from_user)
            open_n = (
                await s.execute(
                    select(func.count())
                    .select_from(Payment)
                    .where(Payment.user_id == u.id, Payment.status.in_(("received", "verifying", "review")))
                )
            ).scalar_one()
            if open_n >= settings.max_open_payments_per_user:
                return await msg.answer(M.TOO_MANY_OPEN.format(n=open_n))
            p = Payment(user_id=u.id, tg_file_id=file_id, status="received", next_attempt_at=now)
            s.add(p)
            await s.flush()
            await audit(s, p.id, f"user:{msg.from_user.id}", "received")
        await msg.answer(M.RECEIVED)

    @user.message(F.text & ~F.text.startswith("/"))
    async def other_text(msg: Message):
        await msg.answer(M.SEND_SCREENSHOT)

    # ------------------------------------------------------------------ admins

    @admin.message(Command("pending"))
    async def pending(msg: Message, bot: Bot):
        async with sm() as s:
            ids = (
                await s.execute(select(Payment.id).where(Payment.status == "review").order_by(Payment.id).limit(10))
            ).scalars().all()
            if not ids:
                return await msg.answer("Nothing waiting for review 🎉")
            for pid in ids:
                caption, kb, file_id = await review_card(s, settings, pid)
                try:
                    await bot.send_photo(msg.chat.id, file_id, caption=caption, reply_markup=kb)
                except TelegramBadRequest:
                    await bot.send_document(msg.chat.id, file_id, caption=caption, reply_markup=kb)

    @admin.callback_query(F.data.startswith("rv:a:"))
    async def cb_approve(cb: CallbackQuery, bot: Bot):
        _, _, pid, n = cb.data.split(":")
        pid, n = int(pid), int(n)
        actor = f"admin:{cb.from_user.id}"
        try:
            async with sm() as s, s.begin():
                p = await s.get(Payment, pid)
                credit = await find_credit(s, p.transaction_id) if p.transaction_id else None
                cid = credit.id if credit and credit.claimed_by_payment_id in (None, pid) else None
                numbers = await approve(s, pid, n, actor, cid)
                if cid is None and p.amount is None:
                    p.amount = Decimal(n) * settings.ticket_price
                p.reviewed_by = cb.from_user.id
                tg_id = (await s.get(User, p.user_id)).telegram_id
        except (IntegrityError, CreditAlreadyClaimed):
            return await cb.answer("That transaction is already used by another approved payment.", show_alert=True)
        except InvalidState as e:
            return await cb.answer(str(e), show_alert=True)
        await safe_send(bot, tg_id, M.APPROVED.format(numbers=", ".join(settings.fmt(x) for x in numbers)))
        await _close_card(cb, f"✅ Approved by {cb.from_user.full_name}: {', '.join(settings.fmt(x) for x in numbers)}")

    @admin.callback_query(F.data.startswith("rv:r:"))
    async def cb_reject(cb: CallbackQuery, bot: Bot):
        pid = int(cb.data.split(":")[2])
        try:
            async with sm() as s, s.begin():
                p = await reject(s, pid, M.ADMIN_REJECTED, f"admin:{cb.from_user.id}")
                p.reviewed_by = cb.from_user.id
                tg_id = (await s.get(User, p.user_id)).telegram_id
        except InvalidState as e:
            return await cb.answer(str(e), show_alert=True)
        await safe_send(bot, tg_id, M.ADMIN_REJECTED)
        await _close_card(cb, f"❌ Rejected by {cb.from_user.full_name}")

    async def _close_card(cb: CallbackQuery, note: str):
        if cb.message is not None and hasattr(cb.message, "edit_caption"):
            try:
                await cb.message.edit_caption(caption=f"{cb.message.caption or ''}\n\n{note}"[:1020], reply_markup=None)
            except TelegramBadRequest:
                pass
        await cb.answer("Done")

    @admin.message(Command("stats"))
    async def stats(msg: Message):
        async with sm() as s:
            by_status = dict(
                (await s.execute(select(Payment.status, func.count()).group_by(Payment.status))).all()
            )
            n_tickets = (await s.execute(select(func.count()).select_from(Ticket))).scalar_one()
            n_people = (await s.execute(select(func.count(func.distinct(Ticket.user_id))))).scalar_one()
            nxt = (await s.execute(select(Counter.next_number))).scalar_one()
        lines = [f"{k}: {v}" for k, v in sorted(by_status.items())] or ["no payments yet"]
        await msg.answer(
            "📊 Stats\n" + "\n".join(lines) + f"\n\nTickets issued: {n_tickets}\nParticipants: {n_people}\n"
            f"Next number: {settings.fmt(nxt)}"
        )

    @admin.message(Command("export"))
    async def export(msg: Message, command: CommandObject):
        public = (command.args or "").strip().lower() == "public"
        async with sm() as s:
            if public:
                csv_bytes, count = await build_tickets_csv(s, settings, public=True)
                return await msg.answer_document(
                    BufferedInputFile(csv_bytes, filename="tickets_public.csv"),
                    caption=f"📋 {count} tickets (public list)",
                )
            excel_bytes, count = await build_holders_excel(s, settings)
            csv_bytes, _ = await build_tickets_csv(s, settings, public=False)

        await msg.answer_document(
            BufferedInputFile(excel_bytes, filename="lottery_tickets.xlsx"),
            caption=f"📊 Excel Report: {count} tickets with full names",
        )
        await msg.answer_document(
            BufferedInputFile(csv_bytes, filename="tickets_full.csv"),
            caption=f"📄 CSV Backup: {count} tickets",
        )

    @admin.message(Command("find"))
    async def find(msg: Message, command: CommandObject):
        arg = (command.args or "").strip()
        if not arg:
            return await msg.answer("Usage: /find <ticket number> | @username | <transaction id>")
        async with sm() as s:
            if arg.lstrip("#").isdigit():
                num = int(arg.lstrip("#"))
                row = (
                    await s.execute(
                        select(User, Ticket.payment_id).join(Ticket, Ticket.user_id == User.id).where(Ticket.number == num)
                    )
                ).first()
                if not row:
                    return await msg.answer("No such ticket.")
                u, pid = row
                return await msg.answer(f"{settings.fmt(num)} → {u.full_name} @{u.username} ({u.telegram_id}), payment #{pid}")
            if arg.startswith("@"):
                u = (await s.execute(select(User).where(User.username == arg[1:]))).scalar_one_or_none()
                if not u:
                    return await msg.answer("No such user.")
                nums = (await s.execute(select(Ticket.number).where(Ticket.user_id == u.id).order_by(Ticket.number))).scalars().all()
                return await msg.answer(f"{u.full_name} ({u.telegram_id}): " + (", ".join(settings.fmt(n) for n in nums) or "no tickets"))
            ps = (await s.execute(select(Payment).where(Payment.transaction_id == normalize_ref(arg)))).scalars().all()
            if not ps:
                return await msg.answer("No payment with that reference.")
            await msg.answer("\n".join(f"payment #{p.id}: {p.status} {p.reject_reason or ''}" for p in ps))

    dp = Dispatcher()
    dp.include_router(admin)
    dp.include_router(user)
    return dp
