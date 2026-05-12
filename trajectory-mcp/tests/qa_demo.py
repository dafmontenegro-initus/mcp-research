#!/usr/bin/env python3
"""
Demo QA — trajectory-mcp WSR readiness test.

Run from trajectory-mcp/ with the MCP venv active:
    python3 tests/qa_demo.py [company_id]

Default company: NWN. Pass a different code (e.g. CAS) to test another.

Exit codes:
  0 — all checks passed or warned (no blockers)
  1 — one or more FAIL (demo-blocking issues found)
"""
from __future__ import annotations

import sys
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Add trajectory-mcp/ to sys.path so `tools.*` imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Output helpers ────────────────────────────────────────────────────────────

PASS  = "  ✓ PASS "
FAIL  = "  ✗ FAIL "
WARN  = "  ⚠ WARN "
SKIP  = "  - SKIP "
INFO  = "    INFO "

_results: list[tuple[str, str]] = []

def _p(level: str, msg: str) -> None:
    print(f"{level}{msg}")
    _results.append((level.strip(), msg))

def _section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")

def _summary() -> int:
    fails  = [m for l, m in _results if "FAIL"  in l]
    warns  = [m for l, m in _results if "WARN"  in l]
    passes = [m for l, m in _results if "PASS"  in l]
    print(f"\n{'═' * 60}")
    print(f"  SUMMARY: {len(passes)} passed   {len(warns)} warnings   {len(fails)} FAILED")
    print(f"{'═' * 60}")
    if fails:
        print("\n  BLOCKING ISSUES (fix before demo):")
        for m in fails:
            print(f"    ✗ {m}")
    if warns:
        print("\n  WARNINGS (non-blocking, but check):")
        for m in warns:
            print(f"    ⚠ {m}")
    if not fails:
        print("\n  ✓  System is demo-ready for this company.\n")
    else:
        print("\n  ✗  Demo blocked — fix FAIL items above.\n")
    return 1 if fails else 0


# ── Imports ───────────────────────────────────────────────────────────────────

try:
    import httpx
    from tools.wrike import find_task, list_tasks, get_task_details, get_wrike_users
    from tools.meetings import (
        list_meetings, get_meeting_details, get_meeting_transcript,
        get_meeting_participants, get_meeting_chat,
    )
    from tools.bamboohr import get_time_off
except ImportError as e:
    print(f"Import error: {e}")
    print("Run: pip install -r requirements.txt  (in MCP server venv)")
    sys.exit(1)


# ── 1. Wrike connectivity ─────────────────────────────────────────────────────

def check_wrike(company: str) -> None:
    _section(f"Wrike — {company}")

    r = find_task("Status", company, limit=5)
    if "error" in r:
        _p(FAIL, f"Wrike DB error: {r['error']}")
        return
    _p(PASS, f"Wrike DB reachable for {company}")

    # Count total tasks
    r2 = list_tasks(company, limit=200)
    total = r2.get("total", len(r2.get("tasks", [])))
    if total == 0:
        _p(FAIL, f"No tasks found in {company} Wrike workspace")
    else:
        _p(PASS, f"{total} tasks in {company} workspace")

    # Wrike users
    r3 = get_wrike_users(company)
    users = r3.get("users", [])
    if users:
        _p(PASS, f"{len(users)} distinct assignees found")
        _p(INFO, f"Sample: {users[:3]}")
    else:
        _p(WARN, "No assignees found (all tickets unassigned?)")


# ── 2. WSR Baseline identification ───────────────────────────────────────────

