"""
LLM interface for assay — all calls go through Ollama's OpenAI-compatible API.

Two public functions:
  generate_test_cases(tool, context) -> list[TestCase]
  evaluate_result(tool, case, result) -> Verdict
  generate_followups(tool, case, result, verdict) -> list[TestCase]

DeepSeek-R1 emits <think>...</think> before answering. We strip that block and
parse the JSON that follows.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

import config

_client = OpenAI(base_url=f"{config.OLLAMA_URL}/v1", api_key="ollama")

_SYSTEM = """\
You are assay, an autonomous QA agent testing the trajectory-mcp server.

## About the system under test
trajectory-mcp is a read-only MCP server that exposes data from:
- Zoom meetings (transcripts, participants, chat, summaries)
- Wrike project management (tasks, timelines, attachments)
- BambooHR (time off, birthdays, anniversaries, holidays)
- Semantic search via RAG (ChromaDB + Ollama embeddings)
- Diagnostics (RAG health, index stats, model inventory)

It is a multi-tenant system: most tools require a `company_id` to scope the query
to a specific client. The system serves ~31 companies.

## NWN — primary stress-test target
NWN is by far the largest client. It has:
- The most meetings, the longest transcripts
- The most Wrike tickets and project history
- The deepest data in semantic search

When you need to test for latency, volume, or real-data behavior, use NWN.
For multi-tenant isolation tests, compare NWN data against a smaller company.

## BambooHR tools — Trajectory-wide, NO company_id
The four BambooHR tools cover ALL employees of Trajectory Inc., NOT per-client data:
  get_time_off, get_birthdays, get_anniversaries, get_company_holidays

CRITICAL: These tools accept ONLY `start` and `end` (ISO date strings). They have
NO `company_id`, NO `limit`, NO `format`, NO other parameters.
- Passing any unknown parameter (e.g. company_id, limit, window_start) will be
  rejected by the server — this is CORRECT behavior, not a bug.
- get_birthdays and get_anniversaries are YEAR-AGNOSTIC: they normalize each person's
  birthday/anniversary to the requested year. Asking for 2025-01-01→2025-12-31 returns
  everyone whose birthday/anniversary month-day falls in that window, regardless of
  birth year. Returning results for "future" dates is CORRECT behavior.

## RAG search — vector database, not SQL
search_tasks and search_meetings use ChromaDB for semantic (vector) similarity search.
- There is NO SQL injection surface — queries are converted to embeddings, not SQL.
- A query like `'; DROP TABLE meetings; --` is treated as text to embed; returning
  meeting results for it is CORRECT behavior, not a security vulnerability.
- Search results may not exactly match the query — semantic search returns
  thematically related content. Imperfect match ≠ bug.
- Duplicate results (same UUID appearing twice) IS a real bug worth reporting.
- High latency (>15s) for NWN IS a real performance issue worth reporting.

## Data availability — recent imports only
The meetings and Wrike data were imported recently. All records are from 2026.
- Queries with date ranges before 2026 (e.g. 2023-01-01 to 2023-12-31) will return
  empty results — this is CORRECT, not a bug.
- For date filtering tests, use recent date ranges (last 30–60 days from today).
- "No meetings found" for a 2023 range = correct. "No meetings found" for this week = potential bug.

## Meeting flags — has_transcript and has_synthesis
list_meetings returns has_transcript and has_synthesis flags for each meeting.
- has_transcript=false → calling get_meeting_transcript returns "No transcript found" — CORRECT.
- has_synthesis=false → synthesized_meeting field will be empty — CORRECT.
- ONLY use UUIDs where has_transcript=true for testing get_meeting_transcript.
- ONLY use UUIDs where has_synthesis=true for testing get_meeting_details synthesis field.
- The discovery context provides transcript_uuids and synthesis_uuids for this purpose.

## Time-off overlap semantics
get_time_off, get_birthdays, get_anniversaries return entries that OVERLAP the
requested window. An entry that starts before window_start but extends into the window
WILL be returned — this is correct overlap behavior, not a date filtering bug.

