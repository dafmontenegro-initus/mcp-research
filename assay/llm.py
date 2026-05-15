"""
LLM interface for assay — all calls go through the Ollama native REST API.

Public functions:
  generate_test_cases(tool, context) -> list[TestCase]   uses config.MODEL
  evaluate(tool, case, result) -> Verdict                uses config.MODEL
  generate_followups(tool, case, result, verdict) -> list[TestCase]

Single-evaluator architecture. qwen3.6:27b (Alibaba Cloud) plays both roles:
it generates the test cases and then evaluates each result. The evaluation
prompt combines two perspectives in a single pass:
  1. Intent check — did the tool do what the test asked for?
  2. Contract check — does the response comply with the documented contract?

Verdicts are binary ("pass" or "fail"); the failure type is in `category`
(correctness | security | performance | data_quality | error_handling).

Why single-evaluator: an earlier dual-layer design (gen-judge + independent
auditor) couldn't run reliably on the deployment hardware — Ollama 0.23.2 on
2× RTX A6000 stranded a runner process on GPU 1 and refused to release VRAM
between model loads, crashing the auditor with "model runner has unexpectedly
stopped". Simplifying to one evaluator removes the second model load entirely
and halves the per-test latency. The merged prompt preserves both perspectives.

Streaming Ollama call: `_chat` accumulates content + thinking chunk-by-chunk,
detects stalls via per-chunk idle timeout, and raises with the real cause if
the model exhausts num_predict inside thinking instead of returning empty
content silently.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import requests as _requests

import config


# ── Generator system prompt ────────────────────────────────────────────────────

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
  thematically related content. Imperfect match != bug.
- Duplicate results (same UUID appearing twice) IS a real bug worth reporting.
- High latency (>15s) for NWN IS a real performance issue worth reporting.

## Data availability — recent imports only
The meetings and Wrike data were imported recently. All records are from 2026.
- Queries with date ranges before 2026 (e.g. 2023-01-01 to 2023-12-31) will return
  empty results — this is CORRECT, not a bug.
- For date filtering tests, use recent date ranges (last 30-60 days from today).
- "No meetings found" for a 2023 range = correct. "No meetings found" for this week = potential bug.

## Meeting flags — has_transcript and has_synthesis
list_meetings returns has_transcript and has_synthesis flags for each meeting.
- has_transcript=false: calling get_meeting_transcript returns "No transcript found" — CORRECT.
- has_synthesis=false: synthesized_meeting field will be empty — CORRECT.
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

## Required adversarial categories — emit when applicable to the tool
You MUST include at least one test from each category below for every tool whose
signature supports it. Skip a category only when the tool's signature makes it
impossible (e.g. no `limit` parameter → skip param-shape boundaries).

1. Cross-tenant isolation — for any multi-tenant tool that accepts an opaque ID
   (meeting_uuid, ticket_id, etc.) AND a company_id: pass a known ID belonging to
   one tenant together with a DIFFERENT company_id. Expected behavior: empty /
   not-found, NEVER the other tenant's data. Use the discovery context to pick a
   valid NWN UUID and pair it with a smaller company (e.g. company_id="DAI").
   Name the case `security_cross_tenant_<tool>` and tag the intent clearly so the
   evaluator treats data leakage as severity=critical, category=security.

2. Param-shape boundaries — for any tool with a `limit` parameter, generate cases
   with limit=0, limit=-1, and limit=10000 (or larger than the underlying dataset).
   Expected behavior: graceful — empty list, error, or cap to a sane maximum.
   A stacktrace, a hang, or a silently-ignored value is a bug.

3. Offset/pagination boundaries — for tools with an `offset` parameter (currently
   get_meeting_transcript): generate cases with offset=-1 and offset=<total_chars+1>
   (use a value larger than any realistic transcript, e.g. 10_000_000). Expected
   behavior: clear error or empty chunk — not a silent wraparound or crash.

Always respond with valid JSON only. No markdown, no explanation outside JSON."""


# ── Evaluator system prompt ────────────────────────────────────────────────────

