"""
Bearer token authentication middleware for trajectory-mcp.

Users are defined in users.json (not committed to git):
  { "maria.alba@initus.io": "traj_xK9mN2pQ8rL4", ... }

Each user adds to their Claude Desktop config:
  "http://<host>:8080/mcp?token=<their-token>"

Every MCP request is validated. Unknown or missing tokens are rejected.
The authenticated email is stored in _current_user for trace_db to read.

Session tracking: on every MCP initialize message a new UUID is generated
and stored in _session_ids[email]. trace_db reads this to assign a real
session boundary (one session = one Claude Desktop connection), not a
coarse time bucket.
"""
from __future__ import annotations

import json
import secrets
from contextvars import ContextVar
from pathlib import Path

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.middleware import Middleware, MiddlewareContext

_USERS_FILE = Path(__file__).parent / "users.json"

# Set by middleware on every authenticated request; read by trace_db.
_current_user: ContextVar[str] = ContextVar("current_user", default="")

# Maps email → session UUID. Refreshed on every MCP initialize message.
# One entry per user; concurrent users are isolated by email key.
_session_ids: dict[str, str] = {}


def _load_token_map() -> dict[str, str]:
    """Return {token: email}. Reloaded on every server start (not hot-reloaded)."""
    if not _USERS_FILE.exists():
        return {}
    try:
        data: dict[str, str] = json.loads(_USERS_FILE.read_text())
        return {token: email for email, token in data.items()}
    except Exception as e:
        print(f"[auth] WARNING: could not load users.json: {e}")
        return {}


class BearerAuthMiddleware(Middleware):
    """Validates Authorization: Bearer <token> on every MCP message."""

    def __init__(self) -> None:
        self._token_map = _load_token_map()
        count = len(self._token_map)
        if count == 0:
            print("[auth] WARNING: users.json not found or empty — all requests will be rejected")
        else:
            print(f"[auth] {count} user(s) loaded from users.json")

    def _authenticate(self) -> str:
        """Read and validate the token from the current HTTP request.
        Accepts token as URL query parameter (?token=...) or Bearer header.
        Returns the user email. Raises AuthorizationError if invalid."""
        try:
            from fastmcp.server.http import _current_http_request
            request = _current_http_request.get()
            if request is None:
                raise AuthorizationError("No HTTP request context")
            # URL query param takes priority: ?token=traj_...
            token = request.query_params.get("token", "")
            # Fallback: Authorization: Bearer <token>
            if not token:
                raw = request.headers.get("authorization", "")
                token = raw.removeprefix("Bearer ").strip()
        except AuthorizationError:
            raise
        except Exception:
            raise AuthorizationError("Could not read request context")

        if not token:
            raise AuthorizationError(
                "Missing token. Add ?token=<your-token> to the server URL in your mcp-remote config."
            )

        email = self._token_map.get(token)
        if email is None:
            masked = token[:8] + "…" if len(token) > 8 else token
            try:
                from fastmcp.server.http import _current_http_request
                req = _current_http_request.get()
                ip = req.client.host if (req and req.client) else "?"
            except Exception:
                ip = "?"
            print(f"[auth] REJECTED — invalid token ({masked}) from {ip}", flush=True)
            raise AuthorizationError(
                "Invalid token. Contact your administrator to get access."
            )

        print(f"[auth] {email} authenticated", flush=True)
        return email

    async def on_message(self, context: MiddlewareContext, call_next) -> object:
        email = self._authenticate()
        _current_user.set(email)
        if context.method == "initialize":
            sid = secrets.token_hex(8)
            _session_ids[email] = sid
            print(f"[auth] {email} — new session {sid}", flush=True)
        return await call_next(context)
