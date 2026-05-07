from __future__ import annotations

import boto3
from botocore.exceptions import ClientError
from config import AWS_REGION, DEV_S3_BUCKET, resolve_company
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
    limit: int = 100,
) -> dict:
    """
    Discover meetings in a given period for a company.

    Returns one row per meeting with: meeting_uuid, title, start_time, end_time,
    host_email, duration, has_transcript, and has_synthesis. The has_synthesis flag
    tells you whether Cerebro already produced an AI summary — if True, call
    get_meeting_details to read it; if False and has_transcript=1, call
    get_meeting_transcript for the raw VTT. Always use this tool first to collect
    the UUIDs you need for subsequent calls.

    WSR usage: set start_after to the cutoff_date extracted from the baseline ticket.

    Parameters
    ----------
    company_id        : Company identifier (e.g. "NWN", "DAI"). Aliases resolved
                        automatically (e.g. "TJV" → "NWN").
    start_after       : ISO date "YYYY-MM-DD" — meetings starting on or after this date.
    end_before        : ISO date "YYYY-MM-DD" — meetings starting strictly before this date.
    host_email        : Exact email address of the meeting host.
    participant_email : Partial match against the participants_emails field.
    limit             : Maximum meetings to return (default 100, max 200).
    """
    resolve_company(company_id)
    limit = min(limit, 200)

    conditions = ["status != 'inactive'"]
    params: list = []

    if start_after:
        conditions.append("start_time >= %s")
        params.append(start_after)
    if end_before:
        conditions.append("start_time < %s")
        params.append(end_before)
    if host_email:
        conditions.append("host_email = %s")
        params.append(host_email)
    if participant_email:
        conditions.append("participants_emails LIKE %s")
        params.append(f"%{participant_email}%")

    where = " AND ".join(conditions)
    sql = (
        "SELECT meeting_uuid, meeting_title, start_time, end_time, host_email, "
        "duration, has_transcript, "
        "IF(synthesized_meeting IS NOT NULL AND synthesized_meeting != '', 1, 0) AS has_synthesis "
        f"FROM meetings_assets.meetings WHERE {where} "
        "ORDER BY start_time DESC LIMIT %s"
    )
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

    Returns synthesized_meeting (Cerebro's AI summary of the meeting — decisions,
    action items, risks, key topics), zoom_summary, participants, duration, and
    all date fields. Use this after list_meetings for meetings where has_synthesis
    is True. If synthesized_meeting is empty and has_transcript is True, call
    get_meeting_transcript instead to read the raw VTT.

    Call with batches of up to 50 UUIDs at a time to avoid oversized responses.

    Parameters
    ----------
    meeting_uuids : List of meeting UUIDs obtained from list_meetings.
    company_id    : Company identifier (used for context validation).
    """
    if not meeting_uuids:
        return {"message": "No meeting UUIDs provided."}
    if len(meeting_uuids) > 50:
        return {"message": "Provide at most 50 UUIDs per call. Split into batches."}

    placeholders = ", ".join(["%s"] * len(meeting_uuids))
    sql = (
        f"SELECT {_DETAIL_COLUMNS} "
        f"FROM meetings_assets.meetings WHERE meeting_uuid IN ({placeholders})"
    )

    conn = get_meet_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, meeting_uuids)
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

    Use this only for meetings where has_transcript is True but synthesized_meeting
    is empty (i.e. Cerebro has not yet summarized this meeting). The transcript is
    the verbatim caption file from Zoom, split into timed segments. It may be long.
    Read it to extract decisions, action items, risks, blockers, and Wrike ticket
    mentions that would otherwise be missed.

    Reads part1.vtt and part2.vtt (concatenated if both exist).

    Parameters
    ----------
    meeting_uuid : UUID of the meeting (from list_meetings).
    company_id   : Company identifier (for context).
    """
    if not DEV_S3_BUCKET:
        return {"message": "DEV_S3_BUCKET is not configured."}

    parts = []
    for part_name in ("part1.vtt", "part2.vtt"):
        key = f"{meeting_uuid}/{part_name}"
        try:
            obj = _s3.get_object(Bucket=DEV_S3_BUCKET, Key=key)
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
