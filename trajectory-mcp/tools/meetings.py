from __future__ import annotations

import logging
import time

import boto3
import httpx
from botocore.exceptions import ClientError
from config import AWS_REGION, S3_BUCKET, RAG_SERVICE_URL, OLLAMA_BASE_URL, OLLAMA_SUMMARIZE_MODEL, validate_company
from db import get_meet_conn
from utils import fit_to_limit, MAX_RESPONSE_BYTES

log = logging.getLogger(__name__)

_MAX_TRANSCRIPT_CHARS = 120_000  # ~30K tokens — safe for 32K+ context models

_s3 = boto3.client("s3", region_name=AWS_REGION)

_DETAIL_COLUMNS = (
    "meeting_uuid, meeting_title, start_time, end_time, host_email, "
    "participants_emails, peak_participants, duration, status, "
    "has_transcript, synthesized_meeting, zoom_summary"
)

_DATE_FIELDS = ("start_time", "end_time")


def _serialize(rows: list[dict], date_fields: tuple[str, ...]) -> list[dict]:
    for row in rows:
        for f in date_fields:
            if row.get(f) is not None:
                row[f] = str(row[f])
    return rows


def list_meetings(
    company_id: str,
    start_after: str | None = None,
    end_before: str | None = None,
    host_email: str | None = None,
    participant_email: str | None = None,
    limit: int | None = None,
) -> dict:
    """
    Discover meetings in a given period for a company.

    Always call this first to build the list of UUIDs for subsequent tool calls.
    The response includes has_synthesis and has_transcript flags for each meeting —
    use these to decide the next step:
      - has_synthesis=true  → call get_meeting_details (AI summary already generated,
                              much cheaper in tokens than reading the raw transcript)
      - has_synthesis=false AND has_transcript=true → call get_meeting_transcript
                              (verbatim VTT, potentially 50,000+ tokens — use sparingly)

    Volume reasoning: without date filters this can return hundreds of meetings.
    Always set start_after (and optionally end_before) to bound the result set before
    omitting limit. Without limit, returns all matching rows.

    WSR usage: set start_after to the cutoff_date extracted from the baseline ticket title.

    Parameters
    ----------
    company_id        : Company identifier (e.g. "NWN", "DAI").
    start_after       : ISO date "YYYY-MM-DD" — meetings starting on or after this date.
    end_before        : ISO date "YYYY-MM-DD" — meetings starting strictly before this date.
    host_email        : Exact email address of the meeting host.
    participant_email : Partial match against the participants_emails field.
    limit             : Maximum meetings to return. If omitted, returns all matching rows.
                        Use date filters to bound the query before omitting this.
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

    if limit is not None and limit == 0:
        return {"meetings": [], "total": 0}

    conditions = [
        "m.status != 'inactive'",
        "m.meeting_uuid IN (SELECT meeting_id FROM meetings_assets.meetings_projects WHERE project_id = %s)",
    ]
    params: list = [company_id.upper()]

    if start_after:
        conditions.append("m.start_time >= %s")
        params.append(start_after)
    if end_before:
        conditions.append("m.start_time < %s")
        params.append(end_before)
    if host_email:
        conditions.append("m.host_email = %s")
        params.append(host_email)
    if participant_email:
        conditions.append("m.participants_emails LIKE %s")
        params.append(f"%{participant_email}%")

    where = " AND ".join(conditions)
    sql = (
        "SELECT m.meeting_uuid, m.meeting_title, m.start_time, m.end_time, m.host_email, "
        "m.duration, m.has_transcript, "
        "IF(m.synthesized_meeting IS NOT NULL AND m.synthesized_meeting != '', 1, 0) AS has_synthesis "
        f"FROM meetings_assets.meetings m WHERE {where} "
        "ORDER BY m.start_time DESC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    conn = get_meet_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"message": "No meetings found for the given filters."}

    rows = _serialize(rows, _DATE_FIELDS)
    for row in rows:
        row["has_synthesis"] = bool(row.get("has_synthesis"))
        row["has_transcript"] = bool(row.get("has_transcript"))

    return fit_to_limit(rows, "meetings", {"total": len(rows)})


def get_meeting_details(meeting_uuids: list[str], company_id: str) -> dict:
    """
    Fetch full metadata and AI synthesis for one or more meetings by UUID.

    Prefer this over get_meeting_transcript whenever has_synthesis=true — the AI summary
    (synthesized_meeting) is already generated and is far cheaper in tokens than reading
    the raw VTT. Returns: synthesized_meeting (decisions, action items, risks, key topics),
    zoom_summary, participants, duration, and all date fields.

    Call after list_meetings for meetings where has_synthesis=true. If synthesized_meeting
    is empty and has_transcript=true, call get_meeting_transcript instead.

    Volume reasoning: no batch limit enforced — caller decides how many UUIDs to include.
    Keep in mind that synthesized_meeting can be several hundred tokens per meeting;
    factor this into batch size decisions.

    Parameters
    ----------
    meeting_uuids : List of meeting UUIDs obtained from list_meetings.
    company_id    : Company identifier (used for tenant isolation).
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}
    if not meeting_uuids:
        return {"message": "No meeting UUIDs provided."}

    placeholders = ", ".join(["%s"] * len(meeting_uuids))
    sql = (
        f"SELECT {_DETAIL_COLUMNS} "
        f"FROM meetings_assets.meetings "
        f"WHERE meeting_uuid IN ({placeholders}) "
        f"AND meeting_uuid IN ("
        f"  SELECT meeting_id FROM meetings_assets.meetings_projects WHERE project_id = %s"
        f")"
    )

    conn = get_meet_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [*meeting_uuids, company_id.upper()])
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"message": "No meetings found for the given UUIDs."}

    rows = _serialize(rows, _DATE_FIELDS)
    for row in rows:
        row["has_transcript"] = bool(row.get("has_transcript"))

    return fit_to_limit(rows, "meetings", {"total": len(rows)})


