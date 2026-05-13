"""
SQLite-backed trace log for trajectory-mcp.

Session boundary = MCP initialize message (one Claude Desktop connection =
one session). Session ID comes from auth._session_ids, which is refreshed
on every initialize. Falls back to a 30-min time bucket if no initialize
has been seen yet (e.g. server restarted mid-conversation).

Schema (v2):
  sessions   — one row per conversation; tracks all companies consulted,
               total server time, slow call count
  tool_calls — one row per tool call; includes company_id as indexed column
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_TRACES_DIR = Path(__file__).parent / "traces"
_DB_PATH = _TRACES_DIR / "traces.db"

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    user_email        TEXT,
    client_ip         TEXT,
    companies         TEXT    DEFAULT '',
    started_at        REAL,
    last_call_at      REAL,
    total_calls       INTEGER DEFAULT 0,
    error_count       INTEGER DEFAULT 0,
    total_duration_ms INTEGER DEFAULT 0,
    slow_calls        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT,
    called_at      REAL,
    t_ms           INTEGER,
    tool           TEXT,
    company_id     TEXT    DEFAULT '',
    args_json      TEXT,
    result_summary TEXT,
    duration_ms    INTEGER,
    ok             INTEGER,
    error_msg      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tc_session  ON tool_calls (session_id);
CREATE INDEX IF NOT EXISTS idx_tc_called   ON tool_calls (called_at);
CREATE INDEX IF NOT EXISTS idx_tc_company  ON tool_calls (company_id);
"""

# New columns added in v2 — applied if the DB was created with the old schema.
_MIGRATIONS = [
    "ALTER TABLE sessions   ADD COLUMN companies         TEXT    DEFAULT ''",
    "ALTER TABLE sessions   ADD COLUMN total_duration_ms INTEGER DEFAULT 0",
    "ALTER TABLE sessions   ADD COLUMN slow_calls        INTEGER DEFAULT 0",
    "ALTER TABLE tool_calls ADD COLUMN company_id        TEXT    DEFAULT ''",
]

SESSION_GAP_SECONDS = 1800

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_session_starts: dict[str, float] = {}


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _TRACES_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(_DDL)
        c.commit()
        for sql in _MIGRATIONS:
            try:
                c.execute(sql)
                c.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        _conn = c
    return _conn


def _read_request_context() -> tuple[str, str, str]:
    """Return (user_email, client_ip, session_uuid).
    session_uuid is the UUID stored in auth._session_ids when initialize fired.
    """
    try:
        from auth import _current_user, _session_ids
        email = _current_user.get()
        session_uuid = _session_ids.get(email, "")
    except Exception:
        email = session_uuid = ""

    try:
        from fastmcp.server.http import _current_http_request
        request = _current_http_request.get()
        ip = request.client.host if (request and request.client) else ""
    except Exception:
        ip = ""

    return email, ip, session_uuid


def _make_session_id(user_email: str, client_ip: str, session_uuid: str, now: float) -> str:
    if session_uuid:
        return f"{user_email or client_ip or 'anon'}_{session_uuid}"
    bucket = int(now // SESSION_GAP_SECONDS)
    return f"{user_email or client_ip or 'anon'}_{bucket}"


def _ensure_session(session_id: str, user_email: str, client_ip: str, now: float) -> int:
    """Create session row if new. Returns t_ms since session start."""
    if session_id not in _session_starts:
        _session_starts[session_id] = now
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO sessions "
            "(session_id, user_email, client_ip, companies, started_at, last_call_at, "
            " total_calls, error_count, total_duration_ms, slow_calls) "
            "VALUES (?, ?, ?, '', ?, ?, 0, 0, 0, 0)",
            (session_id, user_email, client_ip, now, now),
        )
        conn.commit()
    return int((now - _session_starts[session_id]) * 1000)


def _summarize(tool: str, result: object) -> str:
    """Compact human-readable summary of a tool result. Never raises."""
    try:
        if not isinstance(result, dict):
            return str(result)[:80]

        if "error" in result:
            return f"ERROR: {str(result['error'])[:80]}"

        if tool == "summarize_transcript_for_ticket":
            s = result.get("summary", "")
            return f"summary: {len(s)} chars" if s else "fallback"

        if tool == "get_task_attachment_content":
            return f"{result.get('chars', 0)} chars"

        if tool == "get_meeting_transcript":
            return f"{result.get('total_chars', 0)} chars"

        for key in ("tasks", "meetings", "results", "users", "entries",
                    "participants", "chunks", "tickets", "companies"):
            if key in result and isinstance(result[key], list):
                return f"{len(result[key])} {key}"

        for key in ("total", "found", "count"):
            if key in result:
                return f"{key}={result[key]}"

        if "message" in result:
            return str(result["message"])[:80]

        return str(result)[:80]
    except Exception:
        return "(summary error)"


def _update_companies(conn: sqlite3.Connection, session_id: str, company_id: str) -> None:
    """Add company_id to the session's companies list if not already present."""
    if not company_id:
        return
    conn.execute(
        "UPDATE sessions SET companies = CASE "
        "  WHEN companies = '' THEN ? "
        "  WHEN instr(',' || companies || ',', ',' || ? || ',') > 0 THEN companies "
        "  ELSE companies || ',' || ? "
        "END WHERE session_id = ?",
        (company_id, company_id, company_id, session_id),
    )


def record(tool: str, args: dict, result: object, duration_ms: float, *, ok: bool) -> None:
    """Record one tool call. Thread-safe. Never raises."""
    try:
        now = time.time()

        company_id = ""
        if isinstance(args, dict):
            company_id = str(args.get("company_id", "")).upper()

        user_email, client_ip, session_uuid = _read_request_context()
        session_id = _make_session_id(user_email, client_ip, session_uuid, now)

        with _lock:
            t_ms = _ensure_session(session_id, user_email, client_ip, now)
            summary = _summarize(tool, result)
            error_msg = ""
            if not ok and isinstance(result, dict):
                error_msg = str(result.get("error", ""))[:200]

            conn = _get_conn()
            conn.execute(
                "INSERT INTO tool_calls "
                "(session_id, called_at, t_ms, tool, company_id, args_json, "
                " result_summary, duration_ms, ok, error_msg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, now, t_ms, tool, company_id,
                    json.dumps(args, default=str)[:1000],
                    summary, int(duration_ms), 1 if ok else 0, error_msg,
                ),
            )
            is_slow = 1 if duration_ms > 2000 else 0
            conn.execute(
                "UPDATE sessions SET "
                "  last_call_at      = ?, "
                "  total_calls       = total_calls + 1, "
                "  error_count       = error_count + ?, "
                "  total_duration_ms = total_duration_ms + ?, "
                "  slow_calls        = slow_calls + ? "
                "WHERE session_id = ?",
                (now, 0 if ok else 1, int(duration_ms), is_slow, session_id),
            )
            _update_companies(conn, session_id, company_id)
            conn.commit()
    except Exception:
        pass
