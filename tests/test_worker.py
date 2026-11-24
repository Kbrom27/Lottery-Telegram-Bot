"""End-to-end verification flow against real PostgreSQL, with fake Telegram + fake OCR."""
import asyncio
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, update

from app import messages as M
from app.extract import Extracted
from app.models import Payment, Ticket, utcnow
from app.sms import add_credit
from app.worker import Worker

from .conftest import make_payment, png


class FakeExtractor:
    """Maps image bytes -> what OCR 'read' from it."""

    def __init__(self):
        self.by_image: dict[bytes, Extracted] = {}

    def extract(self, image: bytes) -> Extracted:
        return self.by_image[image]


class Harness:
    def __init__(self, sm, settings):
        self.sm, self.settings = sm, settings
        self.ocr = FakeExtractor()
        self.images: dict[str, bytes] = {}
        self.sent: list[tuple[int, str]] = []
        self.reviews: list[int] = []

        async def fetch(file_id):
            return self.images[file_id]

        async def notify(tg, text):
            self.sent.append((tg, text))

        async def on_review(pid):
            self.reviews.append(pid)

        self.worker = Worker(sm, settings, self.ocr, fetch, notify, on_review)

    async def submit(self, tg: int, seed: int, ex: Extracted, same_image_as: int | None = None) -> int:
        img = png(same_image_as if same_image_as is not None else seed)
        pid = await make_payment(self.sm, tg, seed)
        self.images[f"img-{seed}"] = img
        self.ocr.by_image[img] = ex
        return pid

    async def run(self, pid: int):
        got = await self.worker.claim_next()
        assert got == pid, f"expected to claim {pid}, got {got}"
        await self.worker.process(pid)

    async def payment(self, pid) -> Payment:
        async with self.sm() as s:
            return await s.get(Payment, pid)

    async def tickets(self, pid) -> list[int]:
        async with self.sm() as s:
            return list((await s.execute(select(Ticket.number).where(Ticket.payment_id == pid).order_by(Ticket.number))).scalars())

    async def credit(self, ref, amount, sender=None):
        async with self.sm() as s, s.begin():
            return await add_credit(s, ref, Decimal(amount), sender)


async def test_happy_path(db):
    h = Harness(*db)
    await h.credit("DKL4A7B9Q2", "100", "Abebe Kebede")
    pid = await h.submit(11, 1, Extracted(transaction_id="DKL4A7B9Q2", amount=Decimal("100"), sender="Abebe K"))
    await h.run(pid)
    p = await h.payment(pid)
    assert p.status == "approved" and p.transaction_id == "DKL4A7B9Q2"
    assert await h.tickets(pid) == [1]
    assert h.sent == [(11, M.APPROVED.format(numbers="#00001"))]


async def test_multiple_tickets_for_multiple_of_price(db):
    h = Harness(*db)
    await h.credit("MULTI0000001", "300")
    pid = await h.submit(11, 1, Extracted(transaction_id="MULTI0000001"))
    await h.run(pid)
    assert await h.tickets(pid) == [1, 2, 3]


async def test_screenshot_reuse_by_another_user_is_rejected(db):
    h = Harness(*db)
    await h.credit("DKL4A7B9Q2", "100")
    a = await h.submit(11, 1, Extracted(transaction_id="DKL4A7B9Q2"))
    b = await h.submit(22, 2, Extracted(transaction_id="DKL4A7B9Q2"))
    await h.run(a)
    await h.run(b)
    assert (await h.payment(a)).status == "approved"
    pb = await h.payment(b)
    assert pb.status == "rejected" and pb.reject_reason == M.ALREADY_USED
    assert await h.tickets(b) == []


async def test_exact_duplicate_image_is_rejected(db):
    h = Harness(*db)
    await h.credit("DKL4A7B9Q2", "100")
    a = await h.submit(11, 1, Extracted(transaction_id="DKL4A7B9Q2"))
    b = await h.submit(22, 2, Extracted(transaction_id="DKL4A7B9Q2"), same_image_as=1)
    await h.run(a)
    await h.run(b)
    assert (await h.payment(b)).reject_reason == M.DUPLICATE_IMAGE


