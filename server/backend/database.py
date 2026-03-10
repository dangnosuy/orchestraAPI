"""
Database connection pool using aiomysql
"""
import aiomysql
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME

_pool: aiomysql.Pool | None = None


async def get_pool() -> aiomysql.Pool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            minsize=2,
            maxsize=10,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool and not _pool.closed:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
