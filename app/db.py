"""Engine / session helpers supporting PostgreSQL (asyncpg) and embedded SQLite (aiosqlite)."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy import event, inspect, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import Settings
from .models import Base, Counter

_SQLITE_LOCKS: WeakKeyDictionary[AsyncEngine, asyncio.Lock] = WeakKeyDictionary()


def is_sqlite_url(url: str) -> bool:
    return url.strip().lower().startswith("sqlite")


def make_engine(url: str) -> AsyncEngine:
    if is_sqlite_url(url):
        if ":///" in url and ":memory:" not in url:
            path_part = url.split(":///", 1)[1].split("?", 1)[0]
            parent = os.path.dirname(path_part)
            if parent:
                os.makedirs(parent, exist_ok=True)
        engine = create_async_engine(url, connect_args={"timeout": 30})

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragma(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine

    return create_async_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


class SQLiteSerializedSession(AsyncSession):
    """Serialize write transactions (`async with s.begin():`) on SQLite since it lacks FOR UPDATE."""

    _sqlite_lock: asyncio.Lock | None = None

    @asynccontextmanager
    async def begin(self):
        lock = self._sqlite_lock
        if lock is None:
            async with super().begin() as tx:
                yield tx
            return
        async with lock:
            async with super().begin() as tx:
                yield tx


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    if engine.dialect.name == "sqlite":
        lock = _SQLITE_LOCKS.get(engine)
        if lock is None:
            lock = asyncio.Lock()
            _SQLITE_LOCKS[engine] = lock

        class _BoundSQLiteSession(SQLiteSerializedSession):
            _sqlite_lock = lock

        return async_sessionmaker(engine, class_=_BoundSQLiteSession, expire_on_commit=False)

    return async_sessionmaker(engine, expire_on_commit=False)


def dialect_insert(session_or_conn, model):
    """Return dialect-specific INSERT (PostgreSQL or SQLite) supporting ON CONFLICT."""
    bind = getattr(session_or_conn, "bind", None) or session_or_conn
    name = getattr(getattr(bind, "dialect", None), "name", "postgresql")
    return sqlite_insert(model) if name == "sqlite" else pg_insert(model)


_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("users", "phone", "VARCHAR(64)"),
    ("users", "credit_balance", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
    ("payments", "overpayment_amount", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
    ("tickets", "is_voided", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def _apply_schema_migrations(sync_conn) -> None:
    """Safely add new columns to existing SQLite or PostgreSQL databases on upgrade."""
    insp = inspect(sync_conn)
    existing_tables = set(insp.get_table_names())
    for table, col, sql_def in _COLUMN_MIGRATIONS:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if col not in cols:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {sql_def}"))


async def init_db(engine: AsyncEngine, settings: Settings) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_schema_migrations)
        ins = dialect_insert(conn, Counter)
        await conn.execute(
            ins.values(id=1, next_number=settings.first_ticket_number).on_conflict_do_nothing()
        )