async def test_fake_screenshot_without_matching_money_goes_to_review_after_wait(db):
    h = Harness(*db)
    pid = await h.submit(11, 1, Extracted(transaction_id="FAKE00000000", amount=Decimal("100")))
    await h.run(pid)  # first attempt: SMS may simply be late -> retry, no tickets
    p = await h.payment(pid)
    assert p.status == "received" and p.next_attempt_at > utcnow()
    assert h.sent == [] and await h.tickets(pid) == []

    async with h.sm() as s, s.begin():  # 30 minutes later, still nothing arrived
        await s.execute(update(Payment).where(Payment.id == pid).values(created_at=utcnow() - timedelta(minutes=30), next_attempt_at=utcnow()))
    await h.run(pid)
    p = await h.payment(pid)
    assert p.status == "review" and "No matching incoming payment" in p.reject_reason
    assert h.reviews == [pid] and h.sent == [(11, M.UNDER_REVIEW)]
    assert await h.tickets(pid) == []


async def test_late_bank_sms_is_picked_up_on_retry(db):
    h = Harness(*db)
    pid = await h.submit(11, 1, Extracted(transaction_id="LATE00000001"))
    await h.run(pid)
    assert (await h.payment(pid)).status == "received"
    await h.credit("LATE00000001", "100")  # the bank SMS finally arrives
    async with h.sm() as s, s.begin():
        await s.execute(update(Payment).where(Payment.id == pid).values(next_attempt_at=utcnow()))
    await h.run(pid)
    assert (await h.payment(pid)).status == "approved" and await h.tickets(pid) == [1]


async def test_edited_amount_on_screenshot_goes_to_review(db):
    h = Harness(*db)
    await h.credit("EDIT00000001", "100")
    pid = await h.submit(11, 1, Extracted(transaction_id="EDIT00000001", amount=Decimal("1000")))
    await h.run(pid)
    p = await h.payment(pid)
    assert p.status == "review" and "possible edited image" in p.reject_reason
    assert await h.tickets(pid) == []


async def test_sender_mismatch_goes_to_review(db):
    h = Harness(*db)
    await h.credit("SEND00000001", "100", "Abebe Kebede")
    pid = await h.submit(11, 1, Extracted(transaction_id="SEND00000001", sender="Someone Else"))
    await h.run(pid)
    assert (await h.payment(pid)).status == "review"


async def test_underpayment_rejected_and_odd_amount_reviewed(db):
    h = Harness(*db)
    await h.credit("UNDER0000001", "50")
    await h.credit("ODD000000001", "250")
    a = await h.submit(11, 1, Extracted(transaction_id="UNDER0000001"))
    b = await h.submit(22, 2, Extracted(transaction_id="ODD000000001"))
    await h.run(a)
    await h.run(b)
    assert (await h.payment(a)).status == "rejected"
    assert (await h.payment(b)).status == "review"


async def test_ocr_confusable_characters_still_match(db):
    h = Harness(*db)
    await h.credit("DKL4A7B9Q2", "100")
    pid = await h.submit(11, 1, Extracted(transaction_id="DKI4A7B9O2"))  # L->I, Q->O misreads
    await h.run(pid)
    assert (await h.payment(pid)).status == "approved"


async def test_unreadable_and_invalid_images_are_rejected(db):
    h = Harness(*db)
    a = await h.submit(11, 1, Extracted(transaction_id=None))
    await h.run(a)
    assert (await h.payment(a)).reject_reason == M.UNREADABLE
    b = await make_payment(h.sm, 22, 2)
    h.images["img-2"] = b"this is not an image"
    await h.run(b)
    assert (await h.payment(b)).reject_reason == M.NOT_AN_IMAGE


async def test_crashed_worker_rows_are_requeued(db):
    h = Harness(*db)
    pid = await make_payment(h.sm, 11, 1, status="verifying")
    async with h.sm() as s, s.begin():
        await s.execute(update(Payment).where(Payment.id == pid).values(updated_at=utcnow() - timedelta(minutes=30)))
    assert await h.worker.requeue_stale() == 1
    assert (await h.payment(pid)).status == "received"


async def test_many_workers_in_parallel_issue_contiguous_numbers(db):
    h = Harness(*db)
    n = 60
    pids = []
    for i in range(n):
        await h.credit(f"PAR{i:09d}", "100")
        pids.append(await h.submit(1000 + i, i, Extracted(transaction_id=f"PAR{i:09d}", amount=Decimal("100"))))

    async def worker_loop():
        while (pid := await h.worker.claim_next()) is not None:
            await h.worker.process(pid)

    await asyncio.gather(*(worker_loop() for _ in range(6)))
    async with h.sm() as s:
        nums = sorted((await s.execute(select(Ticket.number))).scalars())
        approved = (await s.execute(select(func.count()).select_from(Payment).where(Payment.status == "approved"))).scalar_one()
    assert approved == n and nums == list(range(1, n + 1))
