"""End-to-end tests for embedded SQLite mode, Web Control Center API, multi-ticket allocation, and Excel exports."""
from __future__ import annotations

import io
from decimal import Decimal

import httpx
import openpyxl
import pytest

import draw as draw_lib
from app.config import Settings, load_desktop_settings
from app.extract import Extracted
from app.models import Payment, Ticket, User
from app.service import approve
from app.service_manager import BotServiceManager
from app.sms import add_credit
from app.web_ui import create_web_app
from app.worker import Worker


@pytest.fixture
async def web_env(tmp_path):
    data_dir = tmp_path / "lottery_data"
    data_dir.mkdir()
    settings = load_desktop_settings(data_dir)
    manager = BotServiceManager(data_dir=data_dir, settings=settings)
    await manager.init_database()
    app = create_web_app(manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, manager
    await manager.shutdown()


async def test_initial_status_is_unconfigured_with_no_hardcoded_bank(web_env):
    client, manager = web_env
    st = (await client.get("/api/status")).json()
    assert st["status"] == "unconfigured"
    assert st["configured"] is False
    assert "SQLite" in st["db_type"]
    cfg = (await client.get("/api/settings")).json()
    assert cfg["bot_token"] == ""
    assert cfg["cbe_account"] == ""


async def test_save_settings_persists_and_initializes_config(web_env):
    client, manager = web_env
    res = await client.post(
        "/api/settings",
        json={
            "campaign_name": "Client Lottery 2026",
            "bot_token": "999999:TEST_TOKEN_ABCDEFGHIJKLMNOP",
            "admin_ids": "123456789",
            "ticket_price": "100",
            "max_tickets_per_payment": 50,
            "first_ticket_number": 1,
            "ticket_width": 5,
            "campaign_unbounded": True,
            "cbe_account": "1000999888777",
            "cbe_account_name": "Client Org",
            "telebirr_number": "0911223344",
            "auto_build_instructions": True,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["status"]["status"] == "stopped"
    assert body["status"]["configured"] is True
    assert "1000999888777" in body["settings"]["payment_instructions"]
    assert "0911223344" in body["settings"]["payment_instructions"]

    reloaded = load_desktop_settings(manager.data_dir)
    assert reloaded.campaign_name == "Client Lottery 2026"
    assert reloaded.cbe_account == "1000999888777"
    assert reloaded.max_tickets_per_payment == 50


async def test_multi_ticket_allocation_dashboard_and_excel_export_sqlite(web_env):
    client, manager = web_env
    await client.post(
        "/api/settings",
        json={
            "campaign_name": "Multi-Ticket Test",
            "bot_token": "999999:TEST_TOKEN_ABCDEFGHIJKLMNOP",
            "admin_ids": "123456789",
            "ticket_price": "100",
            "max_tickets_per_payment": 50,
            "cbe_account": "1000999888777",
            "campaign_unbounded": True,
        },
    )

    sm = manager.sm
    # Simulate 3 participants sending 100 (1x), 200 (2x), 300 (3x), and 400 (4x) via Worker + credits
    images = {}

    class FakeOCR:
        def __init__(self, mapping):
            self.mapping = mapping

        def extract(self, data):
            return self.mapping[data]

    mapping = {}

    async def fetch(fid):
        return images[fid]

    sent = []

    async def notify(tg, txt):
        sent.append((tg, txt))

    worker = Worker(sm, manager.settings, FakeOCR(mapping), fetch, notify)

    async with sm() as s, s.begin():
        u1 = User(telegram_id=1001, username="abebe", full_name="Abebe Kebede")
        u2 = User(telegram_id=1002, username="selam", full_name="Selamawit Tadesse")
        u3 = User(telegram_id=1003, username="dawit", full_name="Dawit Mekonnen")
        s.add_all([u1, u2, u3])
        await s.flush()

        await add_credit(s, "REF200ETB001", Decimal("200.00"), "Abebe Kebede")
        await add_credit(s, "REF300ETB002", Decimal("300.00"), "Selamawit Tadesse")
        await add_credit(s, "REF400ETB003", Decimal("400.00"), "Dawit Mekonnen")

        p1 = Payment(user_id=u1.id, tg_file_id="f1", status="received")
        p2 = Payment(user_id=u2.id, tg_file_id="f2", status="received")
        p3 = Payment(user_id=u3.id, tg_file_id="f3", status="received")
        p_rev = Payment(user_id=u1.id, tg_file_id="f4", status="review", reject_reason="Odd 150 ETB", amount=Decimal("150.00"))
        s.add_all([p1, p2, p3, p_rev])
        await s.flush()

    from PIL import Image

    def mkimg(seed):
        im = Image.new("RGB", (20, 20), (seed, seed, seed))
        b = io.BytesIO()
        im.save(b, "PNG")
        return b.getvalue()

    images["f1"], images["f2"], images["f3"] = mkimg(1), mkimg(2), mkimg(3)
    mapping[images["f1"]] = Extracted(transaction_id="REF200ETB001", amount=Decimal("200.00"), sender="Abebe", provider="cbe")
    mapping[images["f2"]] = Extracted(transaction_id="REF300ETB002", amount=Decimal("300.00"), sender="Selamawit", provider="cbe")
    mapping[images["f3"]] = Extracted(transaction_id="REF400ETB003", amount=Decimal("400.00"), sender="Dawit", provider="telebirr")

    for _ in range(3):
        pid = await worker.claim_next()
        assert pid is not None
        await worker.process(pid)

    # Check Dashboard KPIs: 200 + 300 + 400 = 900 ETB, 2 + 3 + 4 = 9 tickets!
    dash = (await client.get("/api/dashboard")).json()
    assert dash["total_birr_received"] == 900.0
    assert dash["total_tickets_issued"] == 9
    assert dash["total_participants"] == 3
    assert dash["pending_review_count"] == 1
    assert dash["next_ticket_formatted"] == "#00010"

    # Check Holders API ("what numbers given to who")
    holders_resp = (await client.get("/api/holders?search=Selamawit")).json()
    assert len(holders_resp["holders"]) == 1
    selam = holders_resp["holders"][0]
    assert selam["full_name"] == "Selamawit Tadesse"
    assert selam["ticket_count"] == 3
    assert selam["total_birr"] == 300.0
    assert selam["ticket_numbers"] == [3, 4, 5]

    # Check Excel export (.xlsx)
    xlsx_resp = await client.get("/api/export/excel")
    assert xlsx_resp.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_resp.content))
    assert "Lottery Holders" in wb.sheetnames
    assert "Summary by Holder" in wb.sheetnames
    ws1 = wb["Lottery Holders"]
    rows1 = list(ws1.iter_rows(values_only=True))
    assert len(rows1) == 10  # header + 9 tickets
    assert "Full Name" in rows1[0]
    names_in_excel = {r[2] for r in rows1[1:]}
    assert names_in_excel == {"Abebe Kebede", "Selamawit Tadesse", "Dawit Mekonnen"}

    # Check Summary Report Excel
    rep_resp = await client.get("/api/export/report")
    assert rep_resp.status_code == 200
    wb_rep = openpyxl.load_workbook(io.BytesIO(rep_resp.content))
    assert "Financial Summary" in wb_rep.sheetnames

    # Check Draw CSV compatibility
    csv_resp = await client.get("/api/export/csv")
    assert csv_resp.status_code == 200
    csv_path = manager.data_dir / "test_tickets.csv"
    csv_path.write_bytes(csv_resp.content)
    loaded = draw_lib.load_tickets(str(csv_path))
    assert len(loaded) == 9

    # Check Commit & Draw via Web API
    com = (await client.post("/api/draw/commit")).json()
    assert com["ok"] is True and len(com["commitment"]) == 64
    dr = (await client.post("/api/draw/run", json={"entropy": "block-12345", "winners": 2, "one_per_person": True})).json()
    assert dr["ok"] is True and len(dr["winners"]) == 2
    assert dr["winners"][0]["full_name"] in {"Abebe Kebede", "Selamawit Tadesse", "Dawit Mekonnen"}

    # Check Review approval via Web API
    revs = (await client.get("/api/reviews")).json()["reviews"]
    assert len(revs) == 1
    app_res = (await client.post(f"/api/reviews/{revs[0]['id']}/approve", json={"tickets": 1})).json()
    assert app_res["ok"] is True and app_res["numbers"] == ["#00010"]
