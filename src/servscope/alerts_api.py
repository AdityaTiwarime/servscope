"""API for alert rules, incidents, settings, the audit log and the dashboard's config.

Reading is open like the rest of the read API (ServScope belongs on a private network);
every change needs the admin token and is written to the audit log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from . import retention
from .admin import Admin, audit
from .alerting import Kind, Rule, describe
from .config import Settings
from .evaluator import add_event, resolve
from .latency import BUCKET_EDGES_MS, MIN_SAMPLES
from .query import (
    DEGRADED_ERROR_RATE,
    DEGRADED_P95_MS,
    DOWN_ERROR_RATE,
    NO_DATA_AFTER,
    ApiError,
    decode_cursor,
    encode_cursor,
)

router = APIRouter(prefix="/api")

MAX_RULES = 50
RuleId = Annotated[int, Path(ge=1, le=2**31 - 1)]
IncidentId = Annotated[int, Path(ge=1, le=2**63 - 1)]
# What each kind's threshold means and the values that make sense for it.
THRESHOLDS: dict[str, tuple[float, float, str]] = {
    "error_rate": (0.001, 0.999, "a share of requests between 0.001 and 0.999"),
    "p95_latency": (1, 60_000, "milliseconds between 1 and 60000"),
    "failure_rise": (1.5, 100, "a multiple between 1.5 and 100"),
}


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"\S")
    kind: Kind
    service: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    threshold: float = 0.0
    window_s: int = Field(ge=60, le=3600, multiple_of=60)  # rollups are per minute
    for_s: int = Field(default=0, ge=0, le=3600)
    recover_s: int = Field(default=60, ge=0, le=3600)
    enabled: bool = True

    @model_validator(mode="after")
    def threshold_fits_kind(self) -> Self:
        if self.kind == "unavailable":
            self.threshold = 0.0  # unused: the window itself is the condition
            return self
        low, high, meaning = THRESHOLDS[self.kind]
        if not low <= self.threshold <= high:
            raise ValueError(f"threshold for {self.kind} must be {meaning}")
        return self


class RetentionIn(BaseModel):
    spans: int = Field(ge=1, le=90)
    logs: int = Field(ge=1, le=90)
    rollups: int = Field(ge=1, le=365)


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


def _rule(row: Any) -> Rule:
    return Rule(
        row.id, row.name, row.kind, row.service, row.threshold, row.window_s, row.for_s,
        row.recover_s,
    )  # fmt: skip


def _rule_view(row: Any, states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "service": row.service,
        "threshold": row.threshold,
        "window_s": row.window_s,
        "for_s": row.for_s,
        "recover_s": row.recover_s,
        "enabled": row.enabled,
        "description": describe(_rule(row)),
        "updated_at": row.updated_at,
        "states": states or [],
    }


_RULE_COLUMNS = (
    "id, name, kind, service, threshold, window_s, for_s, recover_s, enabled, updated_at"
)


async def _get_rule(conn: AsyncConnection, rule_id: int) -> Any:
    row = (
        await conn.execute(
            text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),  # noqa: S608
            {"id": rule_id},
        )
    ).first()
    if row is None:
        raise ApiError(404, "Alert rule not found")
    return row


async def _close_incidents_of(conn: AsyncConnection, rule_id: int, why: str) -> int:
    now = datetime.now().astimezone()
    rows = await conn.execute(
        text("SELECT id FROM incidents WHERE rule_id = :r AND state = 'open'"), {"r": rule_id}
    )
    ids = [r.id for r in rows]
    for incident_id in ids:
        await resolve(conn, incident_id, now, why)
    return len(ids)


# --- rules -----------------------------------------------------------------------------


@router.get("/alert-rules")
async def list_rules(request: Request) -> dict[str, Any]:
    async with _engine(request).connect() as conn:
        rules = list(
            await conn.execute(
                text(f"SELECT {_RULE_COLUMNS} FROM alert_rules ORDER BY id")  # noqa: S608
            )
        )
        states: dict[int, list[dict[str, Any]]] = {}
        for s in await conn.execute(
            text(
                "SELECT rule_id, service, state, since, value FROM alert_states "
                "ORDER BY rule_id, service"
            )
        ):
            states.setdefault(s.rule_id, []).append(
                {"service": s.service, "state": s.state, "since": s.since, "value": s.value}
            )
    return {"rules": [_rule_view(r, states.get(r.id)) for r in rules]}


@router.post("/alert-rules", status_code=201)
async def create_rule(request: Request, body: RuleIn, actor: Admin) -> dict[str, Any]:
    async with _engine(request).begin() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM alert_rules"))).scalar_one()
        if count >= MAX_RULES:
            raise ApiError(409, "Too many alert rules", f"at most {MAX_RULES}")
        new_id = (
            await conn.execute(
                text(
                    "INSERT INTO alert_rules (name, kind, service, threshold, window_s, for_s, "
                    "recover_s, enabled) VALUES (:name, :kind, :service, :threshold, "
                    ":window_s, :for_s, :recover_s, :enabled) RETURNING id"
                ),
                body.model_dump(),
            )
        ).scalar_one()
        await audit(conn, actor, "rule.create", id=new_id, **body.model_dump())
        row = await _get_rule(conn, new_id)
    return _rule_view(row)


@router.put("/alert-rules/{rule_id}")
async def update_rule(
    request: Request, rule_id: RuleId, body: RuleIn, actor: Admin
) -> dict[str, Any]:
    async with _engine(request).begin() as conn:
        before = await _get_rule(conn, rule_id)
        # Changing WHAT is measured or WHERE makes the old state meaningless; changing a
        # threshold or a window keeps it (the next pass re-judges with the new numbers).
        if (before.kind, before.service) != (body.kind, body.service):
            await conn.execute(text("DELETE FROM alert_states WHERE rule_id = :r"), {"r": rule_id})
            why = "the rule was changed to measure something else"
            await _close_incidents_of(conn, rule_id, why)
        await conn.execute(
            text(
                "UPDATE alert_rules SET name = :name, kind = :kind, service = :service, "
                "threshold = :threshold, window_s = :window_s, for_s = :for_s, "
                "recover_s = :recover_s, enabled = :enabled, updated_at = now() WHERE id = :id"
            ),
            {**body.model_dump(), "id": rule_id},
        )
        await audit(conn, actor, "rule.update", id=rule_id, **body.model_dump())
        row = await _get_rule(conn, rule_id)
    return _rule_view(row)


@router.delete("/alert-rules/{rule_id}", status_code=204)
async def delete_rule(request: Request, rule_id: RuleId, actor: Admin) -> Response:
    async with _engine(request).begin() as conn:
        before = await _get_rule(conn, rule_id)
        closed = await _close_incidents_of(conn, rule_id, "the rule was deleted")
        await conn.execute(text("DELETE FROM alert_rules WHERE id = :id"), {"id": rule_id})
        await audit(conn, actor, "rule.delete", id=rule_id, name=before.name, closed=closed)
    return Response(status_code=204)


# --- incidents -------------------------------------------------------------------------

_INCIDENT_COLUMNS = """
    i.id, i.rule_id, i.rule_name, i.kind, i.service, i.threshold, i.state, i.opened_at,
    i.acknowledged_at, i.resolved_at, i.peak_value,
    (SELECT e.sample_trace_ids FROM incident_events e
     WHERE e.incident_id = i.id AND e.type = 'opened' LIMIT 1) AS sample_trace_ids
