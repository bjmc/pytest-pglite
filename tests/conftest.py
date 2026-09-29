import pytest

from pytest_pglite import PGlite, Runtime


@pytest.fixture(scope="session")
def runtime():
    return Runtime()


@pytest.fixture
def pg(runtime):
    with PGlite(runtime=runtime, busy_timeout=2) as pg:
        yield pg
