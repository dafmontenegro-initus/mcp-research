from __future__ import annotations

from config import validate_company, wrike_table
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


def find_task(query: str, company_id: str, limit: int | None = None) -> dict:
    """
    Search Wrike tasks by partial title match.

    Use this to locate a specific ticket when you know part of its title. The
    primary use case is finding the baseline WSR ticket — search for
    "Weekly Status Report", "Status Report", or "Status Meeting" and pick the
    most recent one whose title contains a date. Also useful for finding a
    parent/container ticket by name. Results are ordered by updated_date DESC
    so the most recently active match comes first.

    Returns ticket_id, title, status, custom_status, responsible, dates, permalink.

    Volume reasoning: without limit, returns all LIKE matches — fine for specific or
    unique titles, potentially broad for generic keywords. For baseline WSR searches,
    limit=5 is usually sufficient.

    Parameters
    ----------
    query      : Partial title to search for (case-insensitive LIKE match).
    company_id : Company identifier (e.g. "NWN", "DAI").
    limit      : Max results. If omitted, returns all matching rows.
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

    table = wrike_table(company_id)
    sql = (
        f"SELECT {_FIND_COLUMNS} FROM {table} "
        "WHERE title LIKE %s "
        "ORDER BY updated_date DESC"
    )
    params: list = [f"%{query}%"]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    conn = get_wrike_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
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
    limit: int | None = None,
) -> dict:
    """
    Filter Wrike tasks by any combination of status, dates, assignee, or title.

    This is the primary data-gathering tool for WSR. Run the five passes below in
    parallel, collect all unique ticket_ids, then call get_task_details once with
    the full deduplicated set.

    WSR 5-pass pattern:

      Pass 1 — Baseline tickets (structure template):
        list_tasks(title_keyword="<keyword from baseline title>", company_id=X)
        → use get_task_details on all results to read description + comments

      Pass 2 — New tickets created this cycle:
        list_tasks(created_after=cutoff_date, status=["Active","Deferred"], company_id=X)

      Pass 3 — Closed this cycle:
        list_tasks(updated_after=cutoff_date, status=["Completed","Cancelled"], company_id=X)

      Pass 4 — Any activity this cycle (delta cross-check):
        list_tasks(updated_after=cutoff_date, company_id=X)

      Pass 5 — Overdue:
        list_tasks(due_before=<today>, status=["Active","Deferred"], company_id=X)

    cutoff_date is extracted from the baseline ticket title (e.g. "— 2026-04-22" → "2026-04-22").
    After all passes: deduplicate ticket_ids and call get_task_details once with the full set.

    Volume reasoning: passes with updated_after and no other filters can return many rows.
    Start with limit=100–200 to calibrate; omit limit when the scope is already bounded
    by tight date ranges or specific status/keyword filters.

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
    limit         : Max results. If omitted, returns all matching rows. Be mindful of
                    result size on broad queries (no status/date filters).
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

    table = wrike_table(company_id)

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
    sql = f"SELECT {_LIST_COLUMNS} FROM {table} {where} ORDER BY updated_date DESC"
    if limit is not None:
        sql += " LIMIT %s"
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

    Call this after list_tasks or find_task to get the fields needed to write
    meaningful WSR content: description (full task body) and comments (latest
    activity thread). Without these, you only have titles and statuses.

    Returns all fields: title, status, custom_status, importance, responsible,
    all date fields, description, comments, permalink, and paths (Wrike folder
    hierarchy — useful for identifying parent tickets and project structure).

    Volume reasoning: no batch limit enforced — caller decides. Keep in mind that
    description + comments can be several hundred tokens per ticket; factor this
    into batch size decisions for large ticket sets.

    Parameters
    ----------
    ticket_ids : List of Wrike ticket IDs (e.g. ["IEAAAAAA3ABCDEF1", ...]).
    company_id : Company identifier.
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}
    if not ticket_ids:
        return {"message": "No ticket IDs provided."}

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

    Call this at the start of any session where you'll filter by responsible in
    list_tasks. The responsible field is free text and may have name variants
    (e.g. "John D." vs "John Doe") — this tool gives you the exact strings as
    stored in the DB so your filters produce consistent results.

    Parameters
    ----------
    company_id : Company identifier (e.g. "NWN", "DAI").
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

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