"""


def _incident_view(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "rule_id": row.rule_id,
        "rule_name": row.rule_name,
        "kind": row.kind,
        "service": row.service,
        "threshold": row.threshold,
        "state": row.state,
        "opened_at": row.opened_at,
        "acknowledged_at": row.acknowledged_at,
        "resolved_at": row.resolved_at,
        "peak_value": row.peak_value,
        "sample_trace_ids": list(row.sample_trace_ids or []),
    }


@router.get("/incidents")
async def list_incidents(
    request: Request,
    state: Literal["open", "resolved"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    """Newest first. Keyset paging on (opened_at, id), so incidents opening while someone
    pages through history never shift or repeat entries."""
    where: list[str] = ["true"]
    params: dict[str, Any] = {"limit": limit + 1}
    if state:
        where.append("i.state = :state")
        params["state"] = state
    if cursor:
        t, key = decode_cursor(cursor)
        if not isinstance(key, int):
            raise ApiError(400, "Invalid cursor")
        where.append("(i.opened_at, i.id) < (:ct, :ck)")
        params.update(ct=t, ck=key)
    sql = text(
        f"SELECT {_INCIDENT_COLUMNS} FROM incidents i WHERE {' AND '.join(where)} "  # noqa: S608
        "ORDER BY i.opened_at DESC, i.id DESC LIMIT :limit"
    )  # the WHERE pieces are fixed strings; every value is a bound parameter
    async with _engine(request).connect() as conn:
        rows = list(await conn.execute(sql, params))
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].opened_at, page[-1].id) if len(rows) > limit else None
    return {"incidents": [_incident_view(r) for r in page], "next_cursor": next_cursor}


@router.get("/incidents/{incident_id}")
async def get_incident(request: Request, incident_id: IncidentId) -> dict[str, Any]:
    async with _engine(request).connect() as conn:
        row = (
            await conn.execute(
                text(f"SELECT {_INCIDENT_COLUMNS} FROM incidents i WHERE i.id = :id"),  # noqa: S608
                {"id": incident_id},
            )
        ).first()
        if row is None:
            raise ApiError(404, "Incident not found")
        events = [
            {
                "time": e.time,
                "type": e.type,
                "value": e.value,
                "detail": e.detail,
                "sample_trace_ids": list(e.sample_trace_ids),
            }
            for e in await conn.execute(
                text(
                    "SELECT time, type, value, detail, sample_trace_ids FROM incident_events "
                    "WHERE incident_id = :id ORDER BY time, id"
                ),
                {"id": incident_id},
            )
        ]
        rule = None
        if row.rule_id is not None:
            r = (
                await conn.execute(
                    text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),  # noqa: S608
                    {"id": row.rule_id},
                )
            ).first()
            rule = _rule_view(r) if r else None
    return {**_incident_view(row), "events": events, "rule": rule}


@router.post("/incidents/{incident_id}/ack")
async def acknowledge(request: Request, incident_id: IncidentId, actor: Admin) -> dict[str, Any]:
    """Someone is looking at it. Acknowledging twice changes nothing; a resolved incident
    can't be acknowledged."""
    now = datetime.now().astimezone()
    async with _engine(request).begin() as conn:
        row = (
            await conn.execute(
                text("SELECT state, acknowledged_at FROM incidents WHERE id = :id FOR UPDATE"),
                {"id": incident_id},
            )
        ).first()
        if row is None:
            raise ApiError(404, "Incident not found")
        if row.state != "open":
            raise ApiError(409, "Incident already resolved")
        if row.acknowledged_at is None:
            await conn.execute(
                text("UPDATE incidents SET acknowledged_at = :now WHERE id = :id"),
                {"now": now, "id": incident_id},
            )
            await add_event(conn, incident_id, now, "acknowledged", detail=f"by {actor}")
            await audit(conn, actor, "incident.ack", id=incident_id)
    return await get_incident(request, incident_id)