def check_baseline(company: str) -> str | None:
    """Returns cutoff_date string if baseline found, None otherwise."""
    _section(f"WSR Baseline — {company}")

    WSR_KEYWORDS = ["weekly", "status", "report", "meeting", "wsr", "update", "summary", "recap", "internal"]
    DATE_PATTERN = re.compile(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}"  # "May 07"
        r"|\d{4}-\d{2}-\d{2}"   # "2026-05-09"
        r"|\d{1,2}/\d{1,2}",    # "05/07"
        re.IGNORECASE,
    )

    # Phase 1 — broad search
    r = list_tasks(company, title_keyword="Internal", limit=200)
    tasks = r.get("tasks", [])
    if not tasks:
        r = list_tasks(company, limit=200)
        tasks = r.get("tasks", [])

    if not tasks:
        _p(FAIL, "No tasks returned at all — Wrike empty or DB error")
        return None

    # Phase 2 — classify
    # Step 1: exclude by known Cerebro folder patterns (path-based).
    #   NWN: "Auto Status Notes/Auto Status Meetings"
    #   BMC and newer: folders tagged with "[AI]"
    def is_cerebro_path(t: dict) -> bool:
        paths = t.get("paths", "") or ""
        return "Auto Status" in paths or "[AI]" in paths

    non_cerebro = [t for t in tasks if not is_cerebro_path(t)]

    # Step 2: within the path-filtered set, titles that appear 3+ times are still
    # Cerebro duplicates (e.g. a company whose AI folder doesn't match the patterns above).
    from collections import Counter
    title_counts = Counter(t.get("title", "") for t in non_cerebro)

    candidates = []
    for t in non_cerebro:
        title = t.get("title", "")
        has_kw = any(k in title.lower() for k in WSR_KEYWORDS)
        has_date = bool(DATE_PATTERN.search(title))
        is_duplicate = title_counts[title] >= 3
        if has_kw and has_date and not is_duplicate:
            candidates.append(t)

    if not candidates:
        _p(FAIL, f"No WSR baseline candidate found (searched {len(tasks)} tasks)")
        _p(INFO, f"Searched for WSR keywords + date in title")
        return None

    # Phase 3 — pick most recent
    today = date.today()

    def title_date(t: dict) -> date | None:
        m = DATE_PATTERN.search(t.get("title", ""))
        if not m:
            return None
        s = m.group(0)
        for fmt in ("%Y-%m-%d", "%b %d", "%B %d", "%m/%d", "%m-%d"):
            try:
                d = datetime.strptime(s, fmt).date()
                if d.year == 1900:
                    d = d.replace(year=today.year)
                return d
            except ValueError:
                continue
        return None

    valid = [(title_date(t), t) for t in candidates if title_date(t)]
    past  = [(d, t) for d, t in valid if d <= today]

    if not past:
        _p(WARN, "All candidates have future dates — using most recent updated")
        past = valid

    baseline_date, baseline = max(past, key=lambda x: x[0])
    cutoff = baseline_date.isoformat()

    _p(PASS, f"Baseline found: «{baseline['title']}»")
    _p(INFO, f"  cutoff_date = {cutoff}   permalink = {baseline.get('permalink','?')}")
    _p(INFO, f"  status = {baseline['status']}   updated = {baseline.get('updated_date','?')}")

    # Coherence check: created_date shouldn't be 7+ days AFTER title date
    cd = baseline.get("created_date")
    if cd:
        try:
            cd_date = datetime.fromisoformat(str(cd)).date()
            delta = (cd_date - baseline_date).days
            if delta > 7:
                _p(WARN, f"Coherence: created_date {cd_date} is {delta} days after title date — suspicious")
        except Exception:
            pass

    return cutoff


# ── 3. Wrike tasks since cutoff ───────────────────────────────────────────────

