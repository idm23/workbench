"""Engine and session management."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from workbench.config import database_url, ensure_data_dir


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    """Apply per-connection pragmas.

    SQLite ignores foreign keys unless asked, which would silently break the
    cascade delete from users to projects. WAL lets reads proceed during a
    write, and is also what makes `sqlite3 .backup` safe on a live database.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def make_engine(url: str) -> Engine:
    """Build an engine with the SQLite pragmas applied.

    Separate from get_engine() so tests can point at a temporary database and
    still exercise the same connection setup the application uses.
    """
    engine = create_engine(url)
    event.listen(engine, "connect", _configure_sqlite)
    return engine


@cache
def get_engine() -> Engine:
    """The process-wide engine, built once on first use.

    Memoised via functools.cache rather than a module-level variable so there
    is no mutable state at import time and no reassignment to manage.
    """
    ensure_data_dir()
    return make_engine(database_url())


@cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """One session per request, closed afterwards.

    A plain generator with no web framework in sight — FastAPI wraps it in
    `Depends` at the call site. It lives here rather than beside the routes
    because there are two sets of routes now, the HTML forms and the JSON API,
    and they must not each open sessions their own way.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
