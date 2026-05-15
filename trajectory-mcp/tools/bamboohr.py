from __future__ import annotations

import ssl
import urllib.request
from datetime import date, datetime, timedelta

from config import (
    BAMBOOHR_ANNIVERSARIES_URL,
    BAMBOOHR_BIRTHDAYS_URL,
    BAMBOOHR_HOLIDAYS_URL,
    BAMBOOHR_TIMEOFF_URL,
)


def _fetch_ical(url: str) -> str | dict:
    """Fetch an iCal feed URL. Returns raw text or an error dict."""
    if not url:
        return {"error": "Feed URL is not configured on this server."}
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; trajectory-mcp/1.0)"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return {"error": f"Failed to fetch feed: {exc}"}


def _parse_ical(raw: str) -> list[dict]:
    """Parse VEVENT blocks from an iCal string."""
    entries: list[dict] = []
    current: dict | None = None
    for line in raw.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            entries.append(current)
            current = None
        elif current is not None:
            if line.startswith("SUMMARY:"):
                current["summary"] = line[len("SUMMARY:"):]
            elif line.startswith("DTSTART"):
                current["start"] = _parse_date(line.split(":", 1)[1])
            elif line.startswith("DTEND"):
                raw_end = _parse_date(line.split(":", 1)[1])
                # iCal DTEND for all-day events is exclusive — subtract one day
                current["end"] = raw_end - timedelta(days=1)
            elif line.startswith("DESCRIPTION:"):
                current["description"] = line[len("DESCRIPTION:"):]
    return entries


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip()[:8], "%Y%m%d").date()


def _filter_overlap(
    entries: list[dict],
    window_start: date,
    window_end: date,
    year_agnostic: bool = False,
) -> list[dict]:
    result = []
    today = date.today()
    for e in entries:
        s = e.get("start")
        if not s:
            continue
        en = e.get("end") or s  # single-day events (birthdays, anniversaries) have no DTEND

        if year_agnostic:
            # Year-agnostic normalization only applies to milestones already
            # reached. Future-natural events (e.g. a new hire's "1 year" event
            # dated next year) stay at their original date so the standard
            # overlap check below only surfaces them when the caller queries
            # a window that actually contains that future date.
            if s.year <= today.year:
                def _normalize(d: date) -> date:
                    for y in (window_start.year, window_start.year + 1):
                        try:
                            nd = d.replace(year=y)
                        except ValueError:
                            nd = d.replace(year=y, day=28)  # Feb 29 edge case
                        if window_start <= nd <= window_end:
                            return nd
                    return d.replace(year=window_start.year)

                s = _normalize(s)
                en = _normalize(en)

        if en < window_start or s > window_end:
            continue
        e = {**e, "start": s, "end": en}
        result.append(e)
    return result


def _clean_date(s: str | None) -> str | None:
    """Strip surrounding quotes/spaces that LLMs sometimes embed in string values."""
    if not s:
        return None
    cleaned = s.strip().strip("\"'").strip()
    return cleaned if cleaned else None


def _default_window(start: str | None, end: str | None) -> tuple[date, date]:
    today = date.today()
    s = _clean_date(start)
    e = _clean_date(end)
    ws = date.fromisoformat(s) if s else today - timedelta(days=today.weekday())
    we = date.fromisoformat(e) if e else ws + timedelta(days=6)
    return ws, we


def get_time_off(start: str | None = None, end: str | None = None) -> dict:
    """
    Return team members who are out of office according to the company-wide BambooHR calendar.

    This tool covers ALL of Trajectory — it does NOT require a company_id.
    Call it any time someone asks who is out, on vacation, or unavailable.

    Filters to entries that overlap the requested window. If no window is given,
    defaults to the current week (Monday–Sunday).

    Parameters
    ----------
    start : ISO date (YYYY-MM-DD). Defaults to this Monday.
    end   : ISO date (YYYY-MM-DD). Defaults to this Sunday.

    ## Inferring true available working days
    This tool only returns PTO/OOO entries. It does NOT subtract weekends or
    company holidays. To answer "how many working days does X have left in
    period P":
      1. Compute the set of weekdays (Mon–Fri) inside P.
      2. Call get_company_holidays(start=P.start, end=P.end) — subtract every
         day inside any returned holiday range (holidays often span more than
         one day; use start..end inclusive).
      3. Call get_time_off(start=P.start, end=P.end), filter entries where
         name == X, and subtract their days_out field (already clipped to the
         window).
    The remainder is X's true available working days. Do NOT skip step 2 — a
    week with a single mid-week holiday silently invalidates a "5 working days"
    answer derived only from PTO data.
    """
    raw = _fetch_ical(BAMBOOHR_TIMEOFF_URL)
    if isinstance(raw, dict):
        return raw

    ws, we = _default_window(start, end)
    entries = _filter_overlap(_parse_ical(raw), ws, we)
    entries.sort(key=lambda x: x["start"])

    return {
        "window_start": ws.isoformat(),
        "window_end": we.isoformat(),
        "total": len(entries),
        "entries": [
            {
                "name": e.get("summary", "Unknown"),
                "start": e["start"].isoformat(),
                "end": e["end"].isoformat(),
                "days_out": (min(e["end"], we) - max(e["start"], ws)).days + 1,
            }
            for e in entries
        ],
    }


