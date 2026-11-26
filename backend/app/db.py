from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_Session = None


def get_engine():
    global _engine, _Session
    if _engine is None:
        st = get_settings()
        if st.is_sqlite:
            _engine = create_engine(st.db_url, connect_args={"check_same_thread": False, "timeout": 30})

            @event.listens_for(_engine, "connect")
            def _pragma(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        else:  # PostgreSQL
            _engine = create_engine(st.db_url, pool_pre_ping=True, pool_size=10, max_overflow=20)

        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope():
    get_engine()
    s: Session = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db(request: Request):
    """FastAPI bağımlılığı. Oturum request.state.db'ye de konur (bkz. api.CommitRoute)."""
    with session_scope() as s:
        request.state.db = s
        yield s


def migrate() -> None:
    """Alembic göçlerini son sürüme çıkarır (uygulama açılışında çağrılır)."""
    from alembic import command
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().db_url)
    command.upgrade(cfg, "head")
