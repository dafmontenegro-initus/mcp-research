#!/usr/bin/env python3
"""
Quick trace viewer for trajectory-mcp.

Usage:
  python3 view_traces.py              # last 20 sessions
  python3 view_traces.py session      # detail of most recent session
  python3 view_traces.py session <id> # detail of specific session
  python3 view_traces.py errors       # all errors (last 30 days)
  python3 view_traces.py slow         # slowest tools (last 7 days)
  python3 view_traces.py user <email> # sessions for one user
"""
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DB = Path(__file__).parent / "traces" / "traces.db"


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_ms(ms: int) -> str:
    return f"{ms}ms" if ms < 1000 else f"{ms/1000:.1f}s"


def connect():
    if not DB.exists():
        print("No trace DB yet — make some tool calls first.")
        sys.exit(0)
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    return conn


def cmd_sessions(conn, email_filter=None):
    q = "SELECT * FROM sessions"
    params = []
    if email_filter:
        q += " WHERE user_email = ?"
        params.append(email_filter)
    q += " ORDER BY started_at DESC LIMIT 20"

    rows = conn.execute(q, params).fetchall()
    if not rows:
        print("No sessions found.")
        return

    print(f"\n{'START':<20} {'USER':<35} {'CO':<12} {'CALLS':>5} {'ERR':>4} {'SLOW':>5} {'SERVER TIME'}")
    print("─" * 100)
    for s in rows:
        duration = int((s["last_call_at"] - s["started_at"]) / 60)
        user = (s["user_email"] or s["client_ip"] or "anon")[:34]
        companies = (s["companies"] or "?")[:11]
        errs = f"  {s['error_count']}✗" if s["error_count"] else ""
        slow = f"  {s['slow_calls']}⚡" if s["slow_calls"] else ""
        total_ms = s["total_duration_ms"] or 0
        sid_hint = f"  [{s['session_id'].split('_')[-1]}]"
        print(
            f"{fmt_time(s['started_at']):<20} {user:<35} {companies:<12} "
            f"{s['total_calls']:>5} {errs or '':>4} {slow or '':>5}  "
            f"{fmt_ms(total_ms):>8}  {duration}m{sid_hint}"
        )


def cmd_session_detail(conn, session_id=None):
    if session_id:
        s = conn.execute("SELECT * FROM sessions WHERE session_id = ?", [session_id]).fetchone()
    else:
        s = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()

    if not s:
        print("Session not found.")
        return

    duration_s = int(s["last_call_at"] - s["started_at"])
    companies = s["companies"] or "?"
    total_ms = s["total_duration_ms"] or 0
    slow = s["slow_calls"] or 0

    print(f"\n{'═'*75}")
    print(f"  SESSION  {s['session_id']}")
    print(f"  User:    {s['user_email'] or s['client_ip'] or 'anon'}")
    print(f"  Company: {companies}")
    print(f"  Start:   {fmt_time(s['started_at'])}")
    print(f"  Wall:    {duration_s}s  |  Server time: {fmt_ms(total_ms)}  |  {s['total_calls']} calls  |  {s['error_count']} errors  |  {slow} slow")
    print(f"{'─'*75}")
    print(f"  {'T+':>7}  {'TOOL':<35} {'CO':<6} {'RESULT':<22} {'TIME':>7}  ")
    print(f"{'─'*75}")

    calls = conn.execute(
        "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY t_ms",
        [s["session_id"]]
    ).fetchall()

    for c in calls:
        status = "✓" if c["ok"] else "✗"
        summary = (c["result_summary"] or "")[:21]
        co = (c["company_id"] or "")[:5]
        slow_flag = " ⚡" if (c["duration_ms"] or 0) > 2000 else "  "
        print(f"  {fmt_ms(c['t_ms']):>7}  {c['tool']:<35} {co:<6} {summary:<22} {fmt_ms(c['duration_ms']):>7}{slow_flag}{status}")
        if not c["ok"] and c["error_msg"]:
            print(f"           ↳ ERROR: {c['error_msg'][:65]}")
    print(f"{'═'*75}\n")


def cmd_errors(conn):
    since = time.time() - 30 * 86400
    rows = conn.execute(
        "SELECT tc.*, s.user_email FROM tool_calls tc "
        "LEFT JOIN sessions s ON tc.session_id = s.session_id "
        "WHERE tc.ok = 0 AND tc.called_at > ? ORDER BY tc.called_at DESC",
        [since]
    ).fetchall()

    if not rows:
        print("No errors in the last 30 days.")
        return

    print(f"\n{'TIME':<20} {'USER':<30} {'CO':<6} {'TOOL':<30} ERROR")
    print("─" * 110)
    for c in rows:
        user = (c["user_email"] or "anon")[:29]
        co = (c["company_id"] or "")[:5]
        print(f"{fmt_time(c['called_at']):<20} {user:<30} {co:<6} {c['tool']:<30} {c['error_msg'][:40]}")


def cmd_slow(conn):
    since = time.time() - 7 * 86400
    rows = conn.execute(
        "SELECT tool, company_id, CAST(AVG(duration_ms) AS INTEGER) avg_ms, "
        "MAX(duration_ms) max_ms, COUNT(*) n, "
        "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) errors "
        "FROM tool_calls WHERE called_at > ? "
        "GROUP BY tool, company_id ORDER BY avg_ms DESC",
        [since]
    ).fetchall()

    if not rows:
        print("No data for the last 7 days.")
        return

    print(f"\n{'TOOL':<35} {'CO':<6} {'AVG':>8} {'MAX':>8} {'CALLS':>6} {'ERRORS':>7}")
    print("─" * 80)
    for r in rows:
        errs = f"{r['errors']}✗" if r["errors"] else ""
        co = (r["company_id"] or "")[:5]
        print(f"{r['tool']:<35} {co:<6} {fmt_ms(r['avg_ms']):>8} {fmt_ms(r['max_ms']):>8} {r['n']:>6} {errs:>7}")


def main():
    conn = connect()
    args = sys.argv[1:]

    if not args:
        cmd_sessions(conn)
    elif args[0] == "session":
        cmd_session_detail(conn, args[1] if len(args) > 1 else None)
    elif args[0] == "errors":
        cmd_errors(conn)
    elif args[0] == "slow":
        cmd_slow(conn)
    elif args[0] == "user" and len(args) > 1:
        cmd_sessions(conn, email_filter=args[1])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