def check_tasks_delta(company: str, cutoff: str) -> None:
    _section(f"Wrike task delta since {cutoff}")

    passes_results = {}
    for label, kwargs in [
        ("new",     {"created_after": cutoff, "status": ["Active", "Deferred"]}),
        ("closed",  {"updated_after": cutoff, "status": ["Completed", "Cancelled"]}),
        ("updated", {"updated_after": cutoff}),
        ("overdue", {"due_before": date.today().isoformat(), "status": ["Active", "Deferred"]}),
    ]:
        r = list_tasks(company, limit=50, **kwargs)
        count = len(r.get("tasks", []))
        passes_results[label] = count
        level = PASS if count > 0 else WARN
        _p(level, f"Pass '{label}': {count} tasks")

    total_unique = sum(passes_results.values())
    if total_unique == 0:
        _p(WARN, "Zero tasks across all passes — WSR will be empty")
    else:
        _p(PASS, f"~{total_unique} task references found (before deduplication)")

    # Sample: get details for 3 tasks to verify field quality
    r = list_tasks(company, updated_after=cutoff, limit=3)
    tasks = r.get("tasks", [])
    if tasks:
        ids = [t["ticket_id"] for t in tasks]
        rd = get_task_details(ids, company)
        detail_tasks = rd.get("tasks", [])
        for t in detail_tasks[:2]:
            has_desc = bool(t.get("description", "").strip())
            has_comments = bool(t.get("comments", "").strip())
            _p(PASS if has_desc else WARN,
               f"  Task «{t['title'][:50]}» — desc={'✓' if has_desc else '✗'}  comments={'✓' if has_comments else '✗'}")


# ── 4. Meetings ───────────────────────────────────────────────────────────────

def check_meetings(company: str, cutoff: str) -> str | None:
    """Returns a sample meeting UUID with good data, or None."""
    _section(f"Meetings since {cutoff} — {company}")

    r = list_meetings(company, start_after=cutoff, limit=50)
    if "error" in r:
        _p(FAIL, f"Meetings DB error: {r['error']}")
        return None

    meetings = r.get("meetings", [])
    if not meetings:
        _p(WARN, f"No meetings found since {cutoff}")
        return None

    total   = len(meetings)
    w_synth = [m for m in meetings if m.get("has_synthesis")]
    w_tx    = [m for m in meetings if m.get("has_transcript")]

    _p(PASS, f"{total} meetings found since {cutoff}")

    if not w_synth and not w_tx:
        _p(WARN, "None of these meetings have synthesis or transcripts yet")
        _p(INFO, "The daemon may not have processed them yet — check older periods")
    else:
        _p(PASS, f"  {len(w_synth)} with synthesis   {len(w_tx)} with transcript")

    # Test get_meeting_details on first meeting
    sample_uuid = meetings[0]["meeting_uuid"]
    rd = get_meeting_details([sample_uuid], company)
    detail_meetings = rd.get("meetings", [])
    if detail_meetings:
        m = detail_meetings[0]
        _p(PASS, f"get_meeting_details OK for {sample_uuid[:8]}...")
        _p(INFO, f"  title: {m.get('meeting_title','')[:60]}")
        _p(INFO, f"  synthesized_meeting: {'✓' if (m.get('synthesized_meeting') or '').strip() else '✗ (empty)'}")
        _p(INFO, f"  zoom_summary: {'✓' if (m.get('zoom_summary') or '').strip() else '✗ (empty)'}")
    else:
        _p(WARN, f"get_meeting_details returned empty for {sample_uuid[:8]}...")

    # Test get_meeting_participants
    rp = get_meeting_participants(sample_uuid, company)
    count = rp.get("total", 0)
    if count > 0:
        _p(PASS, f"get_meeting_participants: {count} participants")
    else:
        _p(WARN, "get_meeting_participants: 0 participants (table may be empty for this meeting)")

    # Test get_meeting_chat
    rc = get_meeting_chat(sample_uuid, company)
    has_chat = bool(rc.get("chat", "").strip())
    _p(PASS if has_chat else INFO,
       f"get_meeting_chat: {'chat data present' if has_chat else 'no chat (normal for this meeting)'}")

    # Return a meeting UUID with synthesis if available
    best = w_synth[0]["meeting_uuid"] if w_synth else sample_uuid
    return best


# ── 5. Transcript ─────────────────────────────────────────────────────────────

def check_transcript(company: str, uuid: str) -> None:
    _section(f"Transcript — {uuid[:12]}...")

    r = get_meeting_transcript(uuid, company)
    if "error" in r:
        _p(WARN, f"Transcript error: {r['error']}")
        return
    if r.get("message"):
        _p(INFO, f"No transcript in S3: {r['message']}")
        return

    chars = r.get("total_chars", 0)
    truncated = r.get("truncated", False)
    _p(PASS, f"Transcript loaded: {chars:,} chars {'(truncated — pagination needed)' if truncated else '(complete)'}")


