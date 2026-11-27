"""State changes on payments. Everything here runs inside the caller's transaction.

Lock order is always: payment row -> bank_credit row -> counter row. A consistent order
means concurrent approvals can never deadlock. Any exception rolls the whole transaction
back, including the counter bump, so ticket numbers stay gap-free.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog, BankCredit, Counter, Payment, Ticket, User, utcnow


class InvalidState(Exception):
    """The payment is not in a state that allows this transition."""


class CreditAlreadyClaimed(Exception):
    """The bank credit already backs a different payment."""


class SoldOutError(Exception):
    """Issuing the requested tickets would exceed the campaign's maximum ticket cap."""

    def __init__(self, remaining: int):
        self.remaining = remaining
        super().__init__(f"Only {remaining} ticket(s) remaining before campaign cap.")


async def audit(session: AsyncSession, payment_id: int | None, actor: str, event: str, detail: str | None = None):
    session.add(AuditLog(payment_id=payment_id, actor=actor, event=event, detail=detail))


async def approve(
    session: AsyncSession,
    payment_id: int,
    n: int,
    actor: str,
    credit_id: int | None = None,
    *,
    max_total_tickets: int = 0,
    ticket_price: Decimal | None = None,
    new_balance: Decimal | None = None,
) -> list[int]:
    """Approve a payment and issue `n` consecutive ticket numbers. Idempotent.

    Raises CreditAlreadyClaimed / SoldOutError / sqlalchemy IntegrityError / InvalidState.
    In every error case the caller's transaction must be rolled back.
    """
    if n < 1:
        raise ValueError("n must be >= 1")

    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one()

    if payment.status == "approved":  # idempotent: a retried job / double tap issues nothing new
        rows = await session.execute(
            select(Ticket.number)
            .where(Ticket.payment_id == payment_id, Ticket.is_voided.is_(False))
            .order_by(Ticket.number)
        )
        return list(rows.scalars())
    if payment.status not in ("verifying", "review"):
        raise InvalidState(f"payment {payment_id} is {payment.status}")

    if credit_id is not None:
        credit = (
            await session.execute(select(BankCredit).where(BankCredit.id == credit_id).with_for_update())
        ).scalar_one()
        if credit.claimed_by_payment_id not in (None, payment_id):
            raise CreditAlreadyClaimed(f"credit {credit_id}")
        credit.claimed_by_payment_id = payment_id
        payment.transaction_id = credit.reference  # the trusted value, not the OCR'd one
        payment.credit_id = credit.id
        if ticket_price is not None:
            expected = Decimal(n) * ticket_price
            if new_balance is None and credit.amount > expected:
                payment.amount = expected
                payment.overpayment_amount = credit.amount - expected
            elif new_balance is not None:
                payment.amount = expected
                payment.overpayment_amount = Decimal("0")
            else:
                payment.amount = credit.amount
                payment.overpayment_amount = Decimal("0")
        else:
            payment.amount = credit.amount
    elif ticket_price is not None:
        payment.amount = Decimal(n) * ticket_price
        payment.overpayment_amount = Decimal("0")

    if new_balance is not None:
        user = (
            await session.execute(select(User).where(User.id == payment.user_id).with_for_update())
        ).scalar_one()
        user.credit_balance = new_balance

    counter = (await session.execute(select(Counter).where(Counter.id == 1).with_for_update())).scalar_one()
    if max_total_tickets > 0:
        active_count = (
            await session.execute(select(func.count(Ticket.number)).where(Ticket.is_voided.is_(False)))
        ).scalar_one() or 0
        if active_count + n > max_total_tickets:
            raise SoldOutError(remaining=max(0, max_total_tickets - int(active_count)))

    first = counter.next_number
    numbers = list(range(first, first + n))
    session.add_all([Ticket(number=x, payment_id=payment_id, user_id=payment.user_id) for x in numbers])
    counter.next_number = first + n

    payment.status = "approved"
    payment.reject_reason = None
    await audit(session, payment_id, actor, "approved", f"tickets {numbers[0]}..{numbers[-1]}")
    await session.flush()  # surfaces unique violations (txid / ticket number) right here
    return numbers


