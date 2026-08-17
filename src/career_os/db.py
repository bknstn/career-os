"""Lazy synchronous SQLAlchemy factories for the local PostgreSQL boundary."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from career_os.settings import Settings


class DatabaseConfigurationError(ValueError):
    """Raised when a persistence operation is requested without a database DSN."""


def database_url_from_settings(settings: Settings) -> str:
    """Return the configured PostgreSQL DSN at the persistence boundary."""
    if settings.database_url is None:
        raise DatabaseConfigurationError(
            "CAREER_OS_DATABASE_URL is required for persistence operations."
        )

    database_url = str(settings.database_url)
    if make_url(database_url).drivername != "postgresql+psycopg":
        raise DatabaseConfigurationError(
            "CAREER_OS_DATABASE_URL must use the synchronous postgresql+psycopg driver."
        )
    return database_url


def create_database_engine(settings: Settings) -> Engine:
    """Create, but do not connect, a synchronous PostgreSQL engine on demand."""
    return create_engine(
        database_url_from_settings(settings),
        echo=False,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=5,
        pool_timeout=30,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the typed session factory shared by future application services."""
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Yield one session and enforce commit, rollback, and close semantics."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
