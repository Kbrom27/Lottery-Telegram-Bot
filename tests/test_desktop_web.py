"""End-to-end tests for embedded SQLite mode, Web Control Center API, multi-ticket allocation, and Excel exports."""
from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
import openpyxl
import pytest
from sqlalchemy import select

import desktop_launcher
import draw as draw_lib
from app.config import Settings, load_desktop_settings
from app.extract import Extracted, _try_cbe_url
from app.models import BankCredit, Counter, Payment, Ticket, User
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
    assert st["campaign_name"] == "Abel Online Lottery"
    assert "SQLite" in st["db_type"]
    cfg = (await client.get("/api/settings")).json()
    assert cfg["campaign_name"] == "Abel Online Lottery"
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


async def test_saving_first_ticket_number_syncs_counter_only_when_no_tickets_issued(web_env):
    client, manager = web_env
    res = await client.post(
        "/api/settings",
        json={
            "campaign_name": "Custom Start Test",
            "bot_token": "999999:TEST_TOKEN_ABCDEFGHIJKLMNOP",
            "admin_ids": "123456789",
            "first_ticket_number": 500,
            "ticket_width": 5,
            "campaign_unbounded": True,
        },
    )
    assert res.status_code == 200
    dash = (await client.get("/api/dashboard")).json()
    assert dash["next_ticket_number"] == 500
    assert dash["next_ticket_formatted"] == "#00500"

    # Issue one ticket via DB/service, then change first_ticket_number again: Counter must stay at 501.
    async with manager.sm() as s, s.begin():
        u = User(telegram_id=88001, full_name="First Holder", username="first")
        s.add(u)
        await s.flush()
        p = Payment(user_id=u.id, tg_file_id="f500", image_sha256="b" * 64, status="review")
        s.add(p)
        await s.flush()
        pid = p.id
    async with manager.sm() as s, s.begin():
        nums = await approve(s, pid, 1, "test")
    assert nums == [500]

    res2 = await client.post(
        "/api/settings",
        json={
            "campaign_name": "Custom Start Test",
            "bot_token": "999999:TEST_TOKEN_ABCDEFGHIJKLMNOP",
            "admin_ids": "123456789",
            "first_ticket_number": 900,
            "ticket_width": 5,
            "campaign_unbounded": True,
        },
    )
    assert res2.status_code == 200
    async with manager.sm() as s:
        ctr = await s.get(Counter, 1)
        assert ctr.next_number == 501


async def test_cbe_qr_verified_credit_preserves_received_at_for_campaign_window(tmp_path):
    mock_tx = {
        "id": "FT25001OLDTX",
        "status": "COMPLETED",
        "amountCredited": "100.00",
        "debitAccountHolder": "Old Sender",
        "creditAccountNo": "1000999888777",
        "dateTimes": ["2025-01-01T10:00:00Z"],
    }
    with patch("app.extract.fetch_cbe_transaction_detail", return_value=mock_tx):
        ex = _try_cbe_url("https://mbreciept.cbe.com.et/tok123", source="QR")
    assert ex is not None
    assert ex.verified_bank_credit["received_at"] == "2025-01-01T10:00:00Z"

    data_dir = tmp_path / "window_data"
    data_dir.mkdir()
    settings = Settings(
        campaign_name="Bounded Campaign",
        bot_token="999999:TEST_TOKEN_ABCDEFGHIJKLMNOP",
        admin_ids=frozenset({123456789}),
        database_url=f"sqlite+aiosqlite:///{data_dir / 'window.db'}",
        storage_dir=str(data_dir / "receipts"),
        cbe_account="1000999888777",
        campaign_unbounded=False,
        campaign_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        campaign_end=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    )
    manager = BotServiceManager(data_dir=data_dir, settings=settings)
    await manager.init_database()
    try:
        from PIL import Image

        buf1 = io.BytesIO()
        Image.new("RGB", (64, 64), (255, 255, 255)).save(buf1, format="PNG")
        buf2 = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 10, 10)).save(buf2, format="PNG")
        img_map = {"old_qr": buf1.getvalue(), "good_qr": buf2.getvalue()}

        class FakeQRExtractor:
            def __init__(self, extracted):
                self.extracted = extracted

            def extract(self, _image: bytes) -> Extracted:
                return self.extracted

        async def fetch_file(fid: str) -> bytes:
            return img_map[fid]

        notifications = []

        async def notify(tg_id: int, text: str) -> None:
            notifications.append((tg_id, text))

        async with manager.sm() as s, s.begin():
            u = User(telegram_id=99001, full_name="Window Tester", username="wintester")
            s.add(u)
            await s.flush()
            p_old = Payment(user_id=u.id, tg_file_id="old_qr", image_sha256="c" * 64, status="received")
            s.add(p_old)
            await s.flush()
            p_old_id = p_old.id

        w_old = Worker(
            manager.sm,
            settings,
            FakeQRExtractor(ex),
            fetch_file,
            notify,
        )
        claimed_old = await w_old.claim_next()
        assert claimed_old == p_old_id
        await w_old.process(claimed_old)

        async with manager.sm() as s:
            pay = await s.get(Payment, p_old_id)
            cred = (await s.execute(select(BankCredit).where(BankCredit.reference == "FT25001OLDTX"))).scalar_one()
            assert pay.status == "rejected"
            assert cred.received_at == datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

        # Now send an in-window CBE QR receipt and confirm it is approved.
        mock_in_window = dict(mock_tx, id="FT26060GOOD1", dateTimes=["2026-06-01T12:00:00Z"])
        with patch("app.extract.fetch_cbe_transaction_detail", return_value=mock_in_window):
            ex_good = _try_cbe_url("https://mbreciept.cbe.com.et/tok456", source="QR")

        async with manager.sm() as s, s.begin():
            u = (await s.execute(select(User).where(User.telegram_id == 99001))).scalar_one()
            p_good = Payment(user_id=u.id, tg_file_id="good_qr", image_sha256="d" * 64, status="received")
            s.add(p_good)
            await s.flush()
            p_good_id = p_good.id

        w_good = Worker(
            manager.sm,
            settings,
            FakeQRExtractor(ex_good),
            fetch_file,
            notify,
        )
        claimed_good = await w_good.claim_next()
        assert claimed_good == p_good_id
        await w_good.process(claimed_good)

        async with manager.sm() as s:
            pay_good = await s.get(Payment, p_good_id)
            assert pay_good.status == "approved"
    finally:
        await manager.shutdown()


