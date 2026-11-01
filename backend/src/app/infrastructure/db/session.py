from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio import AsyncSession as AsyncSession


def create_engine(url: str, *, pool_size: int = 10, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        echo=echo,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    """SQLAlchemy URL -> raw asyncpg DSN (LISTEN connection, migration lock).

    SQLAlchemy hands `?ssl=require` (Azure Database for PostgreSQL) to asyncpg as a keyword,
    but in a raw DSN asyncpg only understands libpq's `sslmode`; unknown query parameters
    would be sent to the server as settings and rejected."""
    dsn = sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parts = urlsplit(dsn)
    if not parts.query:
        return dsn
    query = [("sslmode" if k == "ssl" else k, v) for k, v in parse_qsl(parts.query)]
    return urlunsplit(parts._replace(query=urlencode(query)))