# ── 6. BambooHR ───────────────────────────────────────────────────────────────

def check_bamboohr() -> None:
    _section("BambooHR (global — all companies)")

    r = get_time_off()
    if "error" in r:
        _p(WARN, f"BambooHR time_off error: {r['error']}")
    else:
        count = r.get("total", 0)
        _p(PASS if count >= 0 else WARN,
           f"get_time_off: {count} entries for {r.get('window_start','')} → {r.get('window_end','')}")
        if count > 0:
            _p(INFO, f"  Sample: {r['entries'][0].get('name','?')} ({r['entries'][0].get('start','')} – {r['entries'][0].get('end','')})")


# ── 7. RAG service ────────────────────────────────────────────────────────────

def check_rag(company: str, cutoff: str) -> None:
    _section(f"RAG service — semantic search")
    import os
    url = os.getenv("RAG_SERVICE_URL", "http://localhost:8090")

    try:
        r = httpx.get(f"{url}/health", timeout=5.0)
        data = r.json()
        _p(PASS, f"RAG service running — ollama:{data.get('ollama')} reranker:{data.get('reranker')}")
    except httpx.ConnectError:
        _p(SKIP, f"RAG service not running at {url} — start rag_service/app.py to test semantic search")
        return

    # Test semantic search for tasks
    try:
        t0 = time.monotonic()
        r2 = httpx.post(
            f"{url}/search/tasks",
            json={"query": "deployment risk blockers", "company_id": company, "top_k": 5},
            timeout=60.0,
        )
        elapsed = (time.monotonic() - t0) * 1000
        data2 = r2.json()
        results = data2.get("results", [])
        if results:
            _p(PASS, f"search_tasks: {len(results)} results in {elapsed:.0f}ms (first load may be slow)")
            _p(INFO, f"  Rank 1 excerpt: {results[0].get('excerpt','')[:80]}...")
        elif data2.get("message"):
            _p(WARN, f"search_tasks: {data2['message']}")
        else:
            _p(WARN, f"search_tasks: empty results ({elapsed:.0f}ms)")
    except Exception as e:
        _p(WARN, f"search_tasks failed: {e}")

    # Test semantic search for meetings
    try:
        t0 = time.monotonic()
        r3 = httpx.post(
            f"{url}/search/meetings",
            json={"query": "budget approval status update", "company_id": company, "top_k": 5},
            timeout=60.0,
        )
        elapsed = (time.monotonic() - t0) * 1000
        data3 = r3.json()
        results3 = data3.get("results", [])
        if results3:
            _p(PASS, f"search_meetings: {len(results3)} results in {elapsed:.0f}ms")
        elif data3.get("message"):
            _p(WARN, f"search_meetings: {data3['message']}")
        else:
            _p(WARN, f"search_meetings: empty results")
    except Exception as e:
        _p(WARN, f"search_meetings failed: {e}")


# ── 8. Multi-company validation ───────────────────────────────────────────────