def get_birthdays(start: str | None = None, end: str | None = None) -> dict:
    """
    Return team member birthdays from BambooHR for the given date window.

    This tool covers ALL of Trajectory — it does NOT require a company_id.
    Call it when someone asks about upcoming birthdays or wants to acknowledge
    team members' birthdays in a given period.

    If no window is given, defaults to the current week (Monday–Sunday).

    Parameters
    ----------
    start : ISO date (YYYY-MM-DD). Defaults to this Monday.
    end   : ISO date (YYYY-MM-DD). Defaults to this Sunday.
    """
    raw = _fetch_ical(BAMBOOHR_BIRTHDAYS_URL)
    if isinstance(raw, dict):
        return raw

    ws, we = _default_window(start, end)
    entries = _filter_overlap(_parse_ical(raw), ws, we, year_agnostic=True)
    entries.sort(key=lambda x: x["start"])

    return {
        "window_start": ws.isoformat(),
        "window_end": we.isoformat(),
        "total": len(entries),
        "entries": [
            {
                "name": e.get("summary", "Unknown"),
                "date": e["start"].isoformat(),
                "description": e.get("description", ""),
            }
            for e in entries
        ],
    }


def get_anniversaries(start: str | None = None, end: str | None = None) -> dict:
    """
    Return team member work anniversaries from BambooHR for the given date window.

    This tool covers ALL of Trajectory — it does NOT require a company_id.
    Call it when someone asks about upcoming anniversaries or tenure milestones.

    If no window is given, defaults to the current week (Monday–Sunday).

    Parameters
    ----------
    start : ISO date (YYYY-MM-DD). Defaults to this Monday.
    end   : ISO date (YYYY-MM-DD). Defaults to this Sunday.

    ## Inferring tenure for new hires
    This tool returns ANNIVERSARY MILESTONES (1 year, 2 years, ...), not hire dates.
    If someone doesn't appear in the current year's results, they may not have
    reached their first anniversary yet. Query future years (current_year + 1,
    then +2 if still not found) to locate their first milestone — its date minus
    N years is the hire date.

    Worked example (today = 2026-05-15, user asks tenure for "Daniel Rozo"):
      - Query 2026 (start=2026-01-01, end=2026-12-31): not found → maybe a new hire
      - Query 2027: returns "Daniel Rozo, 2027-05-04, 1 year"
      - Inference: hire_date = 2027-05-04 minus 1 year = 2026-05-04
      - Tenure today = 2026-05-15 minus 2026-05-04 = 11 days

    Do NOT report a not-found result as "no record exists" until you have checked
    at least the next two future years.
    """
    raw = _fetch_ical(BAMBOOHR_ANNIVERSARIES_URL)
    if isinstance(raw, dict):
        return raw

    ws, we = _default_window(start, end)
    entries = _filter_overlap(_parse_ical(raw), ws, we, year_agnostic=True)
    entries.sort(key=lambda x: x["start"])

    return {
        "window_start": ws.isoformat(),
        "window_end": we.isoformat(),
        "total": len(entries),
        "entries": [
            {
                "name": e.get("summary", "Unknown"),
                "date": e["start"].isoformat(),
                "description": e.get("description", ""),
            }
            for e in entries
        ],
    }


def get_company_holidays(start: str | None = None, end: str | None = None) -> dict:
    """
    Return company holidays from BambooHR for the given date window.

    This tool covers ALL of Trajectory — it does NOT require a company_id.
    Call it when someone asks about upcoming holidays, non-working days, or
    when building capacity plans for a sprint or project week.

    If no window is given, defaults to the current week (Monday–Sunday).

    Parameters
    ----------
    start : ISO date (YYYY-MM-DD). Defaults to this Monday.
    end   : ISO date (YYYY-MM-DD). Defaults to this Sunday.
    """
    raw = _fetch_ical(BAMBOOHR_HOLIDAYS_URL)
    if isinstance(raw, dict):
        return raw

    ws, we = _default_window(start, end)
    entries = _filter_overlap(_parse_ical(raw), ws, we)
    entries.sort(key=lambda x: x["start"])

    return {
        "window_start": ws.isoformat(),
        "window_end": we.isoformat(),
        "total": len(entries),
        "entries": [
            {
                "holiday": e.get("summary", "Unknown"),
                "start": e["start"].isoformat(),
                "end": e["end"].isoformat(),
                "description": e.get("description", ""),
            }
            for e in entries
        ],
    }