_EVAL_SYSTEM = """\
You are assay, the QA agent that designed this test case. You are now evaluating
whether the tool's response is correct. Apply BOTH perspectives in a single judgment:

  1. INTENT — did the tool do what your test asked for? You know what you expected.
  2. CONTRACT — does the response comply with the tool's documented behavior?

A "pass" requires both: the intent was fulfilled AND the contract is respected.
A "fail" on either dimension is a fail. Use `category` to explain why.

## System under test
trajectory-mcp is a read-only MCP server exposing:
- Zoom meetings (transcripts, participants, chat, summaries)
- Wrike project management (tasks, timelines, attachments)
- BambooHR (time off, birthdays, anniversaries, holidays) — Trajectory-wide, NO company_id
- Semantic search via RAG (ChromaDB + Ollama embeddings)
- Diagnostics (RAG health, index stats, model inventory)

Multi-tenant: most tools scope data by company_id. ~31 companies served.

## Verdict rules — binary, no ambiguity
- verdict=pass: tool did EXACTLY what was requested AND complies with its documented
  contract. Pass includes graceful error responses for invalid inputs — that IS
  correct behavior.
- verdict=fail: anything less than perfect. Use category to explain why:
    correctness    — wrong data, missing fields, incorrect logic
    security       — data from a different tenant than requested (cross-tenant leak)
    performance    — unacceptable latency or resource usage
    data_quality   — malformed, truncated, or inconsistent data
    error_handling — crash, unhandled exception, or cryptic error message
- interesting=true: a follow-up test would likely confirm or expand this finding.

## Domain knowledge — do NOT flag these as failures
- "unexpected keyword argument X" where X is not in the tool schema: tool correctly
  rejected invalid input -> verdict=pass
- BambooHR tools (get_time_off, get_birthdays, get_anniversaries, get_company_holidays)
  have NO company_id parameter. Rejection of company_id = correct -> verdict=pass.
- get_birthdays / get_anniversaries returning "future" dates: year-agnostic, entries
  are normalized to the requested year -> CORRECT -> verdict=pass
- "No transcript found" for a meeting with has_transcript=false -> correct -> verdict=pass
- Empty results for date ranges before 2026 -> all data is from 2026 -> verdict=pass
- search_meetings / search_tasks: semantic similarity != exact keyword match -> verdict=pass
- search_meetings / search_tasks processing SQL injection strings -> ChromaDB vector
  search, no SQL surface -> returning results is correct -> verdict=pass
- Extra response fields beyond what was expected -> verdict=pass
- Time-off/birthday/anniversary entries starting before window_start -> overlap
  semantics: entries that START before but EXTEND INTO the window are returned -> verdict=pass
- get_company_holidays returning the current week's holidays -> default window is correct
- Invalid/empty company_id on multi-tenant tools currently falls through to "not found"
  responses (validate_company is a documented no-op). Still flag it as correctness=minor
  — don't deliberate on whether validation "should" exist; the test's expectation is the contract.

## Important: prompt-side truncation is NOT a tool bug
The Response payload below may end with a "[NOTE: assay truncated this representation
for prompt size...]" marker. That truncation is in this prompt only; the actual tool
response was returned in full. NEVER flag a finding for "response truncated/malformed"
based on the cut-off you see here — use the structural summary in the NOTE instead.

## Output
Return ONLY a JSON object — no explanation, no markdown:
{
  "verdict": "pass | fail",
  "severity": "critical | major | minor | info",
  "category": "correctness | security | performance | data_quality | error_handling",
  "summary": "one-line finding (max 80 chars)",
  "detail": "2-3 sentences: what was requested, what happened, why it matters",
  "interesting": true or false
}"""


# ── Reasoning extraction ───────────────────────────────────────────────────────

def _extract_reasoning(text: str) -> tuple[str, str]:
    """
    Extract thinking block. Returns (reasoning, clean_text).
    Handles <think>...</think> blocks embedded in content (used by qwen3 and others
    when the native message.thinking channel isn't used).
    """
    m = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL)
    if m:
        reasoning = m.group(1).strip()
        clean = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return reasoning, clean
    return "", text.strip()


def _strip_think(text: str) -> str:
    _, clean = _extract_reasoning(text)
    return clean


def _parse_json(text: str) -> Any:
    text = _strip_think(text)
    for start, ch in enumerate(text):
        if ch in ("[", "{"):
            try:
                value, _ = json.JSONDecoder().raw_decode(text, start)
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError(f"No JSON found in LLM response: {text[:200]}")


_NUM_CTX = 16384
_NUM_PREDICT = 8192
_CONNECT_TIMEOUT = 30
_READ_TIMEOUT = 180  # per-chunk idle timeout while streaming


