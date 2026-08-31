"""Engine/session management. Works with Postgres in production and SQLite in tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from xauusd.database.models import Base


def make_engine(url: str, echo: bool = False, pool_size: int = 5) -> Engine:
    kwargs: dict[str, object] = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        # Ensure the parent directory exists for file-backed SQLite.
        if ":memory:" not in url:
            path = url.split("///", 1)[-1]
            if path:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = pool_size
        kwargs["pool_pre_ping"] = True
    engine = create_engine(url, **kwargs)  # type: ignore[arg-type]

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)


class Database:
    def __init__(self, url: str, echo: bool = False, pool_size: int = 5) -> None:
        self.url = url
        self.engine = make_engine(url, echo, pool_size)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def dispose(self) -> None:
        self.engine.dispose()
