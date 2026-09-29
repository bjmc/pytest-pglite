# pytest-pglite

In-memory Postgres for Python tests: [PGlite](https://github.com/electric-sql/pglite)'s Postgres 18, built as a standalone WASM module, running in-process on [wasmtime](https://github.com/bytecodealliance/wasmtime-py). No server to install, no Docker, no Node.

Each `PGlite` is a separate, throwaway database, served on a Unix socket so any client can connect: psycopg, asyncpg, SQLAlchemy, Django, ...

```python
import psycopg
from pytest_pglite import PGlite

with PGlite() as pg:
    with psycopg.connect(pg.dsn) as conn:
        conn.execute("SELECT 1")
```

`pg.dsn` is a libpq connection string. `pg.url("postgresql+psycopg")` is a URL, e.g. for SQLAlchemy.

## pytest plugin

Installing the package registers a pytest plugin. One database serves the whole test session. Set it up once (migrations, shared data) by overriding `pglite_setup`:

```python
# conftest.py
@pytest.fixture(scope="session")
def pglite_setup(pglite_server):
    with psycopg.connect(pglite_server.dsn) as conn:
        conn.execute(open("schema.sql").read())
```

Tests then get a connection or session inside a transaction that is rolled back afterwards:

| Fixture | Scope | |
| --- | --- | --- |
| `pglite` | session | The database, set up. `.dsn`, `.url()` |
| `pglite_connection` | test | psycopg connection, rolled back after the test. `conn.transaction()` blocks become savepoints; `conn.commit()` raises. |
| `pglite_session` | test | SQLAlchemy ORM session, rolled back after the test. `session.commit()` only releases a savepoint. |
| `pglite_sqlalchemy_connection` | test | The SQLAlchemy connection under `pglite_session`, to bind your own sessions to (with `join_transaction_mode="create_savepoint"`). |
| `pglite_engine` | session | SQLAlchemy engine |
| `pglite_server` | session | The database before `pglite_setup`, for use in it |

**The code under test must use the test's connection or session**, e.g. by passing it in or overriding your app's session factory. PGlite is a single Postgres backend: while the test's transaction is open, any other connection has to wait, and fails after `pglite_busy_timeout` seconds (5 by default) with an error that says so.

Options (`pytest.ini`/`pyproject.toml`): `pglite_busy_timeout`, and `pglite_log` to write the Postgres log to a file.

## Limitations

- **One backend, one database.** PGlite is a single Postgres backend, so connections take turns, and only the `postgres` database can be connected to. A connection that is in a transaction keeps the backend until the transaction ends, and the others wait for it. If a connection waits longer than `busy_timeout` (30 s by default), it gets an error instead. Session state (`SET`, temp tables, prepared statements) is shared by all connections.
- **No extensions yet.** That includes `plpgsql`.
- **Timeouts never fire.** `statement_timeout`, `lock_timeout` and similar settings have no effect.

## Development

The build artifacts come from a pglite checkout. They are built with `postgres-pglite/build-pglite-standalone.sh` there (see "Standalone build" in `postgres-pglite/README-PGLITE-DEV.md`), and collected into `src/pytest_pglite/_artifacts/` with:

```
PGLITE_REPO=../pglite scripts/fetch-artifacts.sh
uv run pytest
```

`fetch-artifacts.sh` also records where the artifacts came from in `_artifacts/BUILD_INFO`.

CI (`.github/workflows/ci.yml`) builds the artifacts from the pglite commit pinned in `pglite.env` (cached per commit, as the WASM build takes a while), then runs the tests on Linux and macOS and builds the package. By default the pglite repository is `<owner of this repository>/pglite`. To package a newer pglite build, update `PGLITE_REF`.

## Building

```
scripts/fetch-artifacts.sh   # first: the wheel is only as good as _artifacts/
uv build
```

This produces a pure-Python `py3-none-any` wheel (the WASM module is platform independent; `wasmtime` provides the platform-specific runtime) and an sdist that includes the artifacts, so neither needs Node or the WASM toolchain to install.

## License

[PostgreSQL License](LICENSE). The wheel also includes PGlite (Apache-2.0) and PostgreSQL (PostgreSQL License), see `licenses/`.
