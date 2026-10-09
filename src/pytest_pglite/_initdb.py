"""Create PGDATA with initdb, run as a standalone module on wasmtime.

initdb runs postgres as a subprocess (with system() and popen()). The module
hands those calls to the host (the pglite.exec import), and we run each in a
fresh instance of the postgres module. Each instance has its own in-memory
filesystem, so PGDATA is copied into the postgres instance before it runs and
back into initdb's afterwards; as the two take turns, that is equivalent to a
shared filesystem. This mirrors initdb.ts in the pglite repository, which runs
the JS build of initdb with a filesystem shared between the two modules.

    python -m pytest_pglite._initdb INITDB_WASM OUTPUT

writes the new PGDATA to OUTPUT (a .tar.gz), using the other artifacts from
the artifacts directory.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import shlex
import sys
import tarfile
import tempfile
from pathlib import Path

import wasmtime as wt

from ._backend import DEFAULT_ENV, PGDATA, PGliteError, Runtime, _Instance

POSTGRES = "/pglite/bin/postgres"
INITDB = "/pglite/bin/initdb"
PIPE_TO_CHILD = "/pglite/pgstdin"
PIPE_FROM_CHILD = "/pglite/pgstdout"

# as in initdb.ts
DEFAULT_ARGS = [
    "--allow-group-access",
    "--encoding", "UTF8",
    "--locale=C.UTF-8",
    "--locale-provider=libc",
    "--auth=trust",
]  # fmt: skip


def _command_args(command: str) -> list[str]:
    """The arguments of a shell command, up to the first redirection."""
    args = []
    for token in shlex.split(command):
        if re.match(r"\d*[<>]", token):
            break
        args.append(token)
    return args


class _Initdb:
    def __init__(self, runtime: Runtime, module: wt.Module, workdir: Path):
        self.runtime = runtime
        self.workdir = workdir
        self.output: list[str] = []  # what initdb and its subprocesses printed
        self.initdb = _Instance(
            runtime.engine,
            module,
            env=DEFAULT_ENV,
            stdout=workdir / "initdb.out",
            stderr=workdir / "initdb.err",
            exec=self._exec,
        )
        self.initdb.load_tar("/", runtime.runtime_fs)

    def run(self, args: list[str]) -> bytes:
        code = self.initdb.call_main([INITDB, *args])
        for name in ("initdb.out", "initdb.err"):
            self.output.append((self.workdir / name).read_text(errors="replace"))
        if code:
            raise PGliteError(f"initdb failed (exit code {code}):\n" + "".join(self.output))
        pgdata: bytes = self.initdb.dump_tar(PGDATA)  # type: ignore[assignment]
        with tarfile.open(fileobj=io.BytesIO(pgdata)) as tar:
            if "postmaster.pid" in tar.getnames():
                raise PGliteError("postmaster.pid left behind: Postgres did not shut down cleanly")
        return pgdata

    def _exec(self, p_command: int, p_stdin_path: int, p_stdout_path: int) -> int:
        """pglite.exec: run a postgres command for initdb, return its exit code."""
        command = self.initdb.string(p_command)
        args = _command_args(command)
        if not args or args[0] != POSTGRES:
            self.output.append(f"initdb: cannot run {command!r}\n")
            return -1
        stdin = self.initdb.read_file(self.initdb.string(p_stdin_path)) if p_stdin_path else None
        # PGDATA does not exist yet when initdb checks the postgres version
        code, stdout, pgdata = self._run_postgres(
            args[1:], self.initdb.dump_tar(PGDATA, missing_ok=True), stdin
        )
        if pgdata is not None:
            self.initdb.remove_tree(PGDATA)
            self.initdb.load_tar(PGDATA, pgdata)
        if p_stdout_path:
            self.initdb.write_file(self.initdb.string(p_stdout_path), stdout)
        return code

    def _run_postgres(
        self, args: list[str], pgdata: bytes | None, stdin: bytes | None
    ) -> tuple[int, bytes, bytes | None]:
        stderr = self.workdir / "postgres.err"
        # initdb unsets PGCLIENTENCODING for its subprocesses
        env = {k: v for k, v in DEFAULT_ENV.items() if k != "PGCLIENTENCODING"}
        pg = _Instance(self.runtime.engine, self.runtime.module, env=env, stderr=stderr)
        pg.load_tar("/", self.runtime.runtime_fs)
        if pgdata is not None:
            pg.load_tar(PGDATA, pgdata)
        # as in initdb.ts: stdin and stdout are files, which pgl_exit() flushes
        if stdin is not None:
            pg.write_file(PIPE_TO_CHILD, stdin)
            self._freopen(pg, PIPE_TO_CHILD, "r", 0)
        self._freopen(pg, PIPE_FROM_CHILD, "w", 1)
        code = pg.call_main([POSTGRES, *args])
        self.output.append(stderr.read_text(errors="replace"))
        return code or 0, pg.read_file(PIPE_FROM_CHILD), pg.dump_tar(PGDATA, missing_ok=True)

    @staticmethod
    def _freopen(pg: _Instance, path: str, mode: str, stream: int) -> None:
        if not pg.call(
            "pgl_freopen", pg.alloc(path.encode() + b"\0"), pg.alloc(mode.encode() + b"\0"), stream
        ):
            raise PGliteError(f"cannot redirect stream {stream} to {path}")


def initdb(
    runtime: Runtime, initdb_wasm: str | os.PathLike, args: list[str] | None = None
) -> bytes:
    """Run initdb, and return the new PGDATA as an (uncompressed) tarball."""
    module = wt.Module.from_file(runtime.engine, os.fspath(initdb_wasm))
    with tempfile.TemporaryDirectory(prefix="pglite-initdb-") as workdir:
        return _Initdb(runtime, module, Path(workdir)).run(DEFAULT_ARGS if args is None else args)


def _main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"usage: python -m {__spec__.name} INITDB_WASM OUTPUT")
    initdb_wasm, output = sys.argv[1:]
    tar = initdb(Runtime(), initdb_wasm)
    Path(output).write_bytes(gzip.compress(tar, mtime=0))


if __name__ == "__main__":
    _main()