def test_find_existing_instance_scans_port_range():
    def fake_in_use(port: int, _host: str = "127.0.0.1") -> bool:
        return port in (8000, 8002)

    def fake_check(port: int) -> bool:
        return port == 8002

    with patch.object(desktop_launcher, "_port_in_use", side_effect=fake_in_use), patch.object(
        desktop_launcher, "_check_existing_instance", side_effect=fake_check
    ):
        assert desktop_launcher._find_existing_instance(8000) == 8002
        assert desktop_launcher._find_existing_instance(8010) is None


def test_desktop_launcher_parse_args():
    ns = desktop_launcher.parse_args(
        ["--host", "0.0.0.0", "--port", "8090", "--no-browser", "--no-tray", "--data-dir", "./custom_data"]
    )
    assert ns.host == "0.0.0.0"
    assert ns.port == 8090
    assert ns.no_browser is True
    assert ns.no_tray is True
    assert ns.data_dir == "./custom_data"


async def test_upgrade_preserves_existing_settings_and_issued_tickets(tmp_path):
    """Simulate an app upgrade re-initializing over an existing lottery_data directory."""
    data_dir = tmp_path / "lottery_data"
    data_dir.mkdir()

    # v1 manager: user configures campaign and issues ticket #00001
    m1 = BotServiceManager(data_dir=data_dir, settings=load_desktop_settings(data_dir))
    await m1.init_database()
    app1 = create_web_app(m1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://test") as c1:
        await c1.post(
            "/api/settings",
            json={
                "campaign_name": "Upgrade Persist Lottery",
                "bot_token": "999999:UPGRADE_TOKEN_ABCDEFGHIJKLMN",
                "admin_ids": "9262763122",
                "ticket_price": "100",
                "first_ticket_number": 1,
                "cbe_account": "1000123456789",
                "campaign_unbounded": True,
            },
        )
    async with m1.sm() as s, s.begin():
        u = User(telegram_id=9262763122, full_name="Abel Equb", username="abel")
        s.add(u)
        await s.flush()
        p = Payment(user_id=u.id, tg_file_id="f_up", image_sha256="e" * 64, status="review")
        s.add(p)
        await s.flush()
        pid = p.id
    async with m1.sm() as s, s.begin():
        nums = await approve(s, pid, 2, "test")
    assert nums == [1, 2]
    await m1.shutdown()

    # v2 manager after upgrade: starts fresh using the exact same lottery_data directory
    m2 = BotServiceManager(data_dir=data_dir, settings=load_desktop_settings(data_dir))
    await m2.init_database()
    try:
        app2 = create_web_app(m2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://test") as c2:
            st = (await c2.get("/api/status")).json()
            assert st["configured"] is True
            assert st["campaign_name"] == "Upgrade Persist Lottery"
            cfg = (await c2.get("/api/settings")).json()
            assert cfg["admin_ids"] == "9262763122"
            assert cfg["cbe_account"] == "1000123456789"
            dash = (await c2.get("/api/dashboard")).json()
            assert dash["total_tickets_issued"] == 2
            assert dash["next_ticket_number"] == 3
            assert dash["total_birr_received"] == 200.0
    finally:
        await m2.shutdown()
