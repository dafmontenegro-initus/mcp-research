from __future__ import annotations

import boto3
from botocore.exceptions import ClientError
from config import AWS_REGION, S3_BUCKET, validate_company
from db import get_meet_conn

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

    return {"total": len(rows), "meetings": rows}


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

    return {"total": len(rows), "meetings": rows}


def get_meeting_transcript(meeting_uuid: str, company_id: str) -> dict:
    """
    Download the raw VTT transcript for a meeting directly from S3.

    WARNING — token budget: a full transcript can exceed 50,000 tokens. Confirm with
    the user that they need the verbatim content before calling this. In most cases,
    get_meeting_details is preferable when has_synthesis=true.

    Use this only when has_transcript=true AND synthesized_meeting is empty (Cerebro has
    not yet summarized this meeting), or when the user explicitly needs verbatim content.
    The transcript is the raw Zoom caption file split into timed VTT segments.

    Reads part1.vtt and part2.vtt from S3 and concatenates them if both exist.

    Parameters
    ----------
    meeting_uuid : UUID of the meeting (from list_meetings).
    company_id   : Company identifier (used to construct the S3 key).
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

    return {
        "meeting_uuid": meeting_uuid,
        "parts": len(parts),
        "transcript": "\n\n".join(parts),
    }
