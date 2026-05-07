from __future__ import annotations

from config import wrike_table
from db import get_wrike_conn

_DATE_FIELDS = ("due_date", "start_date", "created_date", "updated_date")

_DETAIL_COLUMNS = (
    "ticket_id, title, status, custom_status, importance, responsible, "
    "due_date, start_date, created_date, updated_date, "
    "description, comments, permalink, paths"
)

_LIST_COLUMNS = (
    "ticket_id, title, status, custom_status, importance, responsible, "
    "due_date, created_date, updated_date, permalink"
)

_FIND_COLUMNS = (
    "ticket_id, title, status, custom_status, responsible, "
    "created_date, due_date, updated_date, permalink"
)


def _serialize(rows: list[dict]) -> list[dict]:
    for row in rows:
        for f in _DATE_FIELDS:
            if row.get(f) is not None:
                row[f] = str(row[f])
    return rows


def find_task(query: str, company_id: str, limit: int = 10) -> dict:
    """
    Search Wrike tasks by partial title match.

    Use this to locate a specific ticket when you know part of its title. The
    primary use case is finding the baseline WSR ticket — search for
    "Weekly Status Report", "Status Report", or "Status Meeting" and pick the
    most recent one whose title contains a date. Also useful for finding a
    parent/container ticket by name. Results are ordered by updated_date DESC
    so the most recently active match comes first.

    Returns ticket_id, title, status, custom_status, responsible, dates, permalink.

    Parameters
    ----------
    query      : Partial title to search for (case-insensitive LIKE match).
    company_id : Company identifier (e.g. "NWN", "DAI"). Aliases resolved
                 automatically (e.g. "TJV" → "NWN").
    limit      : Max results (default 10, max 50).
    """
    table = wrike_table(company_id)
    limit = min(limit, 50)
    sql = (
        f"SELECT {_FIND_COLUMNS} FROM {table} "
        "WHERE title LIKE %s "
        "ORDER BY updated_date DESC LIMIT %s"
    )

    conn = get_wrike_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (f"%{query}%", limit))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"message": f"No tasks found matching '{query}' for company {company_id}."}

    return {"total": len(rows), "tasks": _serialize(rows)}


def list_tasks(
    company_id: str,
    status: list[str] | None = None,
    responsible: str | None = None,
    title_keyword: str | None = None,
    created_after: str | None = None,
    updated_after: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    limit: int = 100,
) -> dict:
    """
    Filter Wrike tasks by any combination of status, dates, assignee, or title.

    This is the primary data-gathering tool for WSR. Run it in five passes to
    build the complete picture of the reporting period:

      Pass 1 — Baseline tickets (template for structure):
        title_keyword="<keyword from baseline title>", limit=200

      Pass 2 — New tickets (created this cycle):
        created_after=cutoff_date, status=["Active","Deferred"]

      Pass 3 — Closed this cycle:
        updated_after=cutoff_date, status=["Completed","Cancelled"]

      Pass 4 — Any activity this cycle (delta cross-check):
        updated_after=cutoff_date

      Pass 5 — Overdue tickets:
        due_before=<today>, status=["Active","Deferred"]

    Results ordered by updated_date DESC (most recently active first).
    Returns ticket_id, title, status, custom_status, importance, responsible,
    due_date, created_date, updated_date, permalink.

    Parameters
    ----------
    company_id    : Company identifier (e.g. "NWN", "DAI").
    status        : Filter by status list, e.g. ["Active","Deferred"].
    responsible   : Filter by assignee name (partial match).
    title_keyword : Filter by partial title match.
    created_after : ISO date "YYYY-MM-DD" — tasks created on or after.
    updated_after : ISO date "YYYY-MM-DD" — tasks updated on or after.
    due_before    : ISO date "YYYY-MM-DD" — tasks due strictly before (use for overdue).
    due_after     : ISO date "YYYY-MM-DD" — tasks due on or after.
    limit         : Max results (default 100, max 500).
    """
    table = wrike_table(company_id)
    limit = min(limit, 500)

    conditions: list[str] = []
    params: list = []

    if status:
        placeholders = ", ".join(["%s"] * len(status))
        conditions.append(f"status IN ({placeholders})")
        params.extend(status)
    if responsible:
        conditions.append("responsible LIKE %s")
        params.append(f"%{responsible}%")
    if title_keyword:
        conditions.append("title LIKE %s")
        params.append(f"%{title_keyword}%")
    if created_after:
        conditions.append("created_date >= %s")
        params.append(created_after)
    if updated_after:
        conditions.append("updated_date >= %s")
        params.append(updated_after)
    if due_before:
        conditions.append("due_date < %s")
        params.append(due_before)
    if due_after:
        conditions.append("due_date >= %s")
        params.append(due_after)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        f"SELECT {_LIST_COLUMNS} FROM {table} {where} "
        "ORDER BY updated_date DESC LIMIT %s"
    )
    params.append(limit)

    conn = get_wrike_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"message": "No tasks found for the given filters."}

    return {"total": len(rows), "tasks": _serialize(rows)}


def get_task_details(ticket_ids: list[str], company_id: str) -> dict:
    """
    Fetch complete metadata for one or more Wrike tasks by ticket ID.

    Returns all fields needed to write the WSR body for each ticket:
    title, status, custom_status, importance, responsible, all date fields,
    description (full HTML/markdown content), comments (latest activity),
    permalink (direct Wrike link), and paths (position in the Wrike hierarchy —
    useful for identifying parent tickets and understanding task structure).

    Call this after list_tasks or find_task once you have the ticket_ids to fetch.
    Batch up to 100 IDs per call.

    Parameters
    ----------
    ticket_ids : List of Wrike ticket IDs (e.g. ["IEAAAAAA3ABCDEF1", ...]).
    company_id : Company identifier.
    """
    if not ticket_ids:
        return {"message": "No ticket IDs provided."}
    if len(ticket_ids) > 100:
        return {"message": "Provide at most 100 ticket IDs per call. Split into batches."}

    table = wrike_table(company_id)
    placeholders = ", ".join(["%s"] * len(ticket_ids))
    sql = f"SELECT {_DETAIL_COLUMNS} FROM {table} WHERE ticket_id IN ({placeholders})"

    conn = get_wrike_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, ticket_ids)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"message": "No tasks found for the given ticket IDs."}

    return {"total": len(rows), "tasks": _serialize(rows)}


def get_wrike_users(company_id: str) -> dict:
    """
    Return all unique assignees (responsible field) for a company's Wrike workspace.

    Use this at the start of a WSR session to know who the team members are.
    This is the authoritative list of names as they appear in the DB — use these
    exact names when attributing action items or filtering by responsible in
    list_tasks. Also useful to detect name variants (e.g. "John D." vs "John Doe").

    Parameters
    ----------
    company_id : Company identifier (e.g. "NWN", "DAI").
    """
    table = wrike_table(company_id)
    sql = (
        f"SELECT DISTINCT responsible FROM {table} "
        "WHERE responsible IS NOT NULL AND responsible != '' "
        "ORDER BY responsible ASC"
    )

    conn = get_wrike_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"message": f"No users found for company {company_id}."}

    users = [row["responsible"] for row in rows]
    return {"total": len(users), "users": users}