def check_companies() -> None:
    _section("Available companies (server scope)")
    from db import get_meet_conn, get_wrike_conn
    import pymysql

    # Wrike companies
    try:
        conn = get_wrike_conn()
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES FROM wrike LIKE '%_FULL'")
            wrike_cos = {r[list(r.keys())[0]].replace("_FULL", "") for r in cur.fetchall()}
        conn.close()
        _p(PASS, f"Wrike companies ({len(wrike_cos)}): {sorted(wrike_cos)}")
    except Exception as e:
        _p(FAIL, f"Cannot list Wrike companies: {e}")
        wrike_cos = set()

    # Meetings companies
    try:
        conn = get_meet_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT project_id FROM meetings_assets.meetings_projects "
                "WHERE project_id IS NOT NULL ORDER BY project_id"
            )
            meet_cos = {r["project_id"] for r in cur.fetchall() if r["project_id"]}
        conn.close()
        _p(PASS, f"Meetings companies ({len(meet_cos)}): {sorted(meet_cos)}")
    except Exception as e:
        _p(FAIL, f"Cannot list meetings companies: {e}")
        meet_cos = set()

    # Find intersection (full-featured companies)
    full = wrike_cos & meet_cos
    _p(PASS if full else WARN,
       f"Companies with BOTH Wrike + Meetings data ({len(full)}): {sorted(full)}")

    wrike_only = wrike_cos - meet_cos
    meet_only  = meet_cos  - wrike_cos
    if wrike_only:
        _p(INFO, f"Wrike-only (no meetings data): {sorted(wrike_only)}")
    if meet_only:
        _p(INFO, f"Meetings-only (no Wrike data): {sorted(meet_only)}")


# ── 9. Security spot-check ────────────────────────────────────────────────────

def check_security() -> None:
    _section("Security spot-check")

    base = Path(__file__).parent.parent

    # .env not tracked
    import subprocess
    r = subprocess.run(
        ["git", "ls-files", "trajectory-mcp/.env"],
        cwd=base.parent, capture_output=True, text=True
    )
    if r.stdout.strip():
        _p(FAIL, ".env is tracked by git — run: git rm --cached trajectory-mcp/.env")
    else:
        _p(PASS, ".env is NOT tracked by git")

    # .env.example has no real values
    env_example = base / ".env.example"
    if env_example.exists():
        content = env_example.read_text()
        import re
        # Look for actual key patterns
        if re.search(r"AKIA[0-9A-Z]{16}", content):
            _p(FAIL, ".env.example contains what looks like a real AWS Access Key ID")
        elif re.search(r"password\s*=\s*[^\s$]{6,}", content, re.IGNORECASE):
            _p(WARN, ".env.example may contain a non-empty password — verify it's a placeholder")
        else:
            _p(PASS, ".env.example has empty/placeholder values only")

    # Source files have no hardcoded credentials
    suspicious = []
    for f in base.rglob("*.py"):
        if ".venv" in str(f) or "__pycache__" in str(f):
            continue
        text = f.read_text(errors="ignore")
        if re.search(r"AKIA[0-9A-Z]{16}", text):
            suspicious.append(str(f.relative_to(base)))
        if re.search(r"""password\s*=\s*['"][^'"]{8,}['"]""", text, re.IGNORECASE):
            suspicious.append(str(f.relative_to(base)) + " (possible hardcoded password)")

    if suspicious:
        _p(FAIL, f"Possible hardcoded credentials in: {suspicious}")
    else:
        _p(PASS, "No hardcoded credentials found in .py source files")

    # Confirm gitignore covers secrets (check both trajectory-mcp and rag_service gitignores)
    all_gi = ""
    for gi_path in [base / ".gitignore", base / "rag_service" / ".gitignore"]:
        if gi_path.exists():
            all_gi += gi_path.read_text()
    for secret_pattern in [".env", "chroma_data", ".venv"]:
        if secret_pattern in all_gi:
            _p(PASS, f".gitignore covers '{secret_pattern}'")
        else:
            _p(WARN, f".gitignore does NOT cover '{secret_pattern}'")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    company = sys.argv[1].upper() if len(sys.argv) > 1 else "NWN"

    print(f"\n{'═' * 60}")
    print(f"  trajectory-mcp — Demo QA ({company})")
    print(f"  {date.today()}")
    print(f"{'═' * 60}")

    check_companies()
    check_security()
    check_bamboohr()
    check_wrike(company)
    cutoff = check_baseline(company)
    if cutoff:
        check_tasks_delta(company, cutoff)
        sample_uuid = check_meetings(company, cutoff)
        if sample_uuid:
            check_transcript(company, sample_uuid)
    check_rag(company, cutoff or (date.today() - timedelta(days=7)).isoformat())

    sys.exit(_summary())
