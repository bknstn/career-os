"""Tests for lazy database infrastructure without a PostgreSQL server."""

from __future__ import annotations

import importlib

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

import career_os.db as db
from career_os.db import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
    database_url_from_settings,
    session_scope,
)
from career_os.settings import Settings


def test_database_engine_is_configured_without_connecting() -> None:
    engine = create_database_engine(_settings_with_database_url())

    try:
        assert engine.url.render_as_string(hide_password=False) == (
            "postgresql+psycopg://career_os:dev-only@localhost:5432/career_os"
        )
        assert engine.pool._pre_ping is True
    finally:
        engine.dispose()


def test_database_url_is_required_at_the_persistence_boundary() -> None:
    with pytest.raises(DatabaseConfigurationError, match="CAREER_OS_DATABASE_URL"):
        database_url_from_settings(Settings())


def test_database_url_requires_the_synchronous_psycopg_driver() -> None:
    settings = Settings(database_url="postgresql://career_os:dev-only@localhost:5432/career_os")

    with pytest.raises(DatabaseConfigurationError, match="postgresql\\+psycopg"):
        database_url_from_settings(settings)


def test_session_scope_commits_and_closes() -> None:
    session = _FakeSession()

    with session_scope(lambda: session) as current_session:
        assert current_session is session

    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True


def test_session_scope_rolls_back_and_closes_on_error() -> None:
    session = _FakeSession()

    with pytest.raises(RuntimeError, match="expected failure"), session_scope(lambda: session):
        raise RuntimeError("expected failure")

    assert session.committed is False
    assert session.rolled_back is True
    assert session.closed is True


def test_session_scope_rolls_back_when_commit_fails() -> None:
    session = _FakeSession(commit_error=RuntimeError("commit failure"))

    with pytest.raises(RuntimeError, match="commit failure"), session_scope(lambda: session):
        pass

    assert session.rolled_back is True
    assert session.closed is True


def test_importing_database_module_does_not_create_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("database engine must not be created during import")

    with monkeypatch.context() as patch:
        patch.setattr(sqlalchemy, "create_engine", fail_if_called)
        importlib.reload(db)

    importlib.reload(db)


def test_session_factory_is_typed_and_bound_to_the_engine() -> None:
    engine = create_database_engine(_settings_with_database_url())

    try:
        factory = create_session_factory(engine)
        assert isinstance(factory(), Session)
    finally:
        engine.dispose()


def _settings_with_database_url() -> Settings:
    return Settings(database_url="postgresql+psycopg://career_os:dev-only@localhost:5432/career_os")


class _FakeSession:
    def __init__(self, commit_error: Exception | None = None) -> None:
        self.closed = False
        self.committed = False
        self.commit_error = commit_error
        self.rolled_back = False

    def close(self) -> None:
        self.closed = True

    def commit(self) -> None:
        self.committed = True
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rolled_back = True
