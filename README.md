# pglite-wasm

In-memory Postgres for Python tests: [PGlite](https://github.com/electric-sql/pglite)'s Postgres 18, built as a standalone WASM module, running in-process on [wasmtime](https://github.com/bytecodealliance/wasmtime-py). No server to install, no Docker, no Node.

Each `PGlite` is a separate, throwaway database, served on a Unix socket so any client can connect: psycopg, asyncpg, SQLAlchemy, Django, ...

```python
import psycopg
from pglite_wasm import PGlite

with PGlite() as pg:
    with psycopg.connect(pg.dsn) as conn:
        conn.execute("SELECT 1")
```

`pg.dsn` is a libpq connection string. `pg.url("postgresql+psycopg")` is a URL, e.g. for SQLAlchemy.

## Limitations

- **One backend.** PGlite is a single Postgres backend, so connections take turns. A connection that is in a transaction keeps the backend until the transaction ends, and the others wait for it. If a connection waits longer than `busy_timeout` (30 s by default), it gets an error instead. Session state (`SET`, temp tables, prepared statements) is shared by all connections.
- **No extensions yet.** That includes `plpgsql`.
- **Timeouts never fire.** `statement_timeout`, `lock_timeout` and similar settings have no effect.

## Development

The build artifacts come from a pglite checkout. They are built with `pnpm wasm:build:standalone` (see `packages/pglite-standalone` there), and collected with:

```
PGLITE_REPO=../pglite scripts/fetch-artifacts.sh
uv run pytest
```