def get_meeting_transcript(meeting_uuid: str, company_id: str, offset: int = 0) -> dict:
    """
    Download the raw VTT transcript for a meeting directly from S3.

    WARNING — token budget: a full transcript can exceed 50,000 tokens. Confirm with
    the user that they need the verbatim content before calling this. In most cases,
    get_meeting_details is preferable when has_synthesis=true.

    Use this only when has_transcript=true AND synthesized_meeting is empty (Cerebro has
    not yet summarized this meeting), or when the user explicitly needs verbatim content.
    The transcript is the raw Zoom caption file split into timed VTT segments.

    Reads part1.vtt and part2.vtt from S3 and concatenates them if both exist.
    If the transcript exceeds the response size limit it is returned in chunks — call
    again with offset=<next_offset> from the previous response to get the next chunk.

    Parameters
    ----------
    meeting_uuid : UUID of the meeting (from list_meetings).
    company_id   : Company identifier (used to construct the S3 key).
    offset       : Character offset to start reading from (default 0). Use next_offset
                   from a previous truncated response to page through long transcripts.
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}
    if not S3_BUCKET:
        return {"message": "S3_BUCKET is not configured."}

    parts = []
    for part_name in ("part1.vtt", "part2.vtt"):
        key = f"{company_id.upper()}/meetings/transcripts/{meeting_uuid}/{part_name}"
        try:
            obj = _s3.get_object(Bucket=S3_BUCKET, Key=key)
            parts.append(obj["Body"].read().decode("utf-8", errors="replace"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                break
            break

    if not parts:
        return {"message": f"No transcript found in S3 for meeting {meeting_uuid}."}

    full_text = "\n\n".join(parts)
    total_chars = len(full_text)
    chunk = full_text[offset:]

    # Trim chunk to fit within the response size limit (~900 KB encoded)
    # Reserve ~200 bytes for the metadata fields
    max_chars = MAX_RESPONSE_BYTES - 200
    if len(chunk.encode("utf-8")) > max_chars:
        # Cut at a safe char boundary
        chunk = chunk.encode("utf-8")[:max_chars].decode("utf-8", errors="ignore")
        next_offset = offset + len(chunk)
        return {
            "meeting_uuid": meeting_uuid,
            "parts": len(parts),
            "total_chars": total_chars,
            "offset": offset,
            "next_offset": next_offset,
            "truncated": True,
            "transcript": chunk,
        }

    return {
        "meeting_uuid": meeting_uuid,
        "parts": len(parts),
        "total_chars": total_chars,
        "offset": offset,
        "truncated": False,
        "transcript": chunk,
    }


def search_meetings(query: str, company_id: str, limit: int = 10) -> dict:
    """
    Semantic search over meeting transcripts and syntheses using the local RAG service.

    Use this to find meetings related to a topic, decision, or keyword — even if the exact
    words don't appear in the transcript. Returns results ordered by relevance (rank 1 = most
    relevant). The rank order is the signal — do not treat rank numbers as absolute scores.

    Complements list_meetings (date/host filters) for thematic discovery across the corpus.
    Call this when you need to find meetings where a specific subject was discussed without
    knowing which meeting or date to look in.

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
            f"{RAG_SERVICE_URL}/search/meetings",
            json={"query": query, "company_id": company_id.upper(), "top_k": limit},
            timeout=30.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        return {"message": "RAG service unavailable. Start rag_service/app.py first.", "results": []}
    except Exception as e:
        return {"error": f"RAG search failed: {e}", "results": []}


