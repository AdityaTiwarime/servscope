"""Admin access: one bearer token for everything that changes ServScope's behaviour (alert
rules, retention, acknowledging incidents, failure scenarios), and an audit log of each change.

- The token travels in the Authorization header, never in a cookie, so another website
  cannot make the browser send it (no cross-site request forgery).
- It is compared with secrets.compare_digest, which takes the same time however many leading
  characters match, so response timing can't be used to guess it one character at a time.
- Without SERVSCOPE_ADMIN_TOKEN set, every admin call is refused: there is no default.
- Guessing is slowed down: after MAX_FAILURES wrong tokens from one address within
  WINDOW_S, that address gets 429 until the window has passed, even with the right token
  (otherwise a lucky guess during the lockout would still get in).
- Every successful change writes who (from where), what and when to the audit log, in the
  same transaction as the change itself, so the log can't miss a change that happened.
"""

from __future__ import annotations

import json
import math
import secrets
import time
from collections import deque
from collections.abc import Callable
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


MAX_FAILURES = 10
WINDOW_S = 60.0
MAX_TRACKED = 10_000  # addresses remembered at once, so a flood can't grow memory forever


class FailedAttempts:
    """Wrong admin tokens per client address, over a sliding window. In memory, per process:
    enough to make guessing a 48-character token hopeless, not a distributed rate limiter.
    The address is the TCP peer, so behind a reverse proxy all clients would share one."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.seen: dict[str, deque[float]] = {}

    def _recent(self, client: str) -> deque[float]:
        attempts = self.seen.get(client, deque())
        cutoff = self.clock() - WINDOW_S
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        return attempts

    def locked_for(self, client: str) -> float:
        """Seconds until this client may try again; 0 when it may try now."""
        attempts = self._recent(client)
        if len(attempts) < MAX_FAILURES:
            return 0.0
        return attempts[0] + WINDOW_S - self.clock()

    def fail(self, client: str) -> None:
        if client not in self.seen and len(self.seen) >= MAX_TRACKED:
            self.seen = {c: a for c, a in self.seen.items() if self._recent(c)}
            if len(self.seen) >= MAX_TRACKED:
                self.seen.pop(next(iter(self.seen)))  # still full: forget the oldest
        attempts = self._recent(client)
        attempts.append(self.clock())
        self.seen[client] = attempts


def require_admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    """FastAPI dependency: returns the actor name for the audit log, or refuses the call."""
    settings: Settings = request.app.state.settings
    if settings.admin_token is None:
        raise ApiError(403, "Admin API disabled", "set SERVSCOPE_ADMIN_TOKEN to enable it")
    client = request.client.host if request.client else "unknown"
    failures: FailedAttempts = request.app.state.failed_admin_attempts
    if (wait := failures.locked_for(client)) > 0:
        raise ApiError(
            429,
            "Too many wrong admin tokens",
            f"try again in {math.ceil(wait)} s",
            headers={"Retry-After": str(math.ceil(wait))},
        )
    scheme, _, given = (authorization or "").partition(" ")
    expected = settings.admin_token.get_secret_value()
    if scheme.lower() != "bearer" or not secrets.compare_digest(given.encode(), expected.encode()):
        failures.fail(client)
        raise ApiError(
            401,
            "Admin token required",
            "send Authorization: Bearer <admin token>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return f"admin@{client}"[:64]


Admin = Annotated[str, Depends(require_admin)]


async def audit(conn: AsyncConnection, actor: str, action: str, **details: Any) -> None:
    params = {"actor": actor, "action": action, "details": json.dumps(details, default=str)}
    await conn.execute(_AUDIT, params)
