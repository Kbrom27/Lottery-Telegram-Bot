import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import BankCredit, Counter, Payment, Ticket
from app.service import CreditAlreadyClaimed, InvalidState, approve, reject
from app.sms import add_credit

from .conftest import make_payment


async def test_concurrent_allocation_is_gap_free_and_unique(db):
    sm, _ = db
    ids = [await make_payment(sm, 1000 + i % 50, i, status="verifying") for i in range(200)]
    sizes = [1 + (i % 3) for i in range(200)]

    async def one(pid, n):
        async with sm() as s, s.begin():
            return await approve(s, pid, n, "test")

    results = await asyncio.gather(*(one(p, n) for p, n in zip(ids, sizes)))

    issued = sorted(x for r in results for x in r)
    assert issued == list(range(1, sum(sizes) + 1)), "numbers must be contiguous with no duplicates"
    for r, n in zip(results, sizes):  # each payment's block is consecutive
        assert r == list(range(r[0], r[0] + n))
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Ticket))).scalar_one() == sum(sizes)
        assert (await s.execute(select(Counter.next_number))).scalar_one() == sum(sizes) + 1


async def test_approve_is_idempotent(db):
    sm, _ = db
    pid = await make_payment(sm, 1, 1, status="verifying")
    async with sm() as s, s.begin():
        first = await approve(s, pid, 2, "test")
    async with sm() as s, s.begin():
        again = await approve(s, pid, 2, "test")  # retried job / double tap
    assert first == again == [1, 2]
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Ticket))).scalar_one() == 2


async def test_failed_approval_leaves_no_gap(db):
    sm, _ = db
    p1 = await make_payment(sm, 1, 1, status="verifying")
    p2 = await make_payment(sm, 2, 2, status="verifying")
    p3 = await make_payment(sm, 3, 3, status="verifying")
    async with sm() as s, s.begin():
        credit = await add_credit(s, "REF000000001", Decimal("100"))
    async with sm() as s, s.begin():
        await approve(s, p1, 1, "test", credit.id)

    with pytest.raises(CreditAlreadyClaimed):  # same credit, different payment
        async with sm() as s, s.begin():
            await approve(s, p2, 5, "test", credit.id)

    async with sm() as s, s.begin():
        got = await approve(s, p3, 1, "test")
    assert got == [2], "the failed attempt must not consume numbers"


async def test_same_reference_cannot_back_two_approved_payments(db):
    from sqlalchemy.exc import IntegrityError

    sm, _ = db
    p1 = await make_payment(sm, 1, 1, status="verifying")
    p2 = await make_payment(sm, 2, 2, status="verifying")
    async with sm() as s, s.begin():
        (await s.get(Payment, p1)).transaction_id = "SAMEREF12345"
        (await s.get(Payment, p2)).transaction_id = "SAMEREF12345"
    async with sm() as s, s.begin():
        await approve(s, p1, 1, "admin:1")
    with pytest.raises(IntegrityError):  # partial unique index on approved payments
        async with sm() as s, s.begin():
            await approve(s, p2, 1, "admin:1")


async def test_state_guards(db):
    sm, _ = db
    pid = await make_payment(sm, 1, 1, status="received")
    with pytest.raises(InvalidState):
        async with sm() as s, s.begin():
            await approve(s, pid, 1, "test")  # not verifying/review
    pid2 = await make_payment(sm, 2, 2, status="verifying")
    async with sm() as s, s.begin():
        await approve(s, pid2, 1, "test")
    with pytest.raises(InvalidState):
        async with sm() as s, s.begin():
            await reject(s, pid2, "nope", "admin:1")  # cannot reject after tickets were issued
