"""SQLite engine + session helpers.

We use WAL mode and ``check_same_thread=False`` because the background worker
threads share the engine with the FastAPI request threads. Each unit of work
opens its own short-lived ``Session``.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

_engine: Engine | None = None


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - glue
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            settings.db_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db() -> None:
    # Import models so SQLModel registers the tables before create_all.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(get_engine())
    _run_lightweight_migrations()


def _run_lightweight_migrations() -> None:
    """Add columns introduced after a DB was first created.

    SQLModel's create_all never alters existing tables, so we add any missing
    columns by hand. Each entry is (table, column, column DDL with default).
    """
    additions = [
        ("job", "source_deleted", "BOOLEAN NOT NULL DEFAULT 0"),
        ("job", "vmaf_score", "FLOAT"),
        ("batch", "compute_vmaf", "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    with session_scope() as session:
        for table, column, ddl in additions:
            existing = {
                row[1]
                for row in session.execute(text(f"PRAGMA table_info({table})")).all()
            }
            if column not in existing:
                session.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                )


@contextmanager
def session_scope() -> Iterator[Session]:
    # expire_on_commit=False keeps attribute values loaded after commit so the
    # detached ORM objects we hand to Jinja templates stay readable.
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
