"""Background worker: picks up payments, extracts, verifies, allocates, notifies.

The queue is the `payments` table itself (status='received', FOR UPDATE SKIP LOCKED), so
there is no Redis/Celery to run and a crash can't lose work: stale 'verifying' rows are
re-queued automatically. Several worker coroutines (or processes) can run side by side.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import messages as M
from .config import Settings
from .db import is_sqlite_url
from .extract import Extracted
from .models import Payment, User, utcnow
from .service import CreditAlreadyClaimed, approve, reject, schedule_retry, send_to_review
from .sms import add_credit
from .verify import Verdict, decide

log = logging.getLogger("worker")

FetchFile = Callable[[str], Awaitable[bytes]]  # telegram file_id -> bytes
Notify = Callable[[int, str], Awaitable[None]]  # telegram_id, text
OnReview = Callable[[int], Awaitable[None]]  # payment_id


class Worker:
    def __init__(
        self,
        sm: async_sessionmaker,
        settings: Settings,
        extractor,
        fetch_file: FetchFile,
        notify: Notify,
        on_review: OnReview | None = None,
        poll_seconds: float = 1.0,
    ):
        self.sm, self.settings, self.extractor = sm, settings, extractor
        self.fetch_file, self.notify, self.on_review = fetch_file, notify, on_review
        self.poll_seconds = poll_seconds

    # ---- queue ---------------------------------------------------------------------------

    async def claim_next(self) -> int | None:
        async with self.sm() as s, s.begin():
            row = (
                await s.execute(
                    select(Payment)
                    .where(Payment.status == "received", Payment.next_attempt_at <= utcnow())
                    .order_by(Payment.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = "verifying"
            row.attempts += 1
            return row.id

    async def requeue_stale(self, minutes: int = 10) -> int:
        """Rows stuck in 'verifying' (worker crashed mid-job) go back in the queue."""
        async with self.sm() as s, s.begin():
            res = await s.execute(
                update(Payment)
                .where(Payment.status == "verifying", Payment.updated_at < utcnow() - timedelta(minutes=minutes))
                .values(status="received")
            )
            return res.rowcount or 0

    async def run_forever(self, concurrency: int = 3) -> None:
        if is_sqlite_url(self.settings.database_url):
            concurrency = 1
        await self.requeue_stale()

        async def loop():
            while True:
                try:
                    pid = await self.claim_next()
                    if pid is None:
                        await asyncio.sleep(self.poll_seconds)
                        continue
                    await self.process(pid)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("worker loop error")
                    await asyncio.sleep(2)

        async def janitor():
            while True:
                await asyncio.sleep(120)
                try:
                    n = await self.requeue_stale()
                    if n:
                        log.warning("re-queued %d stale payments", n)
                except Exception:
                    log.exception("janitor error")

        await asyncio.gather(*(loop() for _ in range(concurrency)), janitor())

    # ---- one payment ---------------------------------------------------------------------

    async def process(self, payment_id: int) -> None:
        try:
            await self._process(payment_id)
        except Exception as e:  # never leave a payment stuck: hand it to a human
            log.exception("processing payment %s failed", payment_id)
            await self._finish(payment_id, Verdict("review", f"Internal error: {type(e).__name__}: {e}"))

    async def _process(self, payment_id: int) -> None:
        async with self.sm() as s:
            payment = await s.get(Payment, payment_id)
            tg_id = (await s.get(User, payment.user_id)).telegram_id

        data = await self._load_image(payment)
        if data is None:
            return await self._finish(payment_id, Verdict("reject", M.NOT_AN_IMAGE))

        sha = hashlib.sha256(data).hexdigest()
        async with self.sm() as s, s.begin():
            p = await s.get(Payment, payment_id)
            p.image_sha256 = sha
            dup = (
                await s.execute(
                    select(Payment.id).where(
                        Payment.image_sha256 == sha,
                        Payment.id < payment_id,  # the earlier submission wins; avoids mutual rejection
                        Payment.status.in_(("received", "verifying", "review", "approved")),
                    )
                )
            ).first()
        if dup:
            return await self._finish(payment_id, Verdict("reject", M.DUPLICATE_IMAGE))

        if payment.extracted is None:
            ex = await asyncio.to_thread(self.extractor.extract, data)
            async with self.sm() as s, s.begin():
                p = await s.get(Payment, payment_id)
                p.extracted = ex.to_json()
                p.transaction_id = ex.transaction_id
                p.amount = ex.amount
                p.provider = ex.provider
        else:
            ex = Extracted.from_json(payment.extracted)

        # If bank QR code / receipt URL was verified directly with CBE:
        if ex.verified_bank_credit:
            vc = ex.verified_bank_credit
            cbe_acc = self.settings.cbe_account.strip()
            target_suffix = cbe_acc[-4:] if len(cbe_acc) >= 4 else cbe_acc
            credit_acc = str(vc.get("credit_account") or "")
            if target_suffix and credit_acc and target_suffix not in credit_acc:
                return await self._finish(
                    payment_id,
                    Verdict("reject", f"❌ Payment was sent to a different account ({credit_acc}), not our receiving account."),
                    tg_id,
                )
            if target_suffix and target_suffix in credit_acc:
                received_at = None
                raw_ts = vc.get("received_at")
                if isinstance(raw_ts, str) and raw_ts.strip():
                    try:
                        parsed_ts = datetime.fromisoformat(raw_ts.strip().replace("Z", "+00:00"))
                        if parsed_ts.tzinfo is None:
                            parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                        received_at = parsed_ts
                    except ValueError:
                        received_at = None
                async with self.sm() as s, s.begin():
                    await add_credit(
                        s,
                        reference=vc["reference"],
                        amount=Decimal(vc["amount"]),
                        sender=vc.get("sender"),
                        received_at=received_at,
                        raw_text=vc.get("raw_text"),
                    )

        # If local OCR isn't installed and no CBE QR code was found, route to manual review rather than rejecting
        if not ex.transaction_id and ex.raw_text.startswith("[OCR unavailable"):
            return await self._finish(
                payment_id,
                Verdict("review", "No CBE QR code found and Tesseract OCR is not installed on this PC"),
                tg_id,
            )

        async with self.sm() as s:
            p = await s.get(Payment, payment_id)
            verdict = await decide(s, self.settings, p, ex, utcnow())
        await self._finish(payment_id, verdict, tg_id)

    async def _load_image(self, payment: Payment) -> bytes | None:
        path = payment.image_path
        if path and os.path.exists(path):
            data = await asyncio.to_thread(_read, path)
        else:
            data = await self.fetch_file(payment.tg_file_id)
            os.makedirs(self.settings.storage_dir, exist_ok=True)
            path = os.path.join(self.settings.storage_dir, f"{payment.id}.img")
            await asyncio.to_thread(_write, path, data)
            async with self.sm() as s, s.begin():
                (await s.get(Payment, payment.id)).image_path = path
        try:
            await asyncio.to_thread(_verify_image, data)
        except Exception:
            return None
        return data

    async def _finish(self, payment_id: int, verdict: Verdict, tg_id: int | None = None) -> None:
        if tg_id is None:
            async with self.sm() as s:
                p = await s.get(Payment, payment_id)
                tg_id = (await s.get(User, p.user_id)).telegram_id

        if verdict.outcome == "approve":
            try:
                async with self.sm() as s, s.begin():
                    numbers = await approve(s, payment_id, verdict.tickets, "auto", verdict.credit_id)
            except (IntegrityError, CreditAlreadyClaimed):
                verdict = Verdict("reject", M.ALREADY_USED)
            else:
                text = M.APPROVED.format(numbers=", ".join(self.settings.fmt(n) for n in numbers))
                await self.notify(tg_id, text)
                return

        if verdict.outcome == "retry":
            async with self.sm() as s, s.begin():
                await schedule_retry(s, payment_id, self.settings.retry_interval_seconds)
            return

        if verdict.outcome == "reject":
            async with self.sm() as s, s.begin():
                await reject(s, payment_id, verdict.reason or "", "auto")
            await self.notify(tg_id, verdict.reason or M.ADMIN_REJECTED)
            return

        # review
        async with self.sm() as s, s.begin():
            await send_to_review(s, payment_id, verdict.reason or "")
        await self.notify(tg_id, M.UNDER_REVIEW)
        if self.on_review:
            await self.on_review(payment_id)


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def _verify_image(data: bytes) -> None:
    Image.open(io.BytesIO(data)).verify()
