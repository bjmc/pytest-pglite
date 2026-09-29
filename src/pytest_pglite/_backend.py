"""Host for the standalone PGlite WASM module, on wasmtime.

This mirrors the host protocol documented in packages/pglite-standalone/README.md
of the pglite repository (and implemented there for Node in src/index.ts).
"""

from __future__ import annotations

import gzip
import os
import struct
import threading
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Callable

import wasmtime as wt

PGDATA = "/pglite/data"
PGLITE_EXIT_ALIVE = 99
POSTGRES_MAIN_LONGJMP = 100

DEFAULT_START_PARAMS = [
    "--single",
    "-F",
    "-O",
    "-j",
    "-c", "search_path=public",
    "-c", "exit_on_error=false",
    "-c", "log_checkpoints=false",
    "-c", "max_worker_processes=0",
    "-c", "max_parallel_workers=0",
    "-c", "max_parallel_workers_per_gather=0",
    "-c", "io_method=sync",
    "-c", "max_parallel_maintenance_workers=0",
    # there is no /dev/shm; sysv shared memory is emulated in-process
    "-c", "dynamic_shared_memory_type=sysv",
]  # fmt: skip

DEFAULT_ENV = {
    "HOME": "/home/postgres",
    "USER": "postgres",
    "LOGNAME": "postgres",
    "PGDATA": PGDATA,
    "PGUSER": "postgres",
    "PGDATABASE": "postgres",
    "LANG": "en_US.UTF-8",
    "LC_COLLATE": "en_US.UTF-8",
    "LC_CTYPE": "en_US.UTF-8",
    "TZ": "UTC",
    "PGTZ": "UTC",
    "PGCLIENTENCODING": "UTF8",
    "ICU_DATA": "/pglite/icu",
}

ARTIFACTS_DIR_ENV = "PYTEST_PGLITE_ARTIFACTS"


class PGliteError(RuntimeError):
    pass


