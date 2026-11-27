"""Regression tests for PDF/CBE-link receipts, ticket cap, cash tickets, voiding, archive/reset, broadcast, accounting, and migrations."""
from __future__ import annotations

import io
import sqlite3
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import openpyxl
import pytest
from sqlalchemy import select

from app.config import Settings, load_desktop_settings
from app.db import init_db, make_engine
from app.extract import Extracted, extract_pdf
from app.models import BankCredit, Counter, Payment, Ticket, User
from app.service_manager import BotServiceManager
from app.sms import add_credit
from app.web_ui import create_web_app
from app.worker import Worker


def _make_minimal_pdf(text_payload: str) -> bytes:
    """Create a valid minimal PDF containing uncompressed text/URL content."""
    stream = f"BT /F1 12 Tf 50 700 Td ({text_payload}) Tj ET".encode("latin-1")
    parts = [
        b"%PDF-1.4\n",
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n",
        f"4 0 obj << /Length {len(stream)} >> stream\n".encode("latin-1") + stream + b"\nendstream endobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    body = b"".join(parts)
    return body + b"trailer << /Root 1 0 R >>\n%%EOF\n"


async def test_pdf_and_pasted_cbe_link_receipts_verify_end_to_end(tmp_path):
    mock_pdf_tx = {
        "id": "FT26PDF00001",
        "status": "COMPLETED",
        "amountCredited": "200.00",
        "debitAccountHolder": "PDF Sender",
        "creditAccountNo": "1000999888777",
        "dateTimes": ["2026-06-01T10:00:00Z"],
    }
    mock_link_tx = {
        "id": "FT26LINK0002",
        "status": "COMPLETED",
        "amountCredited": "100.00",
        "debitAccountHolder": "Link Sender",
        "creditAccountNo": "1000999888777",
        "dateTimes": ["2026-06-01T11:00:00Z"],
    }
    pdf_bytes = _make_minimal_pdf("Receipt https://mbreciept.cbe.com.et/pdftok123")
    with patch("app.extract.fetch_cbe_transaction_detail", return_value=mock_pdf_tx):
        ex_pdf = extract_pdf(pdf_bytes)
    assert ex_pdf.transaction_id == "FT26PDF00001"
    assert ex_pdf.amount == Decimal("200.00")

    data_dir = tmp_path / "pdf_link_data"
    data_dir.mkdir()
    settings = Settings(
        campaign_name="Abel Online Lottery",
        bot_token="999999:TEST_TOKEN_ABCDEFGHIJKLMNOP",
        admin_ids=frozenset({123456}),
        database_url=f"sqlite+aiosqlite:///{data_dir / 'lottery.db'}",
        storage_dir=str(data_dir / "receipts"),
        cbe_account="1000999888777",
        campaign_unbounded=True,
    )
    manager = BotServiceManager(data_dir=data_dir, settings=settings)
    await manager.init_database()
    try:
        async def fetch_file(_fid: str) -> bytes:
            return pdf_bytes

        sent = []

        async def notify(tg_id: int, text: str) -> None:
            sent.append((tg_id, text))

        async with manager.sm() as s, s.begin():
            u = User(telegram_id=55001, full_name="PDF Tester", username="pdftest")
            s.add(u)
            await s.flush()
            p_pdf = Payment(user_id=u.id, tg_file_id="doc_pdf_1", status="received")
            p_link = Payment(
                user_id=u.id,
                tg_file_id="url:https://mbreciept.cbe.com.et/linktok456",
                status="received",
            )
            s.add_all([p_pdf, p_link])
            await s.flush()
            pdf_id, link_id = p_pdf.id, p_link.id

        class UnusedOCR:
            def extract(self, _img: bytes) -> Extracted:
                raise AssertionError("OCR must not be invoked for PDF or URL receipts")

        worker = Worker(manager.sm, settings, UnusedOCR(), fetch_file, notify)

        with patch("app.extract.fetch_cbe_transaction_detail", return_value=mock_pdf_tx):
            cid = await worker.claim_next()
            assert cid == pdf_id
            await worker.process(cid)

        with patch("app.extract.fetch_cbe_transaction_detail", return_value=mock_link_tx):
            cid2 = await worker.claim_next()
            assert cid2 == link_id
            await worker.process(cid2)

        async with manager.sm() as s:
            tnums = list((await s.execute(select(Ticket.number).order_by(Ticket.number))).scalars())
            assert tnums == [1, 2, 3]
        assert len(sent) == 2
        assert "#00001, #00002" in sent[0][1] and "ሰናይ ዕድል" in sent[0][1]
        assert "#00003" in sent[1][1] and "ሰናይ ዕድል" in sent[1][1]
    finally:
        await manager.shutdown()


async def test_cash_tickets_cap_void_overpayment_broadcast_and_archive_reset(tmp_path):
    data_dir = tmp_path / "full_features"
    data_dir.mkdir()
    settings = load_desktop_settings(data_dir)
    manager = BotServiceManager(data_dir=data_dir, settings=settings)
    await manager.init_database()
    app = create_web_app(manager)

    sent_messages: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **_kw):
            sent_messages.append((chat_id, text))
            return True

    manager.bot = FakeBot()

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Configure max_total_tickets = 5, strict overpayment tracking
            r = await client.post(
                "/api/settings",
                json={
                    "campaign_name": "Abel Online Lottery",
                    "bot_token": "999999:TEST_TOKEN_ABCDEFGHIJKLMNOP",
                    "admin_ids": "9262763122",
                    "ticket_price": "100",
                    "max_total_tickets": 5,
                    "allow_split_payments": False,
                    "campaign_unbounded": True,
                },
            )
            assert r.status_code == 200

            # 1. Issue 2 manual cash tickets
            m1 = await client.post(
                "/api/tickets/manual",
                json={"full_name": "Cash Buyer", "phone": "0911000001", "tickets": 2, "reference": "CASH001"},
            )
            assert m1.status_code == 200
            m1_body = m1.json()
            assert m1_body["ok"] is True and m1_body["numbers"] == ["#00001", "#00002"]
            cash_pid = m1_body["payment_id"]

            # 2. Manual review approval without credit normalizes OCR fee 100.65 -> 100.00
            #    and approval WITH 250 ETB credit for 2 tickets records 50.00 overpayment!
            async with manager.sm() as s, s.begin():
                u_tg = User(telegram_id=777001, full_name="Telegram Buyer", username="tgbuyer")
                s.add(u_tg)
                await s.flush()
                p_fee = Payment(
                    user_id=u_tg.id,
                    tg_file_id="f_fee",
                    status="review",
                    amount=Decimal("100.65"),
                    provider="telebirr",
                )
                await add_credit(s, "OVERPAY0001", Decimal("250.00"), "Telegram Buyer")
                p_over = Payment(
                    user_id=u_tg.id,
                    tg_file_id="f_over",
                    transaction_id="OVERPAY0001",
                    status="review",
                    provider="cbe",
                )
                s.add_all([p_fee, p_over])
                await s.flush()
                p_fee_id, p_over_id = p_fee.id, p_over.id

            a_fee = (await client.post(f"/api/reviews/{p_fee_id}/approve", json={"tickets": 1})).json()
            assert a_fee["ok"] is True and a_fee["numbers"] == ["#00003"]
            a_over = (await client.post(f"/api/reviews/{p_over_id}/approve", json={"tickets": 2})).json()
            assert a_over["ok"] is True and a_over["numbers"] == ["#00004", "#00005"]

            # 3. Sold-out cap (5/5): issuing 1 more cash ticket must be blocked
            m_cap = await client.post("/api/tickets/manual", json={"full_name": "Late Buyer", "tickets": 1})
            assert m_cap.status_code == 400
            assert "0 ticket(s) remaining" in m_cap.json()["error"]

            dash = (await client.get("/api/dashboard")).json()
            assert dash["sold_out"] is True
            assert dash["remaining_tickets"] == 0
            assert dash["total_birr_received"] == 500.0  # 200 cash + 100 normalized fee + 200 ticket value
            assert dash["total_overpayment_birr"] == 50.0  # 250 - 200 tracked cleanly

            # 4. Void the cash payment (#00001, #00002): active count drops to 3 (not sold out!)
            v_res = (await client.post(f"/api/payments/{cash_pid}/void", json={"reason": "Refunded cash"})).json()
            assert v_res["ok"] is True and v_res["voided_numbers"] == ["#00001", "#00002"]

            dash2 = (await client.get("/api/dashboard")).json()
            assert dash2["total_tickets_issued"] == 3
            assert dash2["remaining_tickets"] == 2
            assert dash2["sold_out"] is False
            assert dash2["voided_payments_count"] == 1

            # Draw must ONLY pick from active non-voided tickets (#00003, #00004, #00005)
            await client.post("/api/draw/commit")
            dr = (await client.post("/api/draw/run", json={"entropy": "round1-block", "winners": 1, "one_per_person": True})).json()
            assert dr["ok"] is True and dr["total_tickets"] == 3
            assert dr["winners"][0]["number"] in (3, 4, 5)

            # 5. Notify winners + Broadcast
            nw = (await client.post("/api/draw/notify-winners", json={"winners": dr["winners"]})).json()
            assert nw["ok"] is True and nw["sent"] == 1
            bc = (await client.post("/api/broadcast", json={"message": "Draw results are live!", "audience": "holders"})).json()
            assert bc["ok"] is True and bc["sent"] == 1
            assert any("Congratulations!" in txt for _, txt in sent_messages)
            assert any("Draw results are live!" in txt for _, txt in sent_messages)

            # 6. Archive & Reset campaign round
            bad_reset = await client.post("/api/campaign/reset", json={"confirm": "NO"})
            assert bad_reset.status_code == 400

            reset_res = (
                await client.post(
                    "/api/campaign/reset",
                    json={"confirm": "RESET", "new_campaign_name": "Abel Online Lottery Round 2"},
                )
            ).json()
            assert reset_res["ok"] is True
            arch = Path(reset_res["archive_path"])
            assert arch.exists()
            with zipfile.ZipFile(arch) as zf:
                names = set(zf.namelist())
                assert {"lottery.db", "lottery_tickets.xlsx", "lottery_summary_report.xlsx", "config.json"} <= names

            # After reset: 0 tickets, counter back to #00001, config & bot token preserved
            dash3 = (await client.get("/api/dashboard")).json()
            assert dash3["total_tickets_issued"] == 0
            assert dash3["next_ticket_formatted"] == "#00001"
            cfg = (await client.get("/api/settings")).json()
            assert cfg["campaign_name"] == "Abel Online Lottery Round 2"
            assert cfg["bot_token"] == "999999:TEST_TOKEN_ABCDEFGHIJKLMNOP"
    finally:
        manager.bot = None
        await manager.shutdown()