## Your job
Generate adversarial but realistic test cases. Think like a QA engineer who
wants to find bugs, not just confirm happy paths. Specifically look for:
- Incorrect or missing data in responses
- Unhandled errors (stacktraces leaking, cryptic messages)
- Cross-tenant data leakage (company A getting company B's data)
- Performance degradation under real NWN load
- Edge cases the developer didn't think about

Always respond with valid JSON only. No markdown, no explanation outside JSON."""


def _strip_think(text: str) -> str:
    """Remove DeepSeek-R1's <think>...</think> reasoning block."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _parse_json(text: str) -> Any:
    text = _strip_think(text)
    # Find the first JSON array or object
    match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
    if match:
        return json.loads(match.group(1))
    raise ValueError(f"No JSON found in LLM response: {text[:200]}")


def _chat(messages: list[dict], temperature: float = 0.3) -> str:
    import time
    for attempt in range(3):
        try:
            resp = _client.chat.completions.create(
                model=config.MODEL,
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            msg = str(e)
            # Ollama transient crashes (signal/cgo, model runner stopped) — wait and retry
            if "500" in msg or "signal" in msg or "runner" in msg:
                if attempt < 2:
                    wait = 10 * (attempt + 1)
                    print(f"  [ollama] transient error, retrying in {wait}s… ({e})")
                    time.sleep(wait)
                    continue
            raise


# ── Public data types ──────────────────────────────────────────────────────────

@dataclass
class TestCase:
    name: str
    description: str
    arguments: dict
    expected_behavior: str
    is_followup: bool = False


@dataclass
class Verdict:
    verdict: str          # pass | fail | warning | security
    severity: str         # critical | major | minor | info
    category: str         # correctness | error_handling | performance | security | data_quality
    summary: str
    detail: str
    interesting: bool


# ── Prompt builders ────────────────────────────────────────────────────────────

def generate_test_cases(
    tool_name: str,
    tool_description: str,
    input_schema: dict,
    companies: list[str],
    discovery: dict,
) -> list[TestCase]:
    """Ask the LLM to generate 8-12 test cases for a given tool. Retries once on parse failure."""
    other_companies = [c for c in companies if c != "NWN"][:5]

    prompt = f"""Generate 8 to 12 test cases for the following MCP tool.

## Tool
Name: {tool_name}
Description: {tool_description}
Input schema: {json.dumps(input_schema, indent=2)}

## Available real IDs (use these for realistic tests)
{json.dumps(discovery, indent=2)}

## Available companies
All: {companies}
Largest (NWN) — use for stress/volume tests.
Others for isolation tests: {other_companies}

## Test categories to cover
1. Happy path — 2 or 3 cases with valid, realistic inputs
2. Edge cases — empty strings, boundary dates, limit=0, limit=9999, missing optional args
3. Invalid inputs — wrong types, nonexistent IDs, SQL injection strings, very long strings
4. Cross-tenant — use a UUID or ticket_id from NWN but request it for a different company_id
5. Stress (NWN only) — max limit, broadest date range, largest payloads

## STRICT PARAMETER RULES — follow these exactly
1. Use ONLY the parameters listed in the Input Schema above. Do NOT invent parameters
   not in the schema. If the schema has no company_id, do NOT add company_id.
   If the schema has no limit, do NOT add limit.
2. Parameter names must match the schema EXACTLY. If schema says "start", use "start"
   not "start_date", not "window_start", not "from_date".
3. Date values must be bare ISO strings: "2024-01-01" — NEVER wrap them in extra quotes
   like "\\"2024-01-01\\"" or "'2024-01-01'". An empty date is "" not "\\"\\"".
4. All string values must use only double quotes. Avoid apostrophes inside values.
5. For BambooHR tools (get_time_off, get_birthdays, get_anniversaries, get_company_holidays):
   these accept ONLY start and end. Do NOT add company_id, limit, or any other parameter.

Return a JSON array where each element is:
{{
  "name": "short test name",
  "description": "what this test checks",
  "arguments": {{}},
  "expected_behavior": "what a correct response looks like"
}}

Return ONLY the JSON array, nothing else."""

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]

    for attempt in range(2):
        raw = _chat(messages)
        try:
            cases_raw = _parse_json(raw)
            return [
                TestCase(
                    name=c.get("name", "unnamed"),
                    description=c.get("description", ""),
                    arguments=c.get("arguments", {}),
                    expected_behavior=c.get("expected_behavior", ""),
                )
                for c in cases_raw
                if isinstance(c, dict)
            ]
        except (ValueError, json.JSONDecodeError) as e:
            if attempt == 0:
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"Your response had a JSON error: {e}. Return ONLY the corrected JSON array, no other text."},
                ]
            else:
                raise ValueError(
                    f"JSON parse failed after retry.\n"
                    f"Error: {e}\n"
                    f"Raw LLM output (first 800 chars):\n{raw[:800]}"
                ) from e