def _read_tar(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


@dataclass(frozen=True)
class Artifacts:
    """The build artifacts needed to run a backend."""

    wasm: Path
    runtime_fs: Path
    pgdata: Path

    @classmethod
    def default(cls) -> Artifacts:
        root = Path(os.environ.get(ARTIFACTS_DIR_ENV) or Path(__file__).parent / "_artifacts")
        return cls(
            wasm=root / "pglite-standalone.wasm",
            runtime_fs=root / "pglite-standalone-fs.tar.gz",
            pgdata=root / "pgdata.tar.gz",
        )


class Runtime:
    """A compiled module plus the decompressed filesystem images.

    Expensive to create (compilation, decompression) and cheap to share: create
    one per process and use it for any number of backends.
    """

    def __init__(self, artifacts: Artifacts | None = None):
        self.artifacts = artifacts or Artifacts.default()
        for path in (self.artifacts.wasm, self.artifacts.runtime_fs, self.artifacts.pgdata):
            if not path.exists():
                raise FileNotFoundError(
                    f"PGlite build artifact not found: {path} (set {ARTIFACTS_DIR_ENV})"
                )
        cfg = wt.Config()
        cfg.wasm_exceptions = True
        cfg.cache = True  # wasmtime's on-disk compilation cache
        self.engine = wt.Engine(cfg)
        self.module = wt.Module.from_file(self.engine, str(self.artifacts.wasm))

    @cached_property
    def runtime_fs(self) -> bytes:
        return _read_tar(self.artifacts.runtime_fs)

    @cached_property
    def pgdata(self) -> bytes:
        return _read_tar(self.artifacts.pgdata)


_default_runtime: Runtime | None = None
_default_runtime_lock = threading.Lock()


def default_runtime() -> Runtime:
    global _default_runtime
    with _default_runtime_lock:
        if _default_runtime is None:
            _default_runtime = Runtime()
        return _default_runtime


class Backend:
    """One single-user Postgres backend, in its own wasmtime store.

    Speaks the Postgres wire protocol through exec_protocol_raw(). Not
    thread-safe: callers must serialize access (see _server.Multiplexer).
    """

    def __init__(
        self,
        runtime: Runtime | None = None,
        *,
        pgdata: bytes | None = None,
        start_params: list[str] | None = None,
        env: dict[str, str] | None = None,
        log_path: str | os.PathLike | None = None,
    ):
        self.runtime = runtime or default_runtime()
        self.store = wt.Store(self.runtime.engine)

        env = {**DEFAULT_ENV, **(env or {})}
        wasi = wt.WasiConfig()
        wasi.env = list(env.items())
        log = os.fspath(log_path) if log_path is not None else os.devnull
        wasi.stdout_file = log
        wasi.stderr_file = log
        self.store.set_wasi(wasi)

        linker = wt.Linker(self.runtime.engine)
        linker.define_wasi()
        i32 = wt.ValType.i32()
        linker.define_func("pglite", "read", wt.FuncType([i32, i32], [i32]), self._host_read)
        linker.define_func("pglite", "write", wt.FuncType([i32, i32], [i32]), self._host_write)
        linker.define_func(
            "env", "emscripten_notify_memory_growth", wt.FuncType([i32], []), lambda _: None
        )
        self._instance = linker.instantiate(self.store, self.runtime.module)
        self._exports = self._instance.exports(self.store)
        self._memory: wt.Memory = self._exports["memory"]
        self._funcs: dict[str, wt.Func] = {}

        self._input = b""
        self._read_offset = 0
        self._output: list[bytes] = []
        self._need_input: Callable[[], bytes] | None = None
        self._send_output: Callable[[bytes], None] | None = None

        self._call("_initialize")
        self._load_tar("/", self.runtime.runtime_fs)
        self._load_tar(PGDATA, pgdata if pgdata is not None else self.runtime.pgdata)
        self._start(start_params or DEFAULT_START_PARAMS, env["PGDATABASE"])

    # -- plumbing --------------------------------------------------------------
    def _call(self, name: str, *args):
        func = self._funcs.get(name)
        if func is None:
            func = self._funcs[name] = self._exports[name]
        return func(self.store, *args)

    def _host_read(self, ptr: int, max_len: int) -> int:
        if self._read_offset >= len(self._input) and self._need_input is not None:
            # Postgres wants more input in the middle of a command (COPY FROM STDIN):
            # the client first needs to see what we have so far
            if self._output and self._send_output is not None:
                self._send_output(b"".join(self._output))
                self._output = []
            self._input = self._need_input()
            self._read_offset = 0
        chunk = self._input[self._read_offset : self._read_offset + max_len]
        self._memory.write(self.store, chunk, ptr)
        self._read_offset += len(chunk)
        return len(chunk)

    def _host_write(self, ptr: int, length: int) -> int:
        self._output.append(bytes(self._memory.read(self.store, ptr, ptr + length)))
        return length

    def _alloc(self, data: bytes) -> int:
        ptr = self._call("malloc", max(len(data), 1))
        self._memory.write(self.store, data, ptr)
        return ptr

    def _load_tar(self, prefix: str, tar: bytes) -> None:
        p_prefix = self._alloc(prefix.encode() + b"\0")
        p_tar = self._alloc(tar)
        try:
            rc = self._call("pgl_fs_load_tar", p_prefix, p_tar, len(tar))
        finally:
            self._call("free", p_tar)
            self._call("free", p_prefix)
        if rc != 0:
            raise PGliteError(f"loading filesystem into {prefix} failed (errno {-rc})")

    def _start(self, start_params: list[str], database: str) -> None:
        args = ["/pglite/bin/postgres", *start_params, "-D", PGDATA, database]
        ptrs = [self._alloc(a.encode() + b"\0") for a in args]
        argv = self._alloc(struct.pack(f"<{len(ptrs) + 1}I", *ptrs, 0))
        self._call("pgl_setPGliteActive", 1)
        self._call("pgl_call_main", len(args), argv)
        status = self._call("pgl_setPGliteExitStatus", -3)
        if status != PGLITE_EXIT_ALIVE:
            raise PGliteError(f"PGlite failed to start (exit status {status})")
        self._call("pgl_startPGlite")

    # -- protocol ----------------------------------------------------------------
    def exec_protocol_raw(
        self,
        message: bytes,
        *,
        need_input: Callable[[], bytes] | None = None,
        send_output: Callable[[bytes], None] | None = None,
    ) -> bytes:
        """Execute one or more complete frontend messages; return the backend's response.

        Some commands (COPY FROM STDIN) read further messages from the client
        while they run. For those, need_input is called to get more messages,
        after send_output has been called with the response so far. Without
        need_input, the backend sees the end of its input.
        """
        self._input = message
        self._read_offset = 0
        self._output = []
        self._need_input = need_input
        self._send_output = send_output
        try:
            if message[:1] == b"X":
                return b""
            if message[:1] == b"\0":
                return self._startup()
            try:
                while (
                    self._read_offset < len(self._input)
                    or self._call("pq_buffer_remaining_data") > 0
                ):
                    if self._call("pgl_loop_once") != 0:
                        # unwound to the host: a top level error longjmp
                        if self._call("pgl_setPGliteExitStatus", -2) == POSTGRES_MAIN_LONGJMP:
                            self._call("pgl_longjmp_recover")
            finally:
                self._call("PostgresSendReadyForQueryIfNecessary")
                self._call("pgl_pq_flush")
            return b"".join(self._output)
        finally:
            self._input = b""
            self._output = []
            self._need_input = None
            self._send_output = None

    def _startup(self) -> bytes:
        port = self._call("pgl_getMyProcPort")
        if self._call("ProcessStartupPacket", port, 1, 1) != 0:
            raise PGliteError("cannot process startup packet")
        self._call("pgl_sendConnData")
        self._call("pgl_pq_flush")
        return b"".join(self._output)
