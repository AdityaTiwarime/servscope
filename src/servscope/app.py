"""The ServScope API process."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import __version__
from .config import Settings, get_settings
from .db import make_engine, make_sessionmaker
from .ingest import WriteSlots
from .ingest import router as ingest_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        yield
        await engine.dispose()

    # No interactive docs in production: they advertise every endpoint to whoever finds the port.
    docs = "/docs" if settings.env == "demo" else None
    app = FastAPI(
        title="ServScope", version=__version__, lifespan=lifespan, docs_url=docs, redoc_url=None
    )
    app.state.settings = settings
    app.state.write_slots = WriteSlots(settings.write_slots, settings.slot_wait_seconds)

    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Trust the caller's ID only if it looks like one; otherwise make our own.
        rid = request.headers.get("x-request-id", "")
        if not (0 < len(rid) <= 64 and rid.replace("-", "").isalnum()):
            rid = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        slots: WriteSlots = request.app.state.write_slots
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # any failure to reach the database means "not ready"
            db_ok = False
        ready = db_ok and slots.in_use < slots.total
        body = {
            "database": "ok" if db_ok else "unavailable",
            "write_slots_in_use": slots.in_use,
            "write_slots_total": slots.total,
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    app.include_router(ingest_router)
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="info")  # noqa: S104


if __name__ == "__main__":
    main()
