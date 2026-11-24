import io
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from PIL import Image

from app.config import Settings
from app.db import init_db, make_engine, make_sessionmaker
from app.models import Base, Payment, User

TEST_DB = os.environ.get("TEST_DATABASE_URL")


@pytest_asyncio.fixture
async def db(tmp_path):
    """Fresh schema per test. Needs a real PostgreSQL (TEST_DATABASE_URL)."""
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    settings = Settings(ticket_price=Decimal("100"), storage_dir=str(tmp_path / "img"), credit_wait_minutes=20)
    engine = make_engine(TEST_DB)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db(engine, settings)
    yield make_sessionmaker(engine), settings
    await engine.dispose()


def png(seed: int) -> bytes:
    """A small, valid and *unique* image per seed."""
    img = Image.new("RGB", (32, 32), (seed % 256, (seed // 256) % 256, 7))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def make_payment(sm, telegram_id: int, image_seed: int, status="received") -> int:
    from sqlalchemy import select

    async with sm() as s, s.begin():
        u = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
        if u is None:
            u = User(telegram_id=telegram_id, username=f"user{telegram_id}", full_name=f"User {telegram_id}")
            s.add(u)
            await s.flush()
        p = Payment(user_id=u.id, tg_file_id=f"img-{image_seed}", status=status)
        s.add(p)
        await s.flush()
        return p.id