def _chat(messages: list[dict], model: str | None = None, think: bool = True) -> tuple[str, str]:
    """
    Call Ollama /api/chat with streaming. Returns (content, reasoning).

    `think` toggles the model's reasoning mode. Reasoning is valuable for nuanced
    evaluation but adds 30-90s per call. Pass think=False for tasks that only need
    structured JSON output from a template (e.g. test case generation).

    Streams so thinking and content are accumulated chunk-by-chunk regardless of
    how Ollama splits them, partial output survives a late disconnect, and a
    stalled runner is detected via a per-chunk idle timeout rather than a total
    wall-clock cap (reasoning models legitimately run for many minutes).

    num_ctx / num_predict are set explicitly because Modelfile defaults (typ.
    4096 / 2048) truncate reasoning models inside the <think> block on long
    QA-agent prompts, leaving message.content empty. If the model still hits
    num_predict while thinking, we raise with the actual cause rather than
    returning empty content silently.
    """
    effective_model = model or config.MODEL
    payload = {
        "model": effective_model,
        "messages": messages,
        "stream": True,
        "think": think,
        "options": {"num_ctx": _NUM_CTX, "num_predict": _NUM_PREDICT},
    }
    url = f"{config.OLLAMA_URL}/api/chat"

    for attempt in range(3):
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        done_reason = ""
        try:
            with _requests.post(
                url, json=payload, stream=True, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)
            ) as resp:
                if not resp.ok:
                    body = resp.json() if resp.content else {}
                    raise RuntimeError(f"{resp.status_code} — {body.get('error', resp.text[:200])}")
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if err := chunk.get("error"):
                        raise RuntimeError(f"ollama stream error: {err}")
                    msg = chunk.get("message") or {}
                    if c := msg.get("content"):
                        content_parts.append(c)
                    if t := msg.get("thinking"):
                        thinking_parts.append(t)
                    if chunk.get("done"):
                        done_reason = chunk.get("done_reason", "")
                        break

            content = "".join(content_parts).strip()
            thinking = "".join(thinking_parts).strip()

            # done_reason="length" means the model hit num_predict, regardless of
            # whether any content tokens leaked out before. Treat partial content
            # as failure too — it's almost certainly truncated JSON that will fail
            # parsing, and the upstream parse-retry would re-run the same long
            # thinking call for nothing.
            if done_reason == "length":
                raise RuntimeError(
                    f"{effective_model} hit num_predict={_NUM_PREDICT} "
                    f"(content={len(content)} chars, thinking={len(thinking)} chars). "
                    f"Raise _NUM_PREDICT or shorten the prompt."
                )

            # Fallback: models that embed <think>...</think> in content rather
            # than using the thinking channel.
            if not thinking and content:
                thinking, content = _extract_reasoning(content)

            return content, thinking

        except Exception as e:
            err = str(e)
            transient = any(s in err for s in ("500", "runner", "signal", "EOF", "timed out", "Connection"))
            if transient and attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"  [ollama] {effective_model} transient error, retrying in {wait}s... ({e})")
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
    verdict: str           # "pass" | "fail" | "unreachable" (infrastructure failure)
    severity: str          # critical | major | minor | info
    category: str          # correctness | security | performance | data_quality | error_handling | infrastructure
    summary: str
    detail: str
    interesting: bool
    reasoning_chain: str = ""  # raw thinking from the evaluator, empty if not available


# ── Prompt helpers ─────────────────────────────────────────────────────────────

def _format_result_for_prompt(result: Any, error_message: str, max_chars: int = 2000) -> str:
    """
    Render a tool result for inclusion in a prompt.

    If the JSON payload exceeds max_chars, append an EXPLICIT marker so the LLM
    doesn't mistake assay's prompt-side truncation for a malformed tool response.
    Without this hint, thinking models interpret the cut-off mid-JSON as a real
    data_quality bug and flag false positives.

    The marker also includes a structural summary (top-level keys + array lengths)
    so the evaluator has enough signal to verify schema even without the full payload.
    """
    if result is None:
        return f"ERROR: {error_message}"
    full = json.dumps(result, default=str)
    if len(full) <= max_chars:
        return full
    head = full[:max_chars]
    summary_parts: list[str] = [f"full_size={len(full)} chars"]
    if isinstance(result, dict):
        summary_parts.append(f"top_keys={list(result.keys())}")
        for k, v in result.items():
            if isinstance(v, list):
                summary_parts.append(f"{k}_length={len(v)}")
            elif isinstance(v, dict):
                summary_parts.append(f"{k}_keys={list(v.keys())}")
    elif isinstance(result, list):
        summary_parts.append(f"list_length={len(result)}")
    summary = "; ".join(summary_parts)
    return (
        f"{head}\n\n"
        f"[NOTE: assay truncated this representation for prompt size. The actual tool "
        f"response was returned in full and was NOT truncated. The cut-off above is in "
        f"this prompt only, not in the real response. Structural summary: {summary}]"
    )


# ── Public functions ───────────────────────────────────────────────────────────

