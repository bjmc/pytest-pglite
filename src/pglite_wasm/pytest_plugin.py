"""pytest plugin: an in-memory Postgres for the test session, with per-test rollback.

One PGlite database serves the whole session. Put schema migrations and shared
fixture data in a ``pglite_setup`` fixture, which runs once:

    @pytest.fixture(scope="session")
    def pglite_setup(pglite_server):
        with psycopg.connect(pglite_server.dsn) as conn:
            conn.execute(SCHEMA)

Each test that uses ``pglite_connection`` (psycopg) or ``pglite_session``
(SQLAlchemy) runs inside a transaction that is rolled back afterwards, so tests
don't see each other's changes. The code under test has to use that
connection or session: PGlite is a single Postgres backend, so while the test's
transaction is open, any other connection has to wait (and fails after
``pglite_busy_timeout`` seconds, with a hint pointing here).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from . import PGlite, Runtime

if TYPE_CHECKING:
    import psycopg
    import sqlalchemy
    import sqlalchemy.orm


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "pglite_busy_timeout",
        "Seconds a connection waits for the PGlite backend while another connection "
        "is in a transaction, before failing",
        default="5",
    )
    parser.addini(
        "pglite_log",
        "File to write the PGlite (Postgres) server log to",
        default="",
    )


@pytest.fixture(scope="session")
def pglite_runtime() -> Runtime:
    """The compiled PGlite module and filesystem images."""
    return Runtime()


@pytest.fixture(scope="session")
def pglite_server(pytestconfig: pytest.Config, pglite_runtime: Runtime) -> Iterator[PGlite]:
    """The session's PGlite database, before pglite_setup. Use it in pglite_setup."""
    with PGlite(
        runtime=pglite_runtime,
        busy_timeout=float(pytestconfig.getini("pglite_busy_timeout")),
        log_path=pytestconfig.getini("pglite_log") or None,
    ) as pg:
        yield pg


@pytest.fixture(scope="session")
def pglite_setup(pglite_server: PGlite) -> None:
    """Override to set up the database once per session (migrations, shared data)."""


@pytest.fixture(scope="session")
def pglite(pglite_server: PGlite, pglite_setup: None) -> PGlite:
    """The session's PGlite database, set up. Has .dsn and .url() for connecting."""
    return pglite_server


@pytest.fixture
def pglite_connection(pglite: PGlite) -> Iterator[psycopg.Connection]:
    """A psycopg connection whose changes are rolled back after the test.

    The test runs inside a transaction (psycopg's ``Transaction`` with
    force_rollback): ``conn.transaction()`` blocks in the code under test become
    savepoints, while ``conn.commit()`` raises.
    """
    import psycopg

    with psycopg.connect(pglite.dsn) as conn:
        with conn.transaction(force_rollback=True):
            yield conn


@pytest.fixture(scope="session")
def pglite_engine(pglite: PGlite) -> Iterator[sqlalchemy.Engine]:
    """A SQLAlchemy engine for the session's database (psycopg driver)."""
    import sqlalchemy

    engine = sqlalchemy.create_engine(pglite.url("postgresql+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def pglite_sqlalchemy_connection(
    pglite_engine: sqlalchemy.Engine,
) -> Iterator[sqlalchemy.Connection]:
    """A SQLAlchemy connection in a transaction that is rolled back after the test.

    Bind the code under test's sessions to it with
    ``join_transaction_mode="create_savepoint"``, as pglite_session does.
    """
    with pglite_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def pglite_session(
    pglite_sqlalchemy_connection: sqlalchemy.Connection,
) -> Iterator[sqlalchemy.orm.Session]:
    """A SQLAlchemy ORM session whose changes are rolled back after the test.

    ``session.commit()`` in the code under test only releases a savepoint
    (SQLAlchemy's "joining a session into an external transaction" recipe).
    """
    from sqlalchemy.orm import Session

    with Session(
        bind=pglite_sqlalchemy_connection, join_transaction_mode="create_savepoint"
    ) as session:
        yield session
