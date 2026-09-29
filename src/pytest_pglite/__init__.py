"""In-memory Postgres (PGlite) running in-process on wasmtime, served on a Unix socket."""

from __future__ import annotations

import os
from urllib.parse import quote

from ._backend import Artifacts, Backend, PGliteError, Runtime, default_runtime
from ._server import Server

__all__ = ["Artifacts", "Backend", "PGlite", "PGliteError", "Runtime"]


class PGlite:
    """A throwaway Postgres database, reachable by any client through a Unix socket.

        with PGlite() as pg:
            psycopg.connect(pg.dsn)

    There is a single backend: connections take turns, and session state is shared.
    """

    user = "postgres"
    database = "postgres"

    def __init__(
        self,
        *,
        runtime: Runtime | None = None,
        socket_dir: str | os.PathLike | None = None,
        busy_timeout: float = 30.0,
        log_path: str | os.PathLike | None = None,
    ):
        self.backend = Backend(runtime or default_runtime(), log_path=log_path)
        self.server = Server(self.backend, socket_dir, busy_timeout=busy_timeout)
        self._started = False

    def start(self) -> PGlite:
        if not self._started:
            self.server.start()
            self._started = True
        return self

    def stop(self) -> None:
        if self._started:
            self.server.stop()
            self._started = False

    def __enter__(self) -> PGlite:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def socket_dir(self) -> str:
        return str(self.server.socket_dir)

    @property
    def dsn(self) -> str:
        """A libpq connection string (psycopg, psycopg2)."""
        return f"host={self.socket_dir} port={self.server.port} user={self.user} dbname={self.database}"

    def url(self, scheme: str = "postgresql") -> str:
        """A connection URL, e.g. for SQLAlchemy: url("postgresql+psycopg")."""
        return (
            f"{scheme}://{self.user}@/{self.database}"
            f"?host={quote(self.socket_dir)}&port={self.server.port}"
        )
