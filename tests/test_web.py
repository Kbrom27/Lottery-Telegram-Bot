"""The /sms webhook (lifespan is not started; we inject the sessionmaker directly)."""
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from app.main import create_app
from app.models import BankCredit

SMS = "Dear customer, you have received ETB 100.00 from Abebe Kebede on 16/11/2026. Transaction number is DKL4A7B9Q2."


@pytest.fixture
async def client(db):
    sm, settings = db
    app = create_app(replace(settings, sms_secret="s3cret"))
    app.state.sm = sm
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        c.sm = sm
        yield c


async def test_auth_required(client):
    assert (await client.post("/sms", json={"text": SMS})).status_code == 401
    assert (await client.post("/sms", json={"text": SMS}, headers={"X-Secret": "wrong"})).status_code == 401


async def test_create_duplicate_and_ignored(client):
    h = {"X-Secret": "s3cret"}
    assert (await client.post("/sms", json={"text": SMS}, headers=h)).json() == {"status": "created"}
    assert (await client.post("/sms", json={"message": SMS}, headers=h)).json() == {"status": "duplicate"}
    assert (await client.post("/sms?key=s3cret", content=SMS.encode())).json() == {"status": "duplicate"}  # raw text + query key
    assert (await client.post("/sms", json={"text": "Your OTP is 123456"}, headers=h)).json() == {"status": "ignored"}
    assert (await client.post("/sms", json={}, headers=h)).status_code == 400
    async with client.sm() as s:
        credits = (await s.execute(select(BankCredit))).scalars().all()
    assert len(credits) == 1 and credits[0].reference == "DKL4A7B9Q2" and str(credits[0].amount) == "100.00"


async def test_healthz(client):
    assert (await client.get("/healthz")).json() == {"ok": True}
