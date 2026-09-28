"""Serve a PGlite backend on a Unix domain socket, speaking the Postgres wire protocol.

Any libpq-based client (psycopg, SQLAlchemy, Django, ...) or asyncpg can connect
with host=<socket_dir>.

PGlite is a single backend, so client connections share it: each frontend
message runs under a lock, and a connection that starts a transaction (or an
extended-query sequence, a COPY, ...) keeps the backend until it is idle again,
while the other connections wait. This is the same scheme as pglite-socket's
multiplexer. Session state (SET, prepared statements, temp tables) is shared by
all connections.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import tempfile
import threading
from pathlib import Path
from typing import Callable

from ._backend import Backend

log = logging.getLogger(__name__)

SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104
CANCEL_REQUEST = 80877102
PROTOCOL_3 = 196608


def _last_ready_status(response: bytes) -> bytes | None:
    """The transaction status of the last ReadyForQuery in a response, if any."""
    status = None
    i = 0
    while i + 5 <= len(response):
        kind = response[i : i + 1]
        (length,) = struct.unpack_from("!I", response, i + 1)
        if kind == b"Z":
            status = response[i + 5 : i + 6]
        i += 1 + length
    return status


def _error_response(code: str, message: str) -> bytes:
    """An ErrorResponse followed by ReadyForQuery (idle)."""
    fields = b"".join(
        kind + value.encode() + b"\0"
        for kind, value in ((b"S", "ERROR"), (b"V", "ERROR"), (b"C", code), (b"M", message))
    ) + b"\0"
    return b"E" + struct.pack("!I", len(fields) + 4) + fields + b"Z\0\0\0\5I"


class Multiplexer:
    """Shares one Backend between connections.

    A connection that is waiting for the backend for longer than busy_timeout
    seconds gets an error instead: with a single backend, a client that uses a
    second connection while the first one is in a transaction would otherwise
    wait forever.
    """

    def __init__(self, backend: Backend, busy_timeout: float = 30.0):
        self.backend = backend
        self.busy_timeout = busy_timeout
        self._cond = threading.Condition()
        self._owner: object | None = None

    def execute(
        self,
        conn: object,
        message: bytes,
        need_input: Callable[[], bytes] | None = None,
        send_output: Callable[[bytes], None] | None = None,
    ) -> bytes:
        with self._cond:
            if not self._cond.wait_for(
                lambda: self._owner is None or self._owner is conn, timeout=self.busy_timeout
            ):
                return _error_response(
                    "55006",  # object_in_use
                    f"PGlite backend is busy: another connection has been in a transaction "
                    f"for more than {self.busy_timeout}s (PGlite runs a single backend, so "
                    f"connections cannot work concurrently)",
                )
            self._owner = conn
            try:
                response = self.backend.exec_protocol_raw(
                    message, need_input=need_input, send_output=send_output
                )
            except BaseException:
                self._release()
                raise
            if _last_ready_status(response) == b"I":
                self._release()
            return response

    def disconnect(self, conn: object) -> None:
        """Clean up after a connection that went away, possibly mid-transaction."""
        with self._cond:
            if self._owner is not conn:
                return
            try:
                # end any extended-query sequence, then any transaction
                status = _last_ready_status(self.backend.exec_protocol_raw(b"S\0\0\0\4"))
                if status != b"I":
                    rollback = b"ROLLBACK\0"
                    self.backend.exec_protocol_raw(
                        b"Q" + struct.pack("!I", len(rollback) + 4) + rollback
                    )
            finally:
                self._release()

    def _release(self) -> None:
        self._owner = None
        self._cond.notify_all()


class _Connection:
    def __init__(self, server: Server, sock: socket.socket):
        self.server = server
        self.sock = sock

    def _recv_exact(self, n: int) -> bytes | None:
        chunks = []
        while n:
            chunk = self.sock.recv(n)
            if not chunk:
                return None
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def _recv_message(self) -> bytes | None:
        header = self._recv_exact(5)
        if header is None:
            return None
        (length,) = struct.unpack("!I", header[1:])
        body = self._recv_exact(length - 4)
        if body is None:
            return None
        return header + body

    def _need_input(self) -> bytes:
        """More input for a running command (COPY FROM STDIN)."""
        try:
            message = self._recv_message()
        except OSError:
            message = None
        if message is None:
            # the client went away: fail the COPY rather than the backend
            reason = b"client disconnected\0"
            return b"f" + struct.pack("!I", len(reason) + 4) + reason
        return message

    def serve(self) -> None:
        mux = self.server.multiplexer
        try:
            if not self._startup():
                return
            while True:
                message = self._recv_message()
                if message is None or message[:1] == b"X":
                    return
                response = mux.execute(self, message, self._need_input, self.sock.sendall)
                if response:
                    self.sock.sendall(response)
        except OSError as exc:
            log.debug("connection error: %s", exc)
        finally:
            mux.disconnect(self)
            self.sock.close()

    def _startup(self) -> bool:
        while True:
            header = self._recv_exact(8)
            if header is None:
                return False
            length, code = struct.unpack("!II", header)
            rest = self._recv_exact(length - 8)
            if rest is None:
                return False
            if code in (SSL_REQUEST, GSSENC_REQUEST):
                self.sock.sendall(b"N")  # not supported; the client continues unencrypted
                continue
            if code == CANCEL_REQUEST:
                return False  # queries cannot be cancelled
            if code != PROTOCOL_3:
                return False
            self.sock.sendall(self.server.multiplexer.execute(self, header + rest))
            return True


class Server:
    """A Unix socket server for a Backend, running in background threads."""

    def __init__(
        self,
        backend: Backend,
        socket_dir: str | os.PathLike | None = None,
        port: int = 5432,
        busy_timeout: float = 30.0,
    ):
        self.multiplexer = Multiplexer(backend, busy_timeout)
        self._own_dir = socket_dir is None
        self.socket_dir = Path(socket_dir or tempfile.mkdtemp(prefix="pglite-"))
        self.port = port
        self.socket_path = self.socket_dir / f".s.PGSQL.{port}"
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen()
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, name="pglite-accept", daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while True:
            try:
                sock, _ = self._listener.accept()
            except OSError:
                return  # listener closed
            with self._lock:
                self._connections.add(sock)
            threading.Thread(
                target=self._serve, args=(sock,), name="pglite-conn", daemon=True
            ).start()

    def _serve(self, sock: socket.socket) -> None:
        try:
            _Connection(self, sock).serve()
        finally:
            with self._lock:
                self._connections.discard(sock)

    def stop(self) -> None:
        if self._listener is not None:
            # accept() doesn't wake up on close() alone
            try:
                self._listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._listener.close()
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            for sock in list(self._connections):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.socket_path.unlink(missing_ok=True)
        if self._own_dir:
            try:
                self.socket_dir.rmdir()
            except OSError:
                pass
