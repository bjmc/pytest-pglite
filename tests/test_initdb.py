from pytest_pglite._initdb import _command_args


def test_command_args():
    assert _command_args('"/pglite/bin/postgres" -V') == ["/pglite/bin/postgres", "-V"]
    assert _command_args(
        '"/pglite/bin/postgres" --check -c max_connections=2 < "/dev/null" > "/dev/null" 2>&1'
    ) == ["/pglite/bin/postgres", "--check", "-c", "max_connections=2"]
    assert _command_args('"/pglite/bin/postgres" --single template1 >/dev/null') == [
        "/pglite/bin/postgres",
        "--single",
        "template1",
    ]
