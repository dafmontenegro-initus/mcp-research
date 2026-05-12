from __future__ import annotations

import io
import os
import pickle

import boto3
import httpx
from pymysql.err import ProgrammingError

from config import AWS_REGION, RAG_SERVICE_URL, S3_WRIKE_BUCKET, validate_company, wrike_table
from db import get_wrike_conn
from utils import fit_to_limit

_DATE_FIELDS = ("due_date", "start_date", "created_date", "updated_date")

_DETAIL_COLUMNS = (
    "ticket_id, title, status, custom_status, importance, responsible, "
    "due_date, start_date, created_date, updated_date, "
    "description, comments, permalink, paths"
)

_LIST_COLUMNS = (
    "ticket_id, title, status, custom_status, importance, responsible, "
    "due_date, created_date, updated_date, permalink, paths"
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
    except ProgrammingError:
        return {"error": f"No Wrike data available for company {company_id.upper()}."}
    finally:
        conn.close()

    if not rows:
        return {"message": f"No tasks found matching '{query}' for company {company_id}."}

    return fit_to_limit(_serialize(rows), "tasks", {"total": len(rows)})


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
    except ProgrammingError:
        return {"error": f"No Wrike data available for company {company_id.upper()}."}
    finally:
        conn.close()

    if not rows:
        return {"message": "No tasks found for the given filters."}

    return fit_to_limit(_serialize(rows), "tasks", {"total": len(rows)})


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
    except ProgrammingError:
        return {"error": f"No Wrike data available for company {company_id.upper()}."}
    finally:
        conn.close()

    if not rows:
        return {"message": "No tasks found for the given ticket IDs."}

    return fit_to_limit(_serialize(rows), "tasks", {"total": len(rows)})


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
    except ProgrammingError:
        return {"error": f"No Wrike data available for company {company_id.upper()}."}
    finally:
        conn.close()

    if not rows:
        return {"message": f"No users found for company {company_id}."}

    users = [row["responsible"] for row in rows]
    return {"total": len(users), "users": users}


def search_tasks(query: str, company_id: str, limit: int = 10) -> dict:
    """
    Semantic search over Wrike tasks (titles, descriptions, comments, attachment text).

    Use this to find tickets related to a topic even when the exact wording differs or
    you don't know which ticket to look in. Results are ordered by relevance (rank 1 = most
    relevant). The rank order is the signal — do not treat rank numbers as absolute scores.

    The search corpus includes both ticket content and processed attachment text (PDFs,
    docs) that the daemons extracted and embedded locally. This means a query about
    "network topology diagram" can surface tickets whose attachments contain that content
    even if the ticket title doesn't mention it.

    Complements list_tasks (exact SQL filters) for thematic or cross-cutting discovery.
    Especially useful in WSR for finding related tickets that weren't updated recently
    but are semantically connected to a topic raised in a meeting.

    Parameters
    ----------
    query      : Natural language search query (e.g. "budget approval", "deployment risk").
    company_id : Company identifier — results are isolated to this tenant.
    limit      : Maximum results to return (default 10).
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

    try:
        r = httpx.post(
            f"{RAG_SERVICE_URL}/search/tasks",
            json={"query": query, "company_id": company_id.upper(), "top_k": limit},
            timeout=30.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        return {"message": "RAG service unavailable. Start rag_service/app.py first.", "results": []}
    except Exception as e:
        return {"error": f"RAG search failed: {e}", "results": []}


def get_task_attachment_content(ticket_id: str, company_id: str) -> dict:
    """
    Retrieve the processed attachment text for a Wrike ticket from S3.

    The Wrike daemon extracted text from PDFs, images (via vision model), and documents
    attached to each ticket and stored the result as an embedding pickle in S3. This tool
    loads that pickle and returns the full extracted text — letting you read attachment
    content without any external API calls at query time.

    Use this when:
    - A ticket has important attachments (specs, reports, diagrams) you need to read
    - search_tasks surfaces a ticket but get_task_details doesn't explain why it ranked high
    - You need to verify what a document says vs. what the ticket description claims

    Returns the concatenated chunk_text from the pickle. May be empty if the daemon
    hasn't processed this ticket yet, or if it has no extractable attachments.

    Parameters
    ----------
    ticket_id  : Wrike ticket ID (e.g. "IEAAAAAA3ABCDEF1").
    company_id : Company identifier (used to construct the S3 prefix).
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

    bucket = S3_WRIKE_BUCKET
    key = f"wrike/{company_id.upper()}/{ticket_id}/{ticket_id}.pkl"

    s3 = boto3.client("s3", region_name=AWS_REGION)
    try:
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return {"message": f"No attachment pickle found for ticket {ticket_id}.", "content": ""}
    except Exception as e:
        return {"error": f"Cannot read S3 {bucket}/{key}: {e}"}

    try:
        import pandas as pd
        df: pd.DataFrame = pickle.loads(raw)
        text_col = next((c for c in ("chunk_text", "text", "content") if c in df.columns), None)
        if not text_col:
            return {"message": "Pickle has no recognizable text column.", "content": ""}
        texts = [str(v) for v in df[text_col].dropna() if str(v).strip()]
        content = "\n\n".join(texts)
    except Exception as e:
        return {"error": f"Failed to parse attachment pickle: {e}"}

    if not content.strip():
        return {"ticket_id": ticket_id, "message": "Attachment pickle is empty.", "content": ""}

    return {"ticket_id": ticket_id, "chars": len(content), "content": content}


def ingest_document(s3_key: str, company_id: str, ticket_id: str) -> dict:
    """
    Ingest a new document from S3 into the local RAG index on demand.

    Sends a request to the local RAG service to fetch a document from S3, extract its
    text (PDF via PyMuPDF, or plain text), chunk it, embed it with the local Ollama model,
    and store it in ChromaDB. After ingestion the document is immediately searchable via
    search_tasks.

    This demonstrates local AI processing capabilities: the entire pipeline runs on
    machine_20 with no external API calls — Ollama handles embedding locally.

    Use this when:
    - A new attachment has been uploaded to S3 and needs to be indexed immediately
    - You want to make a specific document searchable before the daemon's next run

    Image ingestion is not yet supported (returns an informative error). Use doc_type="pdf"
    for PDFs and doc_type="text" for plain text files.

    Parameters
    ----------
    s3_key     : S3 path in "bucket/key" format (e.g. "assets-tj-prod/wrike/NWN/specs.pdf").
    company_id : Company identifier — document will be indexed under this tenant.
    ticket_id  : Wrike ticket ID to associate the document with.
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

    try:
        r = httpx.post(
            f"{RAG_SERVICE_URL}/ingest/document",
            json={
                "s3_key": s3_key,
                "company_id": company_id.upper(),
                "ticket_id": ticket_id,
            },
            timeout=120.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        return {"message": "RAG service unavailable. Start rag_service/app.py first."}
    except Exception as e:
        return {"error": f"Ingest request failed: {e}"}
