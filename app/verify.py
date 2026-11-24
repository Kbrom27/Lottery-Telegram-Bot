"""Decide what to do with a payment. Read-only: returns a Verdict, writes nothing.

Trust model: the screenshot gives us a *reference*. The reference is looked up in
`bank_credits` (money that really arrived). Amount, date and "already used" are all judged
from the credit, never from the image.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import messages as M
from .config import Settings
from .extract import Extracted, canon_ref, normalize_ref
from .models import BankCredit, Payment


@dataclass
class Verdict:
    outcome: str  # "approve" | "reject" | "review" | "retry"
    reason: str | None = None  # user-facing for "reject"; internal note for "review"
    credit_id: int | None = None
    tickets: int = 0


async def find_credit(session: AsyncSession, reference: str) -> BankCredit | None:
    """Exact match first; otherwise tolerate OCR confusions if exactly one credit matches."""
    exact = (
        await session.execute(select(BankCredit).where(BankCredit.reference == normalize_ref(reference)))
    ).scalar_one_or_none()
    if exact:
        return exact
    rows = (
        (await session.execute(select(BankCredit).where(BankCredit.reference_canon == canon_ref(reference))))
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{2,}", name.lower())}


def names_compatible(a: str | None, b: str | None) -> bool:
    """If both names are known, they must share at least one word. Unknown => compatible."""
    if not a or not b:
        return True
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return True
    if bool(ta & tb):
        return True
    # Fuzzy match for minor transliteration variations (e.g. "kobrom" vs "kbrom")
    for x in ta:
        for y in tb:
            if difflib.SequenceMatcher(None, x, y).ratio() >= 0.75:
                return True
    return False


async def decide(
    session: AsyncSession, settings: Settings, payment: Payment, ex: Extracted, now: datetime
) -> Verdict:
    if not ex.transaction_id:
        return Verdict("reject", M.UNREADABLE)

    credit = await find_credit(session, ex.transaction_id)
    if credit is None:
        # The bank SMS can arrive after the user's screenshot. Wait for it before escalating.
        if now - payment.created_at < timedelta(minutes=settings.credit_wait_minutes):
            return Verdict("retry")
        return Verdict("review", "No matching incoming payment found in the receiving account")

    if credit.claimed_by_payment_id not in (None, payment.id):
        return Verdict("reject", M.ALREADY_USED)

    if not (settings.campaign_start <= credit.received_at <= settings.campaign_end):
        return Verdict("reject", M.OUTSIDE_WINDOW)

    price = settings.ticket_price
    if credit.amount < price:
        return Verdict("reject", M.UNDERPAID.format(amount=credit.amount, price=price))
    n, remainder = divmod(credit.amount, price)
    n = int(n)
    if remainder != 0:
        return Verdict("review", f"Amount {credit.amount} is not a multiple of the ticket price {price}", credit.id, n)
    if n > settings.max_tickets_per_payment:
        return Verdict("review", f"Amount {credit.amount} would be {n} tickets (max {settings.max_tickets_per_payment})", credit.id, n)

    if ex.amount is not None and ex.amount != credit.amount:
        # Tolerate bank transfer fee variance (e.g. CBE debiting 100.61 with 0.61 fee)
        # or if the exact credit amount appears in raw text
        is_fee_variance = abs(ex.amount - credit.amount) <= Decimal("5.00")
        appears_in_text = bool(
            ex.raw_text and (f"{credit.amount:.2f}" in ex.raw_text or f"{credit.amount:.0f}" in ex.raw_text)
        )
        if not (is_fee_variance or appears_in_text):
            return Verdict(
                "review",
                f"Screenshot shows {ex.amount} but {credit.amount} was received (possible edited image)",
                credit.id,
                n,
            )
    if not names_compatible(ex.sender, credit.sender):
        return Verdict(
            "review",
            f"Sender on screenshot ({ex.sender!r}) differs from sender of the payment ({credit.sender!r})",
            credit.id,
            n,
        )

    return Verdict("approve", credit_id=credit.id, tickets=n)