async def test_schema_auto_migration_upgrades_legacy_sqlite_db(tmp_path):
    """Simulate an older client lottery.db missing newly added columns and confirm init_db upgrades safely."""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            telegram_id BIGINT UNIQUE NOT NULL,
            username VARCHAR(64),
            full_name VARCHAR(256),
            created_at DATETIME
        );
        CREATE TABLE payments (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            tg_file_id TEXT NOT NULL,
            image_path TEXT,
            image_sha256 VARCHAR(64),
            transaction_id VARCHAR(40),
            amount NUMERIC(12,2),
            provider VARCHAR(32),
            status VARCHAR(16),
            reject_reason TEXT,
            extracted JSON,
            attempts INTEGER,
            next_attempt_at DATETIME,
            credit_id INTEGER,
            reviewed_by BIGINT,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE tickets (
            number BIGINT PRIMARY KEY,
            payment_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            issued_at DATETIME
        );
        CREATE TABLE counters (id INTEGER PRIMARY KEY, next_number BIGINT NOT NULL);
        INSERT INTO users (id, telegram_id, username, full_name) VALUES (1, 555001, 'olduser', 'Old Holder');
        INSERT INTO payments (id, user_id, tg_file_id, status, amount, attempts) VALUES (1, 1, 'old_fid', 'approved', 100.00, 1);
        INSERT INTO tickets (number, payment_id, user_id) VALUES (1, 1, 1);
        INSERT INTO counters (id, next_number) VALUES (1, 2);
        """
    )
    conn.commit()
    conn.close()

    settings = Settings(database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}")
    engine = make_engine(settings.database_url)
    try:
        await init_db(engine, settings)
    finally:
        await engine.dispose()

    conn2 = sqlite3.connect(db_file)
    try:
        user_cols = {r[1] for r in conn2.execute("PRAGMA table_info(users)").fetchall()}
        pay_cols = {r[1] for r in conn2.execute("PRAGMA table_info(payments)").fetchall()}
        tkt_cols = {r[1] for r in conn2.execute("PRAGMA table_info(tickets)").fetchall()}
        assert {"phone", "credit_balance"} <= user_cols
        assert "overpayment_amount" in pay_cols
        assert "is_voided" in tkt_cols
        # Existing data must remain intact with safe defaults
        row = conn2.execute("SELECT number, is_voided FROM tickets WHERE number = 1").fetchone()
        assert row == (1, 0)
        bal = conn2.execute("SELECT credit_balance FROM users WHERE id = 1").fetchone()[0]
        assert float(bal) == 0.0
    finally:
        conn2.close()
