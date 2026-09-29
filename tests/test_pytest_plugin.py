"""Tests for the pytest plugin, running example test suites with pytester."""

import pytest

CONFTEST = """
import psycopg
import pytest

SETUP_RUNS = []

@pytest.fixture(scope="session")
def pglite_setup(pglite_server):
    SETUP_RUNS.append(1)
    with psycopg.connect(pglite_server.dsn) as conn:
        conn.execute("CREATE TABLE items (id serial PRIMARY KEY, name text UNIQUE)")
        conn.execute("INSERT INTO items (name) VALUES ('from setup')")
"""


@pytest.fixture
def suite(pytester):
    pytester.makeconftest(CONFTEST)
    pytester.makeini("[pytest]\npglite_busy_timeout = 0.5\n")
    return pytester


def names(conn):
    return [n for (n,) in conn.execute("SELECT name FROM items ORDER BY id")]


def test_setup_runs_once_and_tests_are_rolled_back(suite):
    suite.makepyfile(
        """
        import conftest

        def names(conn):
            return [n for (n,) in conn.execute("SELECT name FROM items ORDER BY id")]

        def test_a(pglite_connection):
            pglite_connection.execute("INSERT INTO items (name) VALUES ('from a')")
            assert names(pglite_connection) == ["from setup", "from a"]

        def test_b(pglite_connection):
            assert names(pglite_connection) == ["from setup"]
            pglite_connection.execute("INSERT INTO items (name) VALUES ('from b')")

        def test_c(pglite_connection):
            assert names(pglite_connection) == ["from setup"]
            assert conftest.SETUP_RUNS == [1]
        """
    )
    suite.runpytest_subprocess().assert_outcomes(passed=3)


def test_code_under_test_can_use_savepoints_but_not_commit(suite):
    suite.makepyfile(
        """
        import psycopg
        import pytest

        def add_item(conn, name):
            # code under test managing its own transaction
            with conn.transaction():
                conn.execute("INSERT INTO items (name) VALUES (%s)", (name,))

        def test_savepoints(pglite_connection):
            add_item(pglite_connection, "x")
            with pytest.raises(psycopg.errors.UniqueViolation):
                add_item(pglite_connection, "x")
            # the failed savepoint doesn't abort the test's transaction
            assert pglite_connection.execute("SELECT count(*) FROM items").fetchone() == (2,)

        def test_commit_is_forbidden(pglite_connection):
            with pytest.raises(psycopg.ProgrammingError):
                pglite_connection.commit()

        def test_nothing_leaked(pglite_connection):
            assert pglite_connection.execute("SELECT count(*) FROM items").fetchone() == (1,)
        """
    )
    suite.runpytest_subprocess("-p", "no:randomly").assert_outcomes(passed=3)


def test_sqlalchemy_session(suite):
    suite.makepyfile(
        """
        import sqlalchemy as sa

        def create_item(session, name):
            # code under test that commits
            session.execute(sa.text("INSERT INTO items (name) VALUES (:n)"), {"n": name})
            session.commit()

        def test_commit_in_code_under_test(pglite_session):
            create_item(pglite_session, "committed")
            count = pglite_session.execute(sa.text("SELECT count(*) FROM items")).scalar()
            assert count == 2

        def test_rolled_back_anyway(pglite_session):
            count = pglite_session.execute(sa.text("SELECT count(*) FROM items")).scalar()
            assert count == 1
        """
    )
    suite.runpytest_subprocess("-p", "no:randomly").assert_outcomes(passed=2)


def test_other_connections_fail_with_a_hint(suite):
    suite.makepyfile(
        """
        import psycopg
        import pytest

        def test_own_connection(pglite, pglite_connection):
            pglite_connection.execute("SELECT 1")
            with pytest.raises(psycopg.OperationalError) as exc:
                psycopg.connect(pglite.dsn)  # the code under test opening its own
            assert "use the test's connection" in str(exc.value)
        """
    )
    suite.runpytest_subprocess().assert_outcomes(passed=1)