def get_meeting_participants(meeting_uuid: str, company_id: str) -> dict:
    """
    Fetch the participant list for a meeting.

    Returns the emails from meetings.participants_emails (JSON), marks which is host via
    meetings.host_email, and includes the raw participant count from meetings_participants.

    Parameters
    ----------
    meeting_uuid : UUID of the meeting (from list_meetings).
    company_id   : Company identifier (used for tenant isolation).
    """
    import json as _json

    err = validate_company(company_id)
    if err:
        return {"error": err}

    sql = (
        "SELECT m.participants_emails, m.host_email, "
        "COUNT(mp.id) AS participant_count "
        "FROM meetings_assets.meetings m "
        "JOIN meetings_assets.meetings_projects proj ON proj.meeting_id = m.meeting_uuid "
        "LEFT JOIN meetings_assets.meetings_participants mp ON mp.meeting_id = m.meeting_uuid "
        "WHERE m.meeting_uuid = %s AND proj.project_id = %s "
        "GROUP BY m.meeting_uuid, m.participants_emails, m.host_email"
    )

    conn = get_meet_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [meeting_uuid, company_id.upper()])
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"message": f"Meeting {meeting_uuid} not found for company {company_id}."}

    emails_raw = row.get("participants_emails") or "[]"
    try:
        emails = _json.loads(emails_raw) if isinstance(emails_raw, str) else (emails_raw or [])
    except Exception:
        emails = []

    host = row.get("host_email", "")
    participants = [
        {"email": e, "is_host": e == host}
        for e in emails
    ]

    return {
        "meeting_uuid": meeting_uuid,
        "participants": participants,
        "total": len(participants),
        "participant_count_from_db": row.get("participant_count", 0),
    }


