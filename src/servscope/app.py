"""The ServScope API process: OTLP ingest, the read API, and the retention job."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import text

from . import __version__, retention
from .config import Settings, get_settings
from .db import make_engine, make_sessionmaker
from .ingest import WriteSlots
from .ingest import router as ingest_router
from .query import ApiError
from .query import router as query_router
from .stats import Stats

PROBLEM = "application/problem+json"


# The dashboard may load scripts, styles, fonts and data only from ServScope itself, and may
# not be framed by another site. 'unsafe-inline' for styles covers React's style attributes;
# scripts get no such exception.
UI_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def mount_ui(app: FastAPI, ui_dir: Path) -> None:
    """Serve the built dashboard. Real files (hashed assets) are sent as they are; any other
    path gets index.html so the dashboard's own router can show /traces/<id> and the like."""
    root = ui_dir.resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def ui(path: str) -> Response:
        if path.startswith(("api/", "v1/")):  # an unknown API route is a 404, not the page
            return problem(404, "Not found")
        candidate = (root / path).resolve()
        # resolve() collapses any ../ first, so this check stops path traversal.
        if path and candidate.is_file() and candidate.is_relative_to(root):
            immutable = candidate.parent.name == "assets"  # Vite puts a hash in these names
            cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
            return FileResponse(candidate, headers={**UI_HEADERS, "Cache-Control": cache})
        return FileResponse(index, headers={**UI_HEADERS, "Cache-Control": "no-cache"})


def problem(status: int, title: str, detail: str = "") -> JSONResponse:
    return JSONResponse(
        {"title": title, "status": status, "detail": detail}, status, media_type=PROBLEM
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        task = None
        if settings.retention_enabled:
            task = asyncio.create_task(retention.run_forever(engine, settings, app.state.stats))
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await engine.dispose()

    # No interactive docs in production: they advertise every endpoint to whoever finds the port.
    docs = "/docs" if settings.env == "demo" else None
    app = FastAPI(
        title="ServScope", version=__version__, lifespan=lifespan, docs_url=docs, redoc_url=None
    )
    app.state.settings = settings
    app.state.write_slots = WriteSlots(settings.write_slots, settings.slot_wait_seconds)
    app.state.stats = Stats()

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem(exc.status, exc.title, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Say which parameter is wrong, without echoing the submitted value back.
        fields = sorted({".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()})
        return problem(422, "Invalid request", "check: " + ", ".join(fields))

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

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> PlainTextResponse:
        slots: WriteSlots = request.app.state.write_slots
        body = request.app.state.stats.render(
            {
                "servscope_write_slots_in_use": slots.in_use,
                "servscope_write_slots_total": slots.total,
            }
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    app.include_router(ingest_router)
    app.include_router(query_router)
    if settings.ui_dir is not None and (settings.ui_dir / "index.html").is_file():
        mount_ui(app, settings.ui_dir)  # last, so it never shadows an API route
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="info")  # noqa: S104


if __name__ == "__main__":
    main()