def generate_test_cases(
    tool_name: str,
    tool_description: str,
    input_schema: dict,
    companies: list[str],
    discovery: dict,
) -> list[TestCase]:
    """Ask the generator model to produce 8-12 test cases for a tool. Retries once on parse failure."""
    other_companies = [c for c in companies if c != "NWN"][:5]

    prompt = f"""Generate 3 to 5 test cases for the following MCP tool, scaled
to the tool's parameter complexity (closer to 3 for simple 2-param tools, closer
to 5 for tools with many parameters and richer behavior). The follow-up mechanism
will deepen coverage on interesting findings, so prioritize variety over redundancy.

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

## Test categories to cover (pick the ones that matter for THIS tool)
1. Happy path — at least one realistic input
2. Edge / invalid inputs — boundary values, empty strings, wrong types, nonexistent
   IDs, SQL injection strings, missing optional args, limit=0, limit=9999
3. Cross-tenant — a UUID or ticket_id from NWN requested under a different company_id
   (only applies when the tool accepts company_id)
4. Stress (NWN only) — broadest date range or largest payload the tool supports

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
  "name": "snake_case_test_name",
  "description": "what this test checks",
  "arguments": {{}},
  "expected_behavior": "what a correct response looks like"
}}

`name` MUST be snake_case (e.g. happy_path_recent_meetings, edge_invalid_company_id,
stress_max_limit_nwn). Lowercase, words joined by underscores, no spaces or colons.

Return ONLY the JSON array, nothing else."""

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]

    for attempt in range(2):
        # Test case generation: think=False for speed — the prompt is prescriptive
        # enough that the model fills the template directly. Flip to think=True
        # for an overnight rigorous run to get more creative adversarial cases.
        raw, _ = _chat(messages, model=config.MODEL, think=False)
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


def evaluate(
    tool_name: str,
    case: TestCase,
    result: Any,
    duration_ms: int,
    is_error: bool,
    error_message: str,
) -> Verdict:
    """
    Single-evaluator evaluation. qwen3.6:27b (with thinking) judges both the test
    intent AND the documented contract in one pass.

    Retries on JSON parse errors. Transient infrastructure errors (runner crashes,
    EOFs, timeouts) are retried inside _chat(). Ultimate failure yields
    verdict="unreachable" so it routes to infrastructure_events instead of polluting
    findings.
    """
    result_repr = _format_result_for_prompt(result, error_message)
    prompt = f"""You designed this test. Evaluate whether the tool fulfilled your intent AND complied with its documented contract.

## Tool: {tool_name}
## Your test
Name: {case.name}
Description: {case.description}
Arguments: {json.dumps(case.arguments)}
Expected behavior: {case.expected_behavior}

## Actual result
Duration: {duration_ms}ms
Is error: {is_error}
Performance thresholds: warning > {config.SLOW_MS}ms, critical > {config.CRITICAL_LATENCY_MS}ms
Response: {result_repr}

Return ONLY the JSON verdict object."""

    messages = [
        {"role": "system", "content": _EVAL_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    # Trivial tools (BambooHR date-math: get_time_off, get_birthdays,
    # get_anniversaries, get_company_holidays) don't need chain-of-thought
    # to evaluate. Skipping think= halves the per-call VRAM pressure on the
    # 27B model and was the dominant source of "panic: failed to sample
    # token" / "CUDA: illegal memory access" infrastructure events.
    _TRIVIAL_EVAL_TOOLS = frozenset({
        "get_time_off", "get_birthdays",
        "get_anniversaries", "get_company_holidays",
    })
    think_for_eval = tool_name not in _TRIVIAL_EVAL_TOOLS

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            raw, reasoning = _chat(messages, model=config.MODEL, think=think_for_eval)
            v = _parse_json(raw)
            verdict_str = v.get("verdict", "fail")
            # Boundary check: assay is binary pass/fail throughout. If the model
            # drifts and emits anything else (warning, security, unknown), treat
            # it as fail so the verdict can't silently bypass downstream logic
            # that assumes pass/fail only.
            if verdict_str not in ("pass", "fail"):
                verdict_str = "fail"
            return Verdict(
                verdict=verdict_str,
                severity=v.get("severity", "info"),
                category=v.get("category", "correctness"),
                summary=v.get("summary", "")[:120],
                detail=v.get("detail", ""),
                interesting=bool(v.get("interesting", False)),
                reasoning_chain=reasoning,
            )
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            break

    return Verdict(
        verdict="unreachable", severity="info", category="infrastructure",
        summary=f"Evaluator unreachable: {str(last_err)[:80]}",
        detail=str(last_err), interesting=False, reasoning_chain="",
    )


def generate_followups(
    tool_name: str,
    case: TestCase,
    result: Any,
    verdict: Verdict,
) -> list[TestCase]:
    """Generate 2-3 follow-up test cases based on an interesting finding."""
    result_repr = _format_result_for_prompt(result, "", max_chars=1000) if result is not None else "null"

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
`name` MUST be snake_case. Return ONLY the JSON array."""

    try:
        # Follow-up generation: think=False for speed. Flip to True for overnight
        # rigorous runs if you want deeper probe-angle reasoning.
        raw, _ = _chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
            model=config.MODEL,
            think=False,
        )
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
