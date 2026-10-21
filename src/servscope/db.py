from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    # A short statement timeout keeps one slow query from holding a write slot for ever.
    return create_async_engine(
        url,
        pool_size=10,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={"server_settings": {"statement_timeout": "5000"}, "timeout": 3},
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(engine, expire_on_commit=False)
