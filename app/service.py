"""State changes on payments. Everything here runs inside the caller's transaction.

Lock order is always: payment row -> bank_credit row -> counter row. A consistent order
means concurrent approvals can never deadlock. Any exception rolls the whole transaction
back, including the counter bump, so ticket numbers stay gap-free.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog, BankCredit, Counter, Payment, Ticket, utcnow


class InvalidState(Exception):
    """The payment is not in a state that allows this transition."""


class CreditAlreadyClaimed(Exception):
    """The bank credit already backs a different payment."""


async def audit(session: AsyncSession, payment_id: int | None, actor: str, event: str, detail: str | None = None):
    session.add(AuditLog(payment_id=payment_id, actor=actor, event=event, detail=detail))


async def approve(
    session: AsyncSession,
    payment_id: int,
    n: int,
    actor: str,
    credit_id: int | None = None,
) -> list[int]:
    """Approve a payment and issue `n` consecutive ticket numbers. Idempotent.

    Raises CreditAlreadyClaimed / sqlalchemy IntegrityError (reference already approved
    elsewhere) / InvalidState. In every error case the caller's transaction must be rolled back.
    """
    if n < 1:
        raise ValueError("n must be >= 1")

    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one()

    if payment.status == "approved":  # idempotent: a retried job / double tap issues nothing new
        rows = await session.execute(
            select(Ticket.number).where(Ticket.payment_id == payment_id).order_by(Ticket.number)
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
        payment.amount = credit.amount
        payment.credit_id = credit.id

    counter = (await session.execute(select(Counter).where(Counter.id == 1).with_for_update())).scalar_one()
    first = counter.next_number
    numbers = list(range(first, first + n))
    session.add_all([Ticket(number=x, payment_id=payment_id, user_id=payment.user_id) for x in numbers])
    counter.next_number = first + n

    payment.status = "approved"
    payment.reject_reason = None
    await audit(session, payment_id, actor, "approved", f"tickets {numbers[0]}..{numbers[-1]}")
    await session.flush()  # surfaces unique violations (txid / ticket number) right here
    return numbers


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
