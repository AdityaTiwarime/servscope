from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(url: str, statement_timeout_ms: int = 5000) -> AsyncEngine:
    # A short statement timeout keeps one slow query from holding a write slot or a
    # connection for ever; PostgreSQL cancels it and the API answers 503.
    return create_async_engine(
        url,
        pool_size=10,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {"statement_timeout": str(statement_timeout_ms)},
            "timeout": 3,
        },
    )
