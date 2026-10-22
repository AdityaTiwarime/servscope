"""Admin access: one bearer token for everything that changes ServScope's behaviour (alert
rules, retention, acknowledging incidents, failure scenarios), and an audit log of each change.

- The token travels in the Authorization header, never in a cookie, so another website
  cannot make the browser send it (no cross-site request forgery).
- It is compared with secrets.compare_digest, which takes the same time however many leading
  characters match, so response timing can't be used to guess it one character at a time.
- Without SERVSCOPE_ADMIN_TOKEN set, every admin call is refused: there is no default.
- Every successful change writes who (from where), what and when to the audit log, in the
  same transaction as the change itself, so the log can't miss a change that happened.
"""

from __future__ import annotations

import json
import secrets
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .config import Settings
from .query import ApiError

_AUDIT = text(
    "INSERT INTO audit_log (actor, action, details) "
    "VALUES (:actor, :action, CAST(:details AS jsonb))"
)


def require_admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    """FastAPI dependency: returns the actor name for the audit log, or refuses the call."""
    settings: Settings = request.app.state.settings
    if settings.admin_token is None:
        raise ApiError(403, "Admin API disabled", "set SERVSCOPE_ADMIN_TOKEN to enable it")
    scheme, _, given = (authorization or "").partition(" ")
    expected = settings.admin_token.get_secret_value()
    if scheme.lower() != "bearer" or not secrets.compare_digest(given.encode(), expected.encode()):
        raise ApiError(
            401,
            "Admin token required",
            "send Authorization: Bearer <admin token>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    client = request.client.host if request.client else "unknown"
    return f"admin@{client}"[:64]


Admin = Annotated[str, Depends(require_admin)]


async def audit(conn: AsyncConnection, actor: str, action: str, **details: Any) -> None:
    params = {"actor": actor, "action": action, "details": json.dumps(details, default=str)}
    await conn.execute(_AUDIT, params)
