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


# errno values of the modules (WASI numbering), for messages
WASI_ENOENT = 44
_WASI_ERRNO_NAMES = {
    2: "EACCES", 20: "EEXIST", 28: "EINVAL", 29: "EIO", 31: "EISDIR", 37: "ENAMETOOLONG",
    44: "ENOENT", 48: "ENOMEM", 51: "ENOSPC", 54: "ENOTDIR", 55: "ENOTEMPTY", 58: "ENOTSUP",
}  # fmt: skip


def _check_artifact(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(
            f"PGlite build artifact not found: {path} (set {ARTIFACTS_DIR_ENV})"
        )


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
        for path in (self.artifacts.wasm, self.artifacts.runtime_fs):
            _check_artifact(path)
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
        _check_artifact(self.artifacts.pgdata)
        return _read_tar(self.artifacts.pgdata)


_default_runtime: Runtime | None = None
_default_runtime_lock = threading.Lock()


def default_runtime() -> Runtime:
    global _default_runtime
    with _default_runtime_lock:
        if _default_runtime is None:
            _default_runtime = Runtime()
        return _default_runtime


class _Instance:
    """An instance of one of the standalone modules, in its own wasmtime store.

    Provides the imports the modules need (WASI, and the "pglite" host
    functions), and wraps the exports for the filesystem and main().
    """

    def __init__(
        self,
        engine: wt.Engine,
        module: wt.Module,
        *,
        env: dict[str, str],
        stdout: str | os.PathLike | None = None,
        stderr: str | os.PathLike | None = None,
        read: Callable[[int, int], int] | None = None,
        write: Callable[[int, int], int] | None = None,
        exec: Callable[[int, int, int], int] | None = None,
    ):
        self.store = wt.Store(engine)
        wasi = wt.WasiConfig()
        wasi.env = list(env.items())
        # each file is truncated and written through its own handle: don't share them
        wasi.stdout_file = os.fspath(stdout) if stdout is not None else os.devnull
        wasi.stderr_file = os.fspath(stderr) if stderr is not None else os.devnull
        self.store.set_wasi(wasi)

        linker = wt.Linker(engine)
        linker.define_wasi()
        i32 = wt.ValType.i32()
        linker.define_func(
            "pglite", "read", wt.FuncType([i32, i32], [i32]), read or (lambda p, n: 0)
        )
        linker.define_func(
            "pglite", "write", wt.FuncType([i32, i32], [i32]), write or (lambda p, n: n)
        )
        linker.define_func(
            "pglite", "exec", wt.FuncType([i32, i32, i32], [i32]), exec or self._exec
        )
        linker.define_func(
            "env", "emscripten_notify_memory_growth", wt.FuncType([i32], []), lambda _: None
        )
        self._instance = linker.instantiate(self.store, module)
        self._exports = self._instance.exports(self.store)
        self.memory: wt.Memory = self._exports["memory"]
        self._funcs: dict[str, wt.Func] = {}
        self.call("_initialize")

    def _exec(self, p_command: int, p_stdin_path: int, p_stdout_path: int) -> int:
        """pglite.exec: Postgres only runs "locale -a" (as in pglite.ts); initdb, see _initdb.py."""
        if self.string(p_command).split() == ["locale", "-a"] and p_stdout_path:
            self.write_file(self.string(p_stdout_path), self.read_file("/pglite/locale-a"))
            return 0
        return -1

    def call(self, name: str, *args):
        func = self._funcs.get(name)
        if func is None:
            func = self._funcs[name] = self._exports[name]
        return func(self.store, *args)

    def read(self, ptr: int, length: int) -> bytes:
        return bytes(self.memory.read(self.store, ptr, ptr + length))

    def write(self, data: bytes, ptr: int) -> None:
        self.memory.write(self.store, data, ptr)

    def alloc(self, data: bytes) -> int:
        ptr = self.call("malloc", max(len(data), 1))
        self.write(data, ptr)
        return ptr

    def string(self, ptr: int) -> str:
        end = ptr
        while self.read(end, 1) != b"\0":
            end += 1
        return self.read(ptr, end - ptr).decode()

    def _check(self, rc: int, what: str) -> None:
        if rc != 0:
            raise PGliteError(f"{what} failed: {_WASI_ERRNO_NAMES.get(-rc, f'errno {-rc}')}")

    def _call_with_path(self, name: str, path: str, *args) -> int:
        p_path = self.alloc(path.encode() + b"\0")
        try:
            return self.call(name, p_path, *args)
        finally:
            self.call("free", p_path)

    def _call_returning_buffer(
        self, name: str, path: str, missing_ok: bool = False
    ) -> bytes | None:
        out = self.alloc(bytes(8))  # char **, size_t *
        try:
            rc = self._call_with_path(name, path, out, out + 4)
            if missing_ok and rc == -WASI_ENOENT:
                return None
            self._check(rc, f"{name}({path})")
            ptr, length = struct.unpack("<II", self.read(out, 8))
            try:
                return self.read(ptr, length)
            finally:
                self.call("free", ptr)
        finally:
            self.call("free", out)

    def load_tar(self, prefix: str, tar: bytes) -> None:
        p_tar = self.alloc(tar)
        try:
            rc = self._call_with_path("pgl_fs_load_tar", prefix, p_tar, len(tar))
        finally:
            self.call("free", p_tar)
        self._check(rc, f"loading filesystem into {prefix}")

    def dump_tar(self, path: str, missing_ok: bool = False) -> bytes | None:
        """A tarball of the directory path, or None if missing_ok and it does not exist."""
        return self._call_returning_buffer("pgl_fs_dump_tar", path, missing_ok)

    def read_file(self, path: str) -> bytes:
        return self._call_returning_buffer("pgl_fs_read_file", path)  # type: ignore[return-value]

    def write_file(self, path: str, data: bytes, mode: int = 0o600) -> None:
        p_data = self.alloc(data)
        try:
            rc = self._call_with_path("pgl_fs_write_file", path, p_data, len(data), mode)
        finally:
            self.call("free", p_data)
        self._check(rc, f"writing {path}")

    def remove_tree(self, path: str) -> None:
        self._check(self._call_with_path("pgl_fs_remove_tree", path), f"removing {path}")

    def call_main(self, args: list[str]) -> int | None:
        """Call main(args); return the exit code if it exited, else None.

        main() returns normally when it unwinds to the host, which a backend does
        once it is ready for queries (see Backend._start).
        """
        ptrs = [self.alloc(a.encode() + b"\0") for a in args]
        argv = self.alloc(struct.pack(f"<{len(ptrs) + 1}I", *ptrs, 0))
        try:
            self.call("pgl_call_main", len(args), argv)
        except wt.ExitTrap as e:
            return e.code
        return None


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
        env = {**DEFAULT_ENV, **(env or {})}
        self._pg = _Instance(
            self.runtime.engine,
            self.runtime.module,
            env=env,
            stderr=log_path,  # where Postgres logs
            read=self._host_read,
            write=self._host_write,
        )
        self._call = self._pg.call

        self._input = b""
        self._read_offset = 0
        self._output: list[bytes] = []
        self._need_input: Callable[[], bytes] | None = None
        self._send_output: Callable[[bytes], None] | None = None

        self._pg.load_tar("/", self.runtime.runtime_fs)
        self._pg.load_tar(PGDATA, pgdata if pgdata is not None else self.runtime.pgdata)
        self._start(start_params or DEFAULT_START_PARAMS, env["PGDATABASE"])

    # -- plumbing --------------------------------------------------------------
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
        self._pg.write(chunk, ptr)
        self._read_offset += len(chunk)
        return len(chunk)

    def _host_write(self, ptr: int, length: int) -> int:
        self._output.append(self._pg.read(ptr, length))
        return length

    def _start(self, start_params: list[str], database: str) -> None:
        self._call("pgl_setPGliteActive", 1)
        code = self._pg.call_main(["/pglite/bin/postgres", *start_params, "-D", PGDATA, database])
        status = self._call("pgl_setPGliteExitStatus", -3)
        if code is not None or status != PGLITE_EXIT_ALIVE:
            raise PGliteError(f"PGlite failed to start (exit code {code}, exit status {status})")
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