async def credit_wallet(
    session: AsyncSession,
    payment_id: int,
    credit_id: int,
    actor: str = "auto",
) -> tuple[Decimal, Decimal]:
    """Record a partial payment into the user's wallet balance for split-transfer combining."""
    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one()
    if payment.status not in ("verifying", "review"):
        raise InvalidState(f"payment {payment_id} is {payment.status}")

    credit = (
        await session.execute(select(BankCredit).where(BankCredit.id == credit_id).with_for_update())
    ).scalar_one()
    if credit.claimed_by_payment_id not in (None, payment_id):
        raise CreditAlreadyClaimed(f"credit {credit_id}")

    user = (
        await session.execute(select(User).where(User.id == payment.user_id).with_for_update())
    ).scalar_one()

    credit.claimed_by_payment_id = payment_id
    payment.transaction_id = credit.reference
    payment.amount = credit.amount
    payment.credit_id = credit.id
    payment.status = "credited"
    payment.reject_reason = None

    new_bal = Decimal(str(user.credit_balance or 0)) + credit.amount
    user.credit_balance = new_bal
    await audit(session, payment_id, actor, "credited_wallet", f"+{credit.amount} ETB (balance {new_bal})")
    await session.flush()
    return credit.amount, new_bal


async def void_payment(
    session: AsyncSession,
    payment_id: int,
    reason: str,
    actor: str,
) -> tuple[list[int], int]:
    """Void an approved payment and mark its issued tickets voided with full audit history."""
    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one()
    if payment.status != "approved":
        raise InvalidState(f"payment {payment_id} is {payment.status}, only approved payments can be voided")

    nums = list(
        (
            await session.execute(
                select(Ticket.number)
                .where(Ticket.payment_id == payment_id, Ticket.is_voided.is_(False))
                .order_by(Ticket.number)
            )
        ).scalars()
    )
    await session.execute(
        update(Ticket).where(Ticket.payment_id == payment_id).values(is_voided=True)
    )

    old_cid = payment.credit_id
    if old_cid is not None:
        credit = (
            await session.execute(select(BankCredit).where(BankCredit.id == old_cid).with_for_update())
        ).scalar_one_or_none()
        if credit is not None and credit.claimed_by_payment_id == payment_id:
            credit.claimed_by_payment_id = None
        payment.credit_id = None

    payment.status = "voided"
    payment.reject_reason = reason
    user = await session.get(User, payment.user_id)
    tg_id = int(user.telegram_id) if user else 0
    await audit(session, payment_id, actor, "voided", f"tickets {nums}; old_credit={old_cid}; reason={reason}")
    await session.flush()
    return nums, tg_id


async def reject(session: AsyncSession, payment_id: int, reason: str, actor: str) -> Payment:
    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one()
    if payment.status == "approved":
        raise InvalidState(f"payment {payment_id} is already approved")
    payment.status = "rejected"
    payment.reject_reason = reason
    await audit(session, payment_id, actor, "rejected", reason)
    return payment


async def send_to_review(session: AsyncSession, payment_id: int, reason: str, actor: str = "auto") -> Payment:
    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one()
    if payment.status == "approved":
        raise InvalidState(f"payment {payment_id} is already approved")
    payment.status = "review"
    payment.reject_reason = reason
    await audit(session, payment_id, actor, "review", reason)
    return payment


async def schedule_retry(session: AsyncSession, payment_id: int, delay_seconds: int) -> None:
    from datetime import timedelta

    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one()
    payment.status = "received"
    payment.next_attempt_at = utcnow() + timedelta(seconds=delay_seconds)
    await audit(session, payment_id, "auto", "retry", f"attempt {payment.attempts}")