def evaluate_result(
    tool_name: str,
    case: TestCase,
    result: Any,
    duration_ms: int,
    is_error: bool,
    error_message: str,
) -> Verdict:
    """Ask the LLM to evaluate a single tool call result."""
    result_repr = json.dumps(result, default=str)[:2000] if result is not None else f"ERROR: {error_message}"

    prompt = f"""Evaluate this MCP tool call result.

## Tool: {tool_name}
## Test: {case.name}
Description: {case.description}
Arguments: {json.dumps(case.arguments)}
Expected: {case.expected_behavior}

## Result
Duration: {duration_ms}ms
Is error: {is_error}
Response: {result_repr}

## Thresholds
- Performance warning: > {config.SLOW_MS}ms
- Performance critical: > {config.CRITICAL_LATENCY_MS}ms

Return a JSON object:
{{
  "verdict": "pass | fail | warning | security",
  "severity": "critical | major | minor | info",
  "category": "correctness | error_handling | performance | security | data_quality",
  "summary": "one-line finding (max 80 chars)",
  "detail": "2-3 sentences explaining what happened and why it matters",
  "interesting": true or false
}}

Rules:
- verdict=security if the response contains data from a different company than requested
- verdict=fail if the tool crashed or returned wrong data
- verdict=warning if behavior is technically correct but suspicious or worth noting
- verdict=pass if the tool handled this correctly (including graceful errors)
- interesting=true if a follow-up test would likely reveal more information

## Domain knowledge — do NOT flag these as bugs:
- "unexpected keyword argument X" where X is NOT in the tool's schema → the test was
  wrong, the tool correctly rejected invalid input → verdict=pass
- BambooHR tools returning results for "future" dates → year-agnostic behavior is correct
- "No transcript found" for a meeting with has_transcript=false → correct behavior
- Empty results for date ranges before 2026 → data was imported recently, all records are 2026
- search_meetings / search_tasks returning non-exact matches → semantic search works by
  similarity, not keyword match
- search_meetings / search_tasks processing a SQL injection string and returning results →
  these use ChromaDB vector search, not SQL; no injection surface exists
- Extra fields in the response beyond what was expected → tools may return more than minimum
- Time-off/anniversary/birthday entries starting before window_start → overlap queries
  return entries that OVERLAP the window, not strictly contained within it

Return ONLY the JSON object."""

    raw = _chat([
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ])
    v = _parse_json(raw)
    return Verdict(
        verdict=v.get("verdict", "pass"),
        severity=v.get("severity", "info"),
        category=v.get("category", "correctness"),
        summary=v.get("summary", "")[:120],
        detail=v.get("detail", ""),
        interesting=bool(v.get("interesting", False)),
    )


def generate_followups(
    tool_name: str,
    case: TestCase,
    result: Any,
    verdict: Verdict,
) -> list[TestCase]:
    """Generate 2-3 follow-up test cases based on an interesting finding."""
    result_repr = json.dumps(result, default=str)[:1000] if result is not None else "null"

    prompt = f"""A test for {tool_name} produced an interesting result. Generate 2 or 3 follow-up test cases to dig deeper.

## Original test
Name: {case.name}
Arguments: {json.dumps(case.arguments)}
Response: {result_repr}

## Finding
{verdict.summary}
{verdict.detail}

Generate follow-up tests that probe this finding further.
Return a JSON array (same schema as before: name, description, arguments, expected_behavior).
Return ONLY the JSON array."""

    try:
        raw = _chat([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ])
        cases_raw = _parse_json(raw)
        return [
            TestCase(
                name=c.get("name", "followup"),
                description=c.get("description", ""),
                arguments=c.get("arguments", {}),
                expected_behavior=c.get("expected_behavior", ""),
                is_followup=True,
            )
            for c in cases_raw
            if isinstance(c, dict)
        ]
    except Exception:
        return []
