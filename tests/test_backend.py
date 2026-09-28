import struct

from pglite_wasm import Backend


def startup() -> bytes:
    body = struct.pack("!I", 196608) + b"user\0postgres\0database\0postgres\0\0"
    return struct.pack("!I", len(body) + 4) + body


def query(sql: str) -> bytes:
    body = sql.encode() + b"\0"
    return b"Q" + struct.pack("!I", len(body) + 4) + body


def messages(data: bytes):
    i = 0
    while i < len(data):
        (n,) = struct.unpack_from("!I", data, i + 1)
        yield data[i : i + 1], data[i + 5 : i + 1 + n]
        i += 1 + n


def rows(data: bytes) -> list[list[str | None]]:
    out = []
    for kind, body in messages(data):
        if kind == b"D":
            (ncols,) = struct.unpack_from("!H", body)
            j, row = 2, []
            for _ in range(ncols):
                (n,) = struct.unpack_from("!i", body, j)
                j += 4
                row.append(None if n < 0 else body[j : j + n].decode())
                j += max(n, 0)
            out.append(row)
    return out


def error_code(data: bytes) -> str | None:
    for kind, body in messages(data):
        if kind == b"E":
            for field in body.split(b"\0"):
                if field[:1] == b"C":
                    return field[1:].decode()
    return None


def test_query(runtime):
    backend = Backend(runtime)
    assert [k for k, _ in messages(backend.exec_protocol_raw(startup()))][-1] == b"Z"
    assert rows(backend.exec_protocol_raw(query("SELECT 1"))) == [["1"]]


def test_error_recovery(runtime):
    backend = Backend(runtime)
    backend.exec_protocol_raw(startup())
    assert error_code(backend.exec_protocol_raw(query("SELECT 1/0"))) == "22012"
    assert rows(backend.exec_protocol_raw(query("SELECT 2"))) == [["2"]]


def test_backends_are_isolated(runtime):
    a, b = Backend(runtime), Backend(runtime)
    a.exec_protocol_raw(startup())
    b.exec_protocol_raw(startup())
    a.exec_protocol_raw(query("CREATE TABLE only_in_a (x int)"))
    assert error_code(b.exec_protocol_raw(query("SELECT * FROM only_in_a"))) == "42P01"
