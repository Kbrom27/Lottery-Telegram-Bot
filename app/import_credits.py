"""Import incoming payments from a bank/Telebirr statement CSV (reconciliation / backfill).

    python -m app.import_credits statement.csv

Expected columns (header row required): reference, amount, sender (optional), received_at (optional, ISO 8601).
Existing references are skipped, so it is safe to re-import overlapping statements.
"""
from __future__ import annotations

import asyncio
import csv
import sys
from datetime import datetime
from decimal import Decimal

from .config import EAT, get_settings
from .db import init_db, make_engine, make_sessionmaker
from .extract import normalize_ref
from .sms import add_credit


async def main(path: str) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    await init_db(engine, settings)
    sm = make_sessionmaker(engine)
    created = skipped = 0
    async with sm() as s, s.begin():
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                ref = normalize_ref(row["reference"])
                received = None
                if row.get("received_at"):
                    received = datetime.fromisoformat(row["received_at"])
                    if received.tzinfo is None:
                        received = received.replace(tzinfo=EAT)
                credit = await add_credit(
                    s, ref, Decimal(row["amount"].replace(",", "")), row.get("sender") or None, received, "csv import"
                )
                created += bool(credit)
                skipped += not credit
    await engine.dispose()
    print(f"imported {created}, skipped {skipped} duplicates")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
