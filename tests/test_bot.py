"""Handler wiring: real aiogram Dispatcher + real Postgres, with a fake Telegram HTTP session."""
from decimal import Decimal

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Update
from sqlalchemy import select

from app import messages as M
from app.bot import build_dispatcher, make_review_notifier
from app.models import Payment, Ticket, User
from app.sms import add_credit

from .conftest import make_payment

ADMIN, ALICE = 900, 111


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ in ("SendMessage", "SendPhoto", "SendDocument"):
            return Message.model_validate(
                {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}}, context={"bot": bot}
            )
        return True  # AnswerCallbackQuery, EditMessageCaption, ...

    def texts(self, chat_id=None):
        out = []
        for m in self.calls:
            if chat_id is not None and getattr(m, "chat_id", None) != chat_id:
                continue
            out.append(getattr(m, "text", None) or getattr(m, "caption", None) or "")
        return out


@pytest.fixture
def tg(db):
    sm, settings = db
    from dataclasses import replace

    settings = replace(settings, admin_ids=frozenset({ADMIN}), bot_token="123456:" + "A" * 35)
    session = FakeSession()
    bot = Bot(settings.bot_token, session=session)
    dp = build_dispatcher(sm, settings)

    class H:
        pass

    h = H()
    h.sm, h.settings, h.session, h.bot, h.dp = sm, settings, session, bot, dp

    async def send(user_id, text=None, photo=False, callback=None, doc_mime=None):
        frm = {"id": user_id, "is_bot": False, "first_name": f"U{user_id}", "username": f"u{user_id}"}
        msg = {"message_id": 5, "date": 0, "chat": {"id": user_id, "type": "private"}, "from": frm}
        if text:
            msg["text"] = text
            if text.startswith("/"):
                n = len(text.split()[0])
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": n}]
        if photo:
            msg["photo"] = [{"file_id": "FILE-1", "file_unique_id": "u1", "width": 10, "height": 10}]
        if doc_mime:
            msg["document"] = {"file_id": "DOC-1", "file_unique_id": "d1", "mime_type": doc_mime}
        upd = {"update_id": 1}
        if callback:
            msg["photo"] = [{"file_id": "FILE-1", "file_unique_id": "u1", "width": 10, "height": 10}]
            msg["caption"] = "card"
            upd["callback_query"] = {"id": "cb", "from": frm, "chat_instance": "ci", "data": callback, "message": msg}
        else:
            upd["message"] = msg
        await dp.feed_update(bot, Update.model_validate(upd, context={"bot": bot}))

    h.send = send
    return h


async def test_start_shows_price_and_instructions(tg):
    await tg.send(ALICE, "/start")
    out = tg.session.texts(ALICE)[0]
    assert "Abel Online Lottery" in out
    assert "Ticket price: 100 ETB" in out


async def test_screenshot_creates_queued_payment_and_acknowledges(tg):
    await tg.send(ALICE, photo=True)
    assert tg.session.texts(ALICE) == [M.RECEIVED]
    async with tg.sm() as s:
        p = (await s.execute(select(Payment))).scalar_one()
        assert p.status == "received" and p.tg_file_id == "FILE-1"
        assert (await s.get(User, p.user_id)).telegram_id == ALICE


async def test_image_document_is_accepted_and_plain_text_gets_a_hint(tg):
    await tg.send(ALICE, doc_mime="image/png")
    await tg.send(ALICE, "hello?")
    assert tg.session.texts(ALICE) == [M.RECEIVED, M.SEND_SCREENSHOT]
    await tg.send(ALICE, doc_mime="application/pdf")  # PDF receipts are now accepted
    assert tg.session.texts(ALICE)[-1] == M.RECEIVED
    await tg.send(ALICE, doc_mime="application/zip")  # non-image/non-pdf -> ignored
    assert len(tg.session.calls) == 3


async def test_open_payment_limit(tg):
    for _ in range(4):
        await tg.send(ALICE, photo=True)
    texts = tg.session.texts(ALICE)
    assert texts[:3] == [M.RECEIVED] * 3
    assert texts[3] == M.TOO_MANY_OPEN.format(n=3)


async def test_mytickets(tg):
    await tg.send(ALICE, "/mytickets")
    pid = await make_payment(tg.sm, ALICE, 1, status="verifying")
    from app.service import approve

    async with tg.sm() as s, s.begin():
        await approve(s, pid, 2, "test")
    await tg.send(ALICE, "/mytickets")
    assert tg.session.texts(ALICE) == [M.NO_TICKETS, M.MY_TICKETS.format(n=2, numbers="#00001, #00002")]