def get_meeting_chat(meeting_uuid: str, company_id: str) -> dict:
    """
    Retrieve the Zoom chat log for a meeting.

    Chat messages often contain informal decisions, links, action items, and mentions that
    don't appear in the spoken transcript. This is a fourth source of meeting information
    alongside synthesized_meeting, zoom_summary, and the VTT transcript — treat it as
    complementary, not a substitute.

    Returns the raw chat text as stored by the daemon. May be empty if no chat was recorded
    or if the meeting platform didn't export chat.

    Parameters
    ----------
    meeting_uuid : UUID of the meeting (from list_meetings).
    company_id   : Company identifier (used for tenant isolation).
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

    sql = (
        "SELECT m.chat "
        "FROM meetings_assets.meetings m "
        "JOIN meetings_assets.meetings_projects proj ON proj.meeting_id = m.meeting_uuid "
        "WHERE m.meeting_uuid = %s AND proj.project_id = %s"
    )

    conn = get_meet_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [meeting_uuid, company_id.upper()])
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"message": f"Meeting {meeting_uuid} not found or not associated with {company_id}."}

    chat = row.get("chat") or ""
    if not chat.strip():
        return {"meeting_uuid": meeting_uuid, "message": "No chat recorded for this meeting.", "chat": ""}

    return {"meeting_uuid": meeting_uuid, "chat": chat}


def summarize_transcript_for_ticket(
    meeting_uuid: str,
    ticket_title: str,
    company_id: str,
) -> dict:
    """
    Extract the parts of a meeting transcript relevant to a specific Wrike ticket.

    Uses a local Ollama LLM (model configured via OLLAMA_SUMMARIZE_MODEL) to read the
    full VTT transcript and return only what was said about the given ticket — decisions,
    action items, risks, blockers, status updates. Runs entirely on machine_20 with no
    external API calls.

    Fallback: if Ollama is unavailable or the model is not pulled, returns the raw
    transcript text so the caller can filter manually. The `source` field in the response
    indicates whether the result is an LLM summary ("ollama") or the raw transcript
    ("raw_transcript").

    Prefer this over get_meeting_transcript for per-ticket investigation — it drastically
    reduces token consumption when you only care about one ticket per call.

    Parameters
    ----------
    meeting_uuid  : UUID of the meeting (from list_meetings).
    ticket_title  : Title of the Wrike ticket to focus on (e.g. "Deploy new API v2").
    company_id    : Company identifier (used for tenant isolation and S3 path).
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}
    if not S3_BUCKET:
        return {"message": "S3_BUCKET is not configured."}

    # Load transcript from S3
    parts = []
    for part_name in ("part1.vtt", "part2.vtt"):
        key = f"{company_id.upper()}/meetings/transcripts/{meeting_uuid}/{part_name}"
        try:
            obj = _s3.get_object(Bucket=S3_BUCKET, Key=key)
            parts.append(obj["Body"].read().decode("utf-8", errors="replace"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                break
            break

    if not parts:
        return {"message": f"No transcript found in S3 for meeting {meeting_uuid}."}

    full_transcript = "\n\n".join(parts)
    transcript_for_llm = full_transcript[:_MAX_TRANSCRIPT_CHARS]
    was_truncated = len(full_transcript) > _MAX_TRANSCRIPT_CHARS

    # Try Ollama. Summarization is an extraction task, not reasoning — disable
    # thinking explicitly (qwen3 Modelfiles ship with it on, which causes the
    # model to exhaust num_predict inside <think> on long transcripts and return
    # empty content). num_predict caps output so a stalled thinking run can't
    # silently consume the whole context. Retry transient infra failures (Ollama
    # runner crashes, EOFs, 5xx) before giving up on the raw-transcript fallback.
    payload = {
        "model": OLLAMA_SUMMARIZE_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a focused extraction assistant. Given a meeting transcript "
                    "and a Wrike ticket name, extract ONLY what was said about that ticket. "
                    "Be brief and factual — 2 to 5 sentences. If the ticket is not "
                    "mentioned or discussed, say so in one sentence."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Meeting transcript:\n\n{transcript_for_llm}\n\n---\n\n"
                    f"Extract and summarize ONLY the parts directly relevant to the "
                    f"Wrike ticket: \"{ticket_title}\".\n"
                    f"Include: decisions, action items, risks, blockers, status updates."
                ),
            },
        ],
        "stream": False,
        "think": False,
        "options": {"num_ctx": 65536, "num_predict": 4096},
    }
    fallback_reason = "Ollama unreachable"
    summary = ""
    for attempt in range(3):
        try:
            r = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=120.0)
            r.raise_for_status()
            summary = r.json().get("message", {}).get("content", "").strip()
            break
        except httpx.RequestError as e:
            log.warning("ollama summarize unreachable (attempt %d): %s", attempt + 1, e)
            fallback_reason = f"Ollama unreachable: {e}"
        except httpx.HTTPStatusError as e:
            log.warning("ollama summarize HTTP %d (attempt %d): %s",
                        e.response.status_code, attempt + 1, e)
            fallback_reason = f"Ollama HTTP {e.response.status_code}"
            # Retry only on 5xx — 4xx is a client/payload bug, more attempts won't fix it.
            if e.response.status_code < 500:
                break
        if attempt < 2:
            time.sleep(2 ** attempt)

    if summary:
        return {
            "meeting_uuid": meeting_uuid,
            "ticket_title": ticket_title,
            "model": OLLAMA_SUMMARIZE_MODEL,
            "summary": summary,
            "transcript_truncated": was_truncated,
            "source": "ollama",
        }
    if summary == "" and fallback_reason == "Ollama unreachable":
        # All retries succeeded HTTP-wise but returned empty content — num_predict exhausted.
        log.warning(
            "ollama summarize returned empty content for meeting=%s ticket=%r (model=%s) — "
            "raise num_predict or check Ollama runner",
            meeting_uuid, ticket_title, OLLAMA_SUMMARIZE_MODEL,
        )
        fallback_reason = "Summarizer returned empty output"

    # Fallback: return raw transcript capped to response limit
    max_chars = MAX_RESPONSE_BYTES - 300
    chunk = full_transcript
    truncated_response = False
    if len(chunk.encode("utf-8")) > max_chars:
        chunk = chunk.encode("utf-8")[:max_chars].decode("utf-8", errors="ignore")
        truncated_response = True

    return {
        "meeting_uuid": meeting_uuid,
        "ticket_title": ticket_title,
        "source": "raw_transcript",
        "message": f"{fallback_reason} — raw transcript returned, filter manually.",
        "truncated": truncated_response,
        "transcript": chunk,
    }


