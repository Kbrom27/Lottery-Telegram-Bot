"""Database schema.

Payment lifecycle:  received -> verifying -> approved | rejected | review
                    (verifying -> received again while we wait for the bank SMS)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, TypeDecorator, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """DateTime(timezone=True) that ensures UTC tzinfo even on SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(256))
    phone: Mapped[str | None] = mapped_column(String(64), default=None)
    credit_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tg_file_id: Mapped[str] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(Text)
    image_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    transaction_id: Mapped[str | None] = mapped_column(String(40))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    overpayment_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    provider: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="received")
    # Shown to the user when status == "rejected"; internal note when status == "review".
    reject_reason: Mapped[str | None] = mapped_column(Text)
    extracted: Mapped[dict | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    credit_id: Mapped[int | None] = mapped_column(Integer)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # A transaction reference can back at most ONE approved payment.
        Index(
            "uq_payments_txid_approved",
            "transaction_id",
            unique=True,
            postgresql_where=text("status = 'approved'"),
            sqlite_where=text("status = 'approved'"),
        ),
        Index("ix_payments_queue", "status", "next_attempt_at"),
    )


class Ticket(Base):
    __tablename__ = "tickets"
    number: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    is_voided: Mapped[bool] = mapped_column(Boolean, default=False)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Counter(Base):
    """Single row (id=1). Locked FOR UPDATE to hand out gap-free ticket numbers."""

    __tablename__ = "counters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_number: Mapped[int] = mapped_column(BigInteger)


class BankCredit(Base):
    """Money that actually arrived in the receiving account (parsed from its SMS / statement).

    This is the source of truth. A screenshot is only a claim: "this reference is mine".
    """

    __tablename__ = "bank_credits"
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    reference_canon: Mapped[str] = mapped_column(String(40), index=True)  # O/0, I/1... folded
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    sender: Mapped[str | None] = mapped_column(String(256))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    raw_text: Mapped[str | None] = mapped_column(Text)
    claimed_by_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    payment_id: Mapped[int | None] = mapped_column(Integer, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)
