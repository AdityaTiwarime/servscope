"""Milestone 6: what ServScope does when its own dependencies fail, and its security edges."""

from __future__ import annotations

from typing import Any

from conftest import TOKEN, client_for, settings
from sqlalchemy import text

DEAD_DB = "postgresql+asyncpg://servscope:servscope@127.0.0.1:1/servscope"  # nothing listens


async def test_read_api_with_the_database_down_says_so(clean_db: str) -> None:
    async with client_for(settings(DEAD_DB)) as c:
        for path in ("/api/services", "/api/traces", "/api/incidents", "/api/config"):
            r = await c.get(path)
            assert r.status_code == 503, (path, r.status_code, r.text)
            assert r.headers["content-type"] == "application/problem+json"
            body = r.json()
            assert body["title"] == "Database unavailable"
            assert body["request_id"] == r.headers["X-Request-ID"]
            assert "Traceback" not in r.text and "asyncpg" not in r.text
        assert (await c.get("/healthz")).status_code == 200  # the process itself is fine
        assert (await c.get("/readyz")).status_code == 503


async def test_a_runaway_query_is_cut_off_with_a_clear_answer(clean_db: str) -> None:
    """The statement timeout is enforced by PostgreSQL itself; the caller gets a 503 that
    says what happened, and the connection goes back to the pool for the next request."""
    async with client_for(settings(clean_db, db_statement_timeout_ms=200)) as c:
        app: Any = c.app  # type: ignore[attr-defined]

        async def slow() -> dict[str, str]:
            async with app.state.engine.connect() as conn:
                await conn.execute(text("SELECT pg_sleep(2)"))
            return {"never": "reached"}

        app.add_api_route("/api/_test_slow", slow)
        app.router.routes.insert(0, app.router.routes.pop())  # ahead of the dashboard catch-all
        r = await c.get("/api/_test_slow")
        assert r.status_code == 503
        assert r.json()["title"] == "Query took too long"
        assert r.headers["Retry-After"] == "5"
        assert (await c.get("/api/services")).status_code == 200  # the pool still works


async def test_an_unexpected_error_leaks_nothing(clean_db: str) -> None:
    async with client_for(settings(clean_db)) as c:
        app: Any = c.app  # type: ignore[attr-defined]

        async def broken() -> None:
            raise RuntimeError("password=hunter2 at /srv/internal/module.py line 42")

        app.add_api_route("/api/_test_broken", broken)
        r = await c.get("/api/_test_broken")
    assert r.status_code == 500
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["title"] == "Internal error"
    assert "hunter2" not in r.text and "module.py" not in r.text and "Traceback" not in r.text
    assert r.json()["request_id"] == r.headers["X-Request-ID"]


# --- security edges -------------------------------------------------------------------

EVIL = "https://evil.example"


async def test_other_websites_cannot_read_the_api(make_client: Any) -> None:
    """No CORS headers at all: the dashboard is served from ServScope itself (same origin),
    so no other origin is ever allowed, and the browser blocks a foreign page's reads."""
    async with make_client() as c:
        r = await c.get("/api/services", headers={"Origin": EVIL})
        preflight = await c.options(
            "/api/alert-rules",
            headers={"Origin": EVIL, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "authorization"},
        )  # fmt: skip
    assert r.status_code == 200
    assert not any(h.lower().startswith("access-control-") for h in r.headers)
    assert preflight.status_code in (404, 405)
    assert not any(h.lower().startswith("access-control-") for h in preflight.headers)


async def test_every_response_carries_baseline_security_headers(make_client: Any) -> None:
    async with make_client() as c:
        for r in (await c.get("/api/services"), await c.get("/api/nope"), await c.get("/healthz")):
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["X-Frame-Options"] == "DENY"
        assert (await c.get("/api/services")).headers["Cache-Control"] == "no-store"


async def test_guessing_the_admin_token_is_locked_out(make_client: Any) -> None:
    from conftest import ADMIN, ADMIN_TOKEN
    from pydantic import SecretStr

    from servscope.admin import MAX_FAILURES

    async with make_client(admin_token=SecretStr(ADMIN_TOKEN)) as c:
        app: Any = c.app  # type: ignore[attr-defined]
        now = [1000.0]
        app.state.failed_admin_attempts.clock = lambda: now[0]
        wrong = {"Authorization": "Bearer guess-number-x-0123456789"}
        codes = [
            (await c.get("/api/audit", headers=wrong)).status_code for _ in range(MAX_FAILURES)
        ]
        assert codes == [401] * MAX_FAILURES
        locked = await c.get("/api/audit", headers=ADMIN)  # even the right token waits
        assert locked.status_code == 429
        assert 0 < int(locked.headers["Retry-After"]) <= 60
        now[0] += 61  # the window passes
        assert (await c.get("/api/audit", headers=ADMIN)).status_code == 200


def test_tracking_failed_attempts_has_bounded_memory() -> None:
    from servscope import admin

    tracker = admin.FailedAttempts(clock=lambda: 5.0)
    for i in range(admin.MAX_TRACKED + 500):
        tracker.fail(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
    assert len(tracker.seen) <= admin.MAX_TRACKED


async def test_ingest_still_needs_its_own_token(make_client: Any) -> None:
    """The admin token is not a master key: ingest keeps its separate token."""
    from conftest import ADMIN_TOKEN
    from pydantic import SecretStr

    async with make_client(admin_token=SecretStr(ADMIN_TOKEN)) as c:
        r = await c.post(
            "/v1/traces",
            content=b"",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}",
                     "Content-Type": "application/x-protobuf"},
        )  # fmt: skip
        ok = await c.post(
            "/v1/traces",
            content=b"",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/x-protobuf"},
        )
    assert r.status_code == 401 and ok.status_code == 200


# --- ServScope watching itself (N7) ---------------------------------------------------


async def test_self_health_reports_ingest_database_and_jobs(make_client: Any) -> None:
    async with make_client() as c:
        app: Any = c.app  # type: ignore[attr-defined]
        app.state.stats.add("servscope_spans_stored_total", 7)
        app.state.stats.mark("alerts_ok")
        body = (await c.get("/api/self")).json()
    assert body["ingest"]["spans_stored"] == 7
    assert body["ingest"]["ingest_refused"] == 0  # every counter is present, even at zero
    assert body["database"]["reachable"] is True and body["database"]["latency_ms"] >= 0
    assert body["write_slots"] == {"in_use": 0, "total": 8}
    assert body["jobs"]["alerts"]["last_ok"] is not None
    assert body["jobs"]["retention"]["enabled"] is False  # switched off in tests


async def test_self_health_still_answers_with_the_database_down(clean_db: str) -> None:
    async with client_for(settings(DEAD_DB)) as c:
        r = await c.get("/api/self")
    assert r.status_code == 200
    assert r.json()["database"] == {
        "reachable": False, "latency_ms": None, "pool_in_use": 0, "pool_size": 10,
    }  # fmt: skip
