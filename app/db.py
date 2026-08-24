"""Пул соединений к Postgres. Агент ходит в базу напрямую по SQL."""
import atexit

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import config

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config.DATABASE_URL,
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        # без явного закрытия скрипты падают в трейсбек на выходе из интерпретатора
        atexit.register(close_pool)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def query(sql: str, params: tuple = ()) -> list[dict]:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(sql: str, params: tuple = ()) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def connect() -> psycopg.Connection:
    """Отдельное соединение для долгих задач ingest — пул им занимать незачем."""
    return psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