async def test_admin_commands_are_not_available_to_normal_users(tg):
    await tg.send(ALICE, "/stats")
    await tg.send(ALICE, "/export")
    await tg.send(ALICE, "/pending")
    assert tg.session.calls == []


async def test_admin_review_flow_approve(tg):
    # A payment the worker escalated: credit exists but amount on the screenshot was different.
    pid = await make_payment(tg.sm, ALICE, 1, status="review")
    async with tg.sm() as s, s.begin():
        await add_credit(s, "REVIEW000001", Decimal("200"), "Abebe")
        p = await s.get(Payment, pid)
        p.transaction_id, p.reject_reason, p.extracted = "REVIEW000001", "Screenshot shows 1000 but 200 was received", {"amount": "1000"}

    await make_review_notifier(tg.bot, tg.sm, tg.settings)(pid)
    card = tg.session.calls[-1]
    assert card.chat_id == ADMIN and "Review #" in card.caption
    buttons = [b.callback_data for row in card.reply_markup.inline_keyboard for b in row]
    assert buttons == [f"rv:a:{pid}:2", f"rv:r:{pid}"]  # suggested count comes from the bank credit (200/100)

    await tg.send(ADMIN, callback=buttons[0])
    async with tg.sm() as s:
        assert (await s.get(Payment, pid)).status == "approved"
        assert (await s.execute(select(Ticket.number).order_by(Ticket.number))).scalars().all() == [1, 2]
    assert tg.session.texts(ALICE) == [M.APPROVED.format(numbers="#00001, #00002")]
    assert "ሰናይ ዕድል" in tg.session.texts(ALICE)[0]

    # Second tap on the same (now stale) button must not issue anything new.
    await tg.send(ADMIN, callback=buttons[0])
    async with tg.sm() as s:
        assert len((await s.execute(select(Ticket))).scalars().all()) == 2


async def test_admin_review_flow_reject_then_cannot_approve_twice(tg):
    pid = await make_payment(tg.sm, ALICE, 1, status="review")
    await tg.send(ADMIN, callback=f"rv:r:{pid}")
    assert tg.session.texts(ALICE) == [M.ADMIN_REJECTED]
    await tg.send(ADMIN, callback=f"rv:a:{pid}:1")  # rejected payments can't be approved by a stale button
    async with tg.sm() as s:
        assert (await s.get(Payment, pid)).status == "rejected"
        assert (await s.execute(select(Ticket))).scalars().all() == []


async def test_non_admin_cannot_press_review_buttons(tg):
    pid = await make_payment(tg.sm, ALICE, 1, status="review")
    await tg.send(ALICE, callback=f"rv:a:{pid}:5")
    async with tg.sm() as s:
        assert (await s.get(Payment, pid)).status == "review"


async def test_stats_find_and_exports(tg):
    from app.service import approve

    pid = await make_payment(tg.sm, ALICE, 1, status="verifying")
    async with tg.sm() as s, s.begin():
        await approve(s, pid, 3, "test")

    await tg.send(ADMIN, "/stats")
    stats = tg.session.texts(ADMIN)[-1]
    assert "Tickets issued: 3" in stats and "Participants: 1" in stats and "#00004" in stats

    await tg.send(ADMIN, "/find 2")
    assert "u111" in tg.session.texts(ADMIN)[-1] or "User 111" in tg.session.texts(ADMIN)[-1]

    await tg.send(ADMIN, "/export")
    await tg.send(ADMIN, "/export public")
    docs = [m for m in tg.session.calls if type(m).__name__ == "SendDocument"]
    assert len(docs) == 3
    xlsx_bytes = docs[0].document.data
    full = docs[1].document.data.decode("utf-8-sig")
    public = docs[2].document.data.decode("utf-8-sig")
    assert xlsx_bytes[:2] == b"PK"  # valid .xlsx zip container
    assert "full_name" in full.splitlines()[0] and "111" in full and "User 111" in full
    assert public.splitlines()[0] == "number,holder,name,handle" and "111" not in public and "u111" not in public
    holders = {line.split(",")[1] for line in public.splitlines()[1:]}
    assert len(holders) == 1  # same person -> same anonymous holder id
