"""Ingest "money received" SMS from the receiving account into `bank_credits`.

How the SMS gets here: install an SMS-forwarder app on the phone that holds the receiving
SIM and point it at POST /sms (see README). You can also import a bank statement CSV
(`python -m app.import_credits statement.csv`) to reconcile anything that was missed.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .extract import canon_ref, find_amount, find_sender, find_transaction_id
from .models import BankCredit, utcnow

log = logging.getLogger("sms")

# Only incoming-money messages. Debits / OTPs / promos are ignored.
_CREDIT_WORDS = re.compile(r"\b(received|credited|deposit(?:ed)?)\b", re.I)
_CBE_URL_RE = re.compile(r"https?://(?:www\.)?mbreciept\.cbe\.com\.et/([A-Za-z0-9_-]+)", re.I)


def resolve_cbe_receipt(token: str) -> tuple[str, Decimal, str | None] | None:
    try:
        req = urllib.request.Request(
            f"https://Mb.cbe.com.et/api/v1/transactions/public/transaction-detail/{token}",
            headers={
                "X-App-ID": "d1292e42-7400-49de-a2d3-9731caa4c819",
                "X-App-Version": "0a01980b-9859-1369-8198-59f403820000",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            ref = data.get("id")
            amt_str = data.get("amountCredited") or data.get("amountDebited")
            sender = data.get("debitAccountHolder") or data.get("creditAccountHolder")
            if ref and amt_str:
                return ref, Decimal(str(amt_str)), sender
    except Exception as e:
        log.warning("CBE receipt resolution failed for %s: %s", token, e)
    return None


@dataclass
class ParsedCredit:
    reference: str
    amount: Decimal
    sender: str | None


def parse_credit_sms(text: str) -> ParsedCredit | None:
    if not _CREDIT_WORDS.search(text):
        return None

    # Auto-resolve CBE receipt URL if present
    cbe_m = _CBE_URL_RE.search(text)
    if cbe_m:
        resolved = resolve_cbe_receipt(cbe_m.group(1))
        if resolved:
            ref, amt, sender = resolved
            return ParsedCredit(reference=ref, amount=amt, sender=sender)

    ref = find_transaction_id(text)
    amount = find_amount(text)
    if not ref or amount is None:
        return None
    return ParsedCredit(reference=ref, amount=amount, sender=find_sender(text))


async def add_credit(
    session: AsyncSession,
    reference: str,
    amount: Decimal,
    sender: str | None = None,
    received_at: datetime | None = None,
    raw_text: str | None = None,
) -> BankCredit | None:
    """Insert a credit. Returns None if that reference already exists. Caller commits."""
    credit = BankCredit(
        reference=reference,
        reference_canon=canon_ref(reference),
        amount=amount,
        sender=sender,
        received_at=received_at or utcnow(),
        raw_text=raw_text,
    )
    try:
        async with session.begin_nested():
            session.add(credit)
            await session.flush()
    except IntegrityError:
        return None
    return credit


async def ingest_sms(
    session: AsyncSession, text: str, received_at: datetime | None = None
) -> tuple[str, BankCredit | None]:
    """Returns ("created" | "duplicate" | "ignored", credit)."""
    parsed = parse_credit_sms(text)
    if parsed is None:
        return "ignored", None
    credit = await add_credit(session, parsed.reference, parsed.amount, parsed.sender, received_at, text)
    return ("created", credit) if credit else ("duplicate", None)
