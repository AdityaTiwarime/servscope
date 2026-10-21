import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from servscope.models import Base

DEFAULT_URL = "postgresql+asyncpg://servscope:servscope@localhost:5432/servscope"
url = os.environ.get("SERVSCOPE_DATABASE_URL", DEFAULT_URL)


def run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def main() -> None:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(run)
        await conn.commit()
    await engine.dispose()


asyncio.run(main())