def get_meeting_ticket_links(
    meeting_uuid: str,
    company_id: str,
    limit: int = 10,
) -> dict:
    """
    Find Wrike tickets that are semantically related to a meeting.

    Uses the meeting's AI synthesis and Zoom summary as a search query against the
    local RAG index of Wrike tasks. Returns the most relevant tickets ranked by
    semantic similarity — no manual query required.

    Use this to answer: "Which tickets should I update based on what was discussed
    in this meeting?" without having to read the full transcript or formulate queries
    yourself. Complements get_meeting_details by bridging from meeting content to
    actionable ticket IDs.

    Returns the same structure as search_tasks (rank, ticket_id, title, status, excerpt).

    Parameters
    ----------
    meeting_uuid : UUID of the meeting (from list_meetings).
    company_id   : Company identifier — results are isolated to this tenant.
    limit        : Maximum ticket results to return (default 10).
    """
    err = validate_company(company_id)
    if err:
        return {"error": err}

    # Fetch meeting content to build the search query
    placeholders = "%s"
    sql = (
        "SELECT m.meeting_title, m.synthesized_meeting, m.zoom_summary "
        "FROM meetings_assets.meetings m "
        "JOIN meetings_assets.meetings_projects proj ON proj.meeting_id = m.meeting_uuid "
        f"WHERE m.meeting_uuid = {placeholders} AND proj.project_id = %s"
    )

    conn = get_meet_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [meeting_uuid, company_id.upper()])
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"message": f"Meeting {meeting_uuid} not found for company {company_id}."}

    # Build a rich query from whatever content is available
    parts = []
    if row.get("meeting_title"):
        parts.append(row["meeting_title"])
    if row.get("synthesized_meeting"):
        parts.append((row["synthesized_meeting"] or "")[:2000])
    if row.get("zoom_summary"):
        parts.append((row["zoom_summary"] or "")[:1000])

    if not parts:
        return {
            "meeting_uuid": meeting_uuid,
            "message": "Meeting has no synthesis or summary — cannot build a search query. "
                       "Use search_tasks with a manual topic query instead.",
            "results": [],
        }

    query = "\n\n".join(parts)

    try:
        r = httpx.post(
            f"{RAG_SERVICE_URL}/search/tasks",
            json={"query": query, "company_id": company_id.upper(), "top_k": limit},
            timeout=30.0,
        )
        r.raise_for_status()
        result = r.json()
        result["meeting_uuid"] = meeting_uuid
        return result
    except httpx.ConnectError:
        return {
            "message": "RAG service unavailable. Start rag_service/app.py first.",
            "results": [],
        }
    except Exception as e:
        return {"error": f"RAG search failed: {e}", "results": []}
