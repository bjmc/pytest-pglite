import threading

import asyncpg
import psycopg
import pytest


def test_psycopg(pg):
    with psycopg.connect(pg.dsn) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
        assert conn.execute("SELECT %s::int + %s", (2, 3)).fetchone() == (5,)


def test_psycopg_types_and_prepared_statements(pg):
    with psycopg.connect(pg.dsn) as conn:
        conn.execute("CREATE TABLE t (id serial PRIMARY KEY, name text, data jsonb, at timestamptz)")
        for i in range(10):  # psycopg prepares statements after 5 executions
            conn.execute(
                "INSERT INTO t (name, data, at) VALUES (%s, %s, now())",
                (f"n{i}", psycopg.types.json.Jsonb({"i": i})),
            )
        assert conn.execute("SELECT count(*), max(data->>'i') FROM t").fetchone() == (10, "9")


def test_psycopg_copy(pg):
    with psycopg.connect(pg.dsn) as conn:
        conn.execute("CREATE TABLE c (a int, b text)")
        with conn.cursor().copy("COPY c FROM STDIN") as copy:
            for i in range(100):
                copy.write_row((i, f"row {i}"))
        assert conn.execute("SELECT count(*), sum(a) FROM c").fetchone() == (100, 4950)


def test_errors_and_transactions(pg):
    with psycopg.connect(pg.dsn) as conn:
        conn.execute("CREATE TABLE e (x int)")
        conn.commit()
        with pytest.raises(psycopg.errors.DivisionByZero):
            conn.execute("INSERT INTO e VALUES (1/0)")
        conn.rollback()
        conn.execute("INSERT INTO e VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT x FROM e").fetchall() == [(1,)]


def test_asyncpg(pg):
    import asyncio

    async def main():
        conn = await asyncpg.connect(host=pg.socket_dir, user="postgres", database="postgres")
        try:
            assert await conn.fetchval("SELECT $1::int * 2", 21) == 42
            await conn.execute("CREATE TABLE a (x int)")
            await conn.executemany("INSERT INTO a VALUES ($1)", [(i,) for i in range(5)])
            assert await conn.fetchval("SELECT sum(x) FROM a") == 10
        finally:
            await conn.close()

    asyncio.run(main())


def test_connections_take_turns_around_transactions(pg):
    a = psycopg.connect(pg.dsn)
    b = psycopg.connect(pg.dsn, autocommit=True)
    a.execute("CREATE TABLE turns (who text)")
    a.commit()
    a.execute("INSERT INTO turns VALUES ('a')")  # a is now in a transaction

    result = []
    t = threading.Thread(target=lambda: result.append(b.execute("SELECT who FROM turns").fetchall()))
    t.start()
    t.join(0.3)
    assert t.is_alive(), "b should wait while a is in a transaction"
    a.commit()
    t.join(5)
    assert result == [[("a",)]]
    a.close()
    b.close()


def test_busy_timeout(pg):
    a = psycopg.connect(pg.dsn)
    a.execute("SELECT 1")  # a is in a transaction, and never finishes it
    # connecting needs the backend too
    with pytest.raises(psycopg.OperationalError, match="PGlite backend is busy"):
        psycopg.connect(pg.dsn)
    a.close()


def test_disconnect_mid_transaction_rolls_back(pg):
    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE d (x int)")
    a = psycopg.connect(pg.dsn)
    a.execute("INSERT INTO d VALUES (1)")
    a.close()  # without committing
    with psycopg.connect(pg.dsn) as conn:
        assert conn.execute("SELECT count(*) FROM d").fetchone() == (0,)


def test_sqlalchemy(pg):
    import sqlalchemy as sa
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

    class Base(DeclarativeBase):
        pass

    class User(Base):
        __tablename__ = "users"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]

    engine = sa.create_engine(pg.url("postgresql+psycopg"))  # default QueuePool
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all([User(name="ada"), User(name="grace")])
            session.commit()
        with Session(engine) as session:
            assert session.scalars(sa.select(User.name).order_by(User.id)).all() == ["ada", "grace"]
    finally:
        engine.dispose()
