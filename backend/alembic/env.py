from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.config.settings import get_settings
from app.infrastructure.db.models import Base
from app.infrastructure.runtime.event_loop import configure_event_loop, run

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    explicit = config.attributes.get("database_url")
    if explicit:
        return str(explicit)
    settings = get_settings()
    return settings.database_migration_url or settings.database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


def run_migrations_online() -> None:
    configure_event_loop()
    run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