# --- config, settings, audit ----------------------------------------------------------


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    """What the dashboard needs to know about how this ServScope is set up. No secrets."""
    settings: Settings = request.app.state.settings
    async with _engine(request).connect() as conn:
        days = await retention.effective_days(conn, settings)
    return {
        "env": settings.env,
        "admin_enabled": settings.admin_token is not None,
        "scenarios_enabled": settings.scenarios_enabled,
        "alerts": {
            "enabled": settings.alerts_enabled,
            "interval_s": settings.alert_interval_seconds,
            "settle_s": settings.alert_settle_seconds,
        },
        "health": {
            "degraded_error_rate": DEGRADED_ERROR_RATE,
            "degraded_p95_ms": DEGRADED_P95_MS,
            "down_error_rate": DOWN_ERROR_RATE,
            "no_data_after_s": NO_DATA_AFTER.total_seconds(),
        },
        "latency": {"bucket_edges_ms": list(BUCKET_EDGES_MS), "min_samples": MIN_SAMPLES},
        "ingest": {
            "max_body_bytes": settings.max_body_bytes,
            "write_slots": settings.write_slots,
            "max_attributes_bytes": settings.max_attributes_bytes,
        },
        "retention_days": days,
    }


@router.get("/settings/retention")
async def get_retention(request: Request) -> dict[str, int]:
    async with _engine(request).connect() as conn:
        return await retention.effective_days(conn, request.app.state.settings)


@router.put("/settings/retention")
async def put_retention(request: Request, body: RetentionIn, actor: Admin) -> dict[str, int]:
    async with _engine(request).begin() as conn:
        before = await retention.effective_days(conn, request.app.state.settings)
        await conn.execute(
            text(
                "INSERT INTO app_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()"
            ),
            {"k": retention.SETTINGS_KEY, "v": body.model_dump_json()},
        )
        await audit(conn, actor, "settings.retention", before=before, after=body.model_dump())
        return await retention.effective_days(conn, request.app.state.settings)


@router.get("/audit")
async def audit_log(
    request: Request, actor: Admin, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> dict[str, Any]:
    """Admin only, unlike other reads: entries name the address each change came from."""
    async with _engine(request).connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT time, actor, action, details FROM audit_log "
                "ORDER BY time DESC, id DESC LIMIT :n"
            ),
            {"n": limit},
        )
        entries = [
            {"time": r.time, "actor": r.actor, "action": r.action, "details": r.details}
            for r in rows
        ]
    return {"entries": entries}
