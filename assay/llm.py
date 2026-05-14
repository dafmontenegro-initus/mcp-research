"""
LLM interface for assay — all calls go through the Ollama native REST API.

Public functions:
  generate_test_cases(tool, context) -> list[TestCase]           uses config.MODEL
  evaluate_combined(tool, case, result) -> CombinedVerdict       dual-layer evaluation
  generate_followups(tool, case, result, verdict) -> list[TestCase]  uses config.MODEL

Dual-layer evaluation: generator-as-judge + triple-model independent jury.

Each test result is evaluated by two complementary layers in parallel:
  Layer 1 — Generator-as-judge (qwen3.6:27b, Alibaba Cloud):
    Evaluates from the requester perspective: "did the tool do what I asked?"
    This model designed the test case and knows the intent.
  Layer 2 — Independent jury (deepseek-r1:8b · gemma3:12b · granite4.1:8b):
    Three judges from different organizations (DeepSeek AI, Google DeepMind,
    IBM Research) evaluate from the contract perspective — no knowledge of how
    the test was generated. 3 judges, odd count, majority always possible.

Final verdict: PASS only if both layers approve (AND logic).
  - generator passes AND jury majority passes (≥2/3) → PASS
  - generator fails OR jury fails → FAIL (with layer_consensus tag)

Verdicts are binary: "pass" or "fail". No "warning", no "security" as verdict —
these become category values explaining WHY a result is "fail".

All four LLM calls fire in parallel — no latency increase vs. 3-judge system.

Reasoning capture: DeepSeek-R1 emits thinking via message.thinking (think=true).
Qwen3 and other models may emit <think>...</think> blocks in content, captured
by _extract_reasoning(). reasoning_chain is stored in reports for root-cause analysis.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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

Always respond with valid JSON only. No markdown, no explanation outside JSON."""


# ── Judge system prompt (Layer 2: independent jury) ───────────────────────────

_JUDGE_SYSTEM = """\
You are an impartial QA judge auditing MCP tool calls for trajectory-mcp.

Your role is strictly evaluative. You did NOT generate the test being evaluated.
You have no stake in the outcome. Your mandate: find real failures — not excuses for them.

## System under test
trajectory-mcp is a read-only MCP server exposing:
- Zoom meetings (transcripts, participants, chat, summaries)
- Wrike project management (tasks, timelines, attachments)
- BambooHR (time off, birthdays, anniversaries, holidays) — Trajectory-wide, NO company_id
- Semantic search via RAG (ChromaDB + Ollama embeddings)
- Diagnostics (RAG health, index stats, model inventory)

Multi-tenant: most tools scope data by company_id. ~31 companies served.

## Evaluation standard
Judge the tool call against its documented behavior (name + description + input schema).
Ask yourself three questions:
1. Does the response match what the tool documentation promises?
2. Is the data structurally and semantically correct?
3. Are there signs of failure: server errors, wrong tenant data, missing required fields?

## Verdict rules — binary, no ambiguity
- verdict=pass: the tool did EXACTLY what its documentation says. Perfect behavior only.
  Pass includes graceful error responses for invalid inputs — that IS correct behavior.
- verdict=fail: anything less than perfect. Use category to explain why:
    correctness   — wrong data, missing fields, incorrect logic
    security      — data from a different tenant than requested (cross-tenant leak)
    performance   — unacceptable latency or resource usage
    data_quality  — malformed, truncated, or inconsistent data
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

## Output
Return ONLY a JSON object — no explanation, no markdown:
{
  "verdict": "pass | fail",
  "severity": "critical | major | minor | info",
  "category": "correctness | security | performance | data_quality | error_handling",
  "summary": "one-line finding (max 80 chars)",
  "detail": "2-3 sentences: what happened, why it matters, what the correct behavior is",
  "interesting": true or false
}"""


# ── Generator-as-judge system prompt (Layer 1) ────────────────────────────────

_GENERATOR_JUDGE_SYSTEM = """\
You are assay, the QA agent that designed this test case. Now evaluate whether
the tool's response satisfied the intent of your test.

Your role is DIFFERENT from an independent contract auditor:
- You know exactly what you asked for and why
- Evaluate: did the tool respond to what you requested?
- Focus on: was the intent fulfilled? Did the tool understand the request?

This is Layer 1 of a dual evaluation. An independent jury handles contract
compliance (Layer 2). Together, both layers determine the final verdict.

## Verdict rules — binary, no ambiguity
- verdict=pass: the tool did EXACTLY what was requested. Perfect behavior only.
- verdict=fail: anything less than perfect. Use category to explain why:
    correctness   — wrong data, missing fields, incorrect logic
    security      — data from a different tenant than requested (cross-tenant leak)
    performance   — unacceptable latency or resource usage
    data_quality  — malformed, truncated, or inconsistent data
    error_handling — crash, unhandled exception, or cryptic error message
- interesting=true: a follow-up test would likely reveal more about this behavior

Return ONLY a JSON verdict object — no markdown, no explanation:
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
    Handles DeepSeek/Qwen3 <think>...</think> blocks.
    Returns ("", text) if no thinking block found.
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


def _chat(messages: list[dict], temperature: float = 0.3, model: str | None = None) -> tuple[str, str]:
    """
    Call Ollama native REST API. Returns (clean_content, reasoning_chain).

    Uses think=true only for DeepSeek — DeepSeek-R1 returns reasoning in
    message.thinking. Other models respond normally; if they emit <think> blocks
    in content, _extract_reasoning() captures them as fallback.
    """
    effective_model = model or config.MODEL
    model_lower = effective_model.lower()

    payload: dict = {
        "model": effective_model,
        "messages": messages,
        "stream": False,
        "options": {"num_ctx": 8192, "temperature": temperature},
    }
    if "deepseek" in model_lower:
        payload["think"] = True

    url = f"{config.OLLAMA_URL}/api/chat"

    for attempt in range(3):
        try:
            resp = _requests.post(url, json=payload, timeout=300)
            if not resp.ok:
                body = resp.json() if resp.content else {}
                raise RuntimeError(f"{resp.status_code} — {body.get('error', resp.text[:200])}")
            data = resp.json()
            msg_data = data.get("message", {})
            content = msg_data.get("content", "").strip()
            thinking = msg_data.get("thinking", "").strip()

            # Fallback: some models embed <think> in content instead of message.thinking
            if not thinking and content:
                thinking, content = _extract_reasoning(content)

            return content, thinking
        except Exception as e:
            err = str(e)
            if "500" in err or "runner" in err or "signal" in err or "EOF" in err:
                if attempt < 2:
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
    verdict: str
    severity: str
    category: str
    summary: str
    detail: str
    interesting: bool


@dataclass
class JudgeVerdict:
    model: str
    verdict: str           # pass | fail
    severity: str          # critical | major | minor | info
    category: str          # correctness | security | performance | data_quality | error_handling
    summary: str
    detail: str
    interesting: bool
    reasoning_chain: str   # raw thinking content; "" if model has no thinking mode


@dataclass
class JuryVerdict:
    verdicts: list[JudgeVerdict]  # one per judge, in config.JUDGE_MODELS order
    final_verdict: str
    final_severity: str
    final_category: str
    final_summary: str
    final_detail: str
    consensus: str                # "unanimous" | "majority"
    interesting: bool             # true if ANY judge marked interesting


@dataclass
class CombinedVerdict:
    generator_judge: JudgeVerdict   # Layer 1: generator's self-evaluation
    jury: JuryVerdict               # Layer 2: 3 independent judges
    final_verdict: str              # "pass" only if BOTH layers pass
    final_severity: str
    final_category: str
    final_summary: str
    final_detail: str
    layer_consensus: str            # "both_pass" | "both_fail" | "generator_only_fail" | "jury_only_fail"
    interesting: bool               # True if EITHER layer flags interesting


# ── Jury aggregation ───────────────────────────────────────────────────────────

_VERDICT_RANK = {"fail": 0, "pass": 1}
_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def _aggregate_jury(verdicts: list[JudgeVerdict]) -> JuryVerdict:
    vote_counts = Counter(v.verdict for v in verdicts)
    top_verdict, top_count = vote_counts.most_common(1)[0]
    n = len(verdicts)
    consensus = "unanimous" if top_count == n else "majority"

    majority = [v for v in verdicts if v.verdict == top_verdict]
    anchor = max(majority, key=lambda v: len(v.reasoning_chain))
    final_severity = min(majority, key=lambda v: _SEVERITY_RANK.get(v.severity, 4)).severity

    return JuryVerdict(
        verdicts=verdicts,
        final_verdict=top_verdict,
        final_severity=final_severity,
        final_category=anchor.category,
        final_summary=anchor.summary,
        final_detail=anchor.detail,
        consensus=consensus,
        interesting=any(v.interesting for v in verdicts),
    )


# ── Internal LLM callers ───────────────────────────────────────────────────────

def _call_generator_judge(
    tool_name: str,
    case: TestCase,
    result: Any,
    duration_ms: int,
    is_error: bool,
    error_message: str,
) -> JudgeVerdict:
    """Layer 1: generator evaluates its own test from the requester perspective."""
    result_repr = json.dumps(result, default=str)[:2000] if result is not None else f"ERROR: {error_message}"
    prompt = f"""You designed this test. Now evaluate whether the tool fulfilled your intent.

## Tool: {tool_name}
## Your test
Name: {case.name}
Description: {case.description}
Arguments: {json.dumps(case.arguments)}
Expected behavior: {case.expected_behavior}

## Actual result
Duration: {duration_ms}ms
Is error: {is_error}
Response: {result_repr}

Return ONLY the JSON verdict object."""

    messages = [
        {"role": "system", "content": _GENERATOR_JUDGE_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    last_err = None
    for attempt in range(3):
        try:
            raw, reasoning = _chat(messages, temperature=0.1, model=config.MODEL)
            v = _parse_json(raw)
            return JudgeVerdict(
                model=config.MODEL,
                verdict=v.get("verdict", "fail"),
                severity=v.get("severity", "info"),
                category=v.get("category", "correctness"),
                summary=v.get("summary", "")[:120],
                detail=v.get("detail", ""),
                interesting=bool(v.get("interesting", False)),
                reasoning_chain=reasoning,
            )
        except Exception as e:
            last_err = e
            if "runner" in str(e) or "500" in str(e) or "EOF" in str(e):
                time.sleep(10 * (attempt + 1))
    return JudgeVerdict(
        model=config.MODEL, verdict="fail", severity="minor",
        category="error_handling",
        summary=f"Generator-judge unreachable after 3 attempts: {str(last_err)[:60]}",
        detail=str(last_err), interesting=False, reasoning_chain="",
    )


def _make_judge_caller(judge_messages: list[dict]):
    """Returns a callable that calls a single judge with retry."""
    def _call(judge_model: str) -> JudgeVerdict:
        last_err = None
        for attempt in range(3):
            try:
                raw, reasoning = _chat(judge_messages, temperature=0.1, model=judge_model)
                v = _parse_json(raw)
                return JudgeVerdict(
                    model=judge_model,
                    verdict=v.get("verdict", "fail"),
                    severity=v.get("severity", "info"),
                    category=v.get("category", "correctness"),
                    summary=v.get("summary", "")[:120],
                    detail=v.get("detail", ""),
                    interesting=bool(v.get("interesting", False)),
                    reasoning_chain=reasoning,
                )
            except Exception as e:
                last_err = e
                if "runner" in str(e) or "500" in str(e) or "EOF" in str(e):
                    time.sleep(10 * (attempt + 1))
        return JudgeVerdict(
            model=judge_model, verdict="fail", severity="minor",
            category="error_handling",
            summary=f"Judge unreachable after 3 attempts: {str(last_err)[:60]}",
            detail=str(last_err), interesting=False, reasoning_chain="",
        )
    return _call


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
        raw, _ = _chat(messages, model=config.MODEL)
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


def evaluate_combined(
    tool_name: str,
    case: TestCase,
    result: Any,
    duration_ms: int,
    is_error: bool,
    error_message: str,
    on_judge: "callable | None" = None,
) -> CombinedVerdict:
    """
    Dual-layer evaluation: generator-as-judge (Layer 1) + independent jury (Layer 2).
    All four LLM calls fire in parallel. Final verdict = AND(layer1_pass, layer2_pass).
    Each judge retries up to 3 times on transient errors; failure yields verdict=fail.
    """
    result_repr = json.dumps(result, default=str)[:2000] if result is not None else f"ERROR: {error_message}"
    judge_prompt = f"""Audit this MCP tool call against its documented contract.

## Tool: {tool_name}
## Input
Arguments: {json.dumps(case.arguments)}

## Result
Duration: {duration_ms}ms
Is error: {is_error}
Response: {result_repr}

## Performance thresholds
- Warning: > {config.SLOW_MS}ms
- Critical: > {config.CRITICAL_LATENCY_MS}ms

Return ONLY the JSON verdict object."""

    judge_messages = [{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": judge_prompt}]
    _call_judge = _make_judge_caller(judge_messages)

    with ThreadPoolExecutor(max_workers=4) as pool:
        gen_future = pool.submit(
            _call_generator_judge, tool_name, case, result, duration_ms, is_error, error_message
        )
        judge_futures = [(m, pool.submit(_call_judge, m)) for m in config.JUDGE_MODELS]

    if on_judge:
        on_judge(0, len(config.JUDGE_MODELS) + 1, config.MODEL)

    generator_judge = gen_future.result()
    judge_verdicts: list[JudgeVerdict] = []
    for idx, (model, fut) in enumerate(judge_futures):
        if on_judge:
            on_judge(idx + 1, len(config.JUDGE_MODELS) + 1, model)
        judge_verdicts.append(fut.result())

    jury = _aggregate_jury(judge_verdicts)

    gen_passes = generator_judge.verdict == "pass"
    jury_passes = jury.final_verdict == "pass"

    if gen_passes and jury_passes:
        layer_consensus = "both_pass"
        final_verdict = "pass"
        final_summary = jury.final_summary
        final_severity = "info"
        final_category = jury.final_category
        final_detail = jury.final_detail
    elif not gen_passes and not jury_passes:
        layer_consensus = "both_fail"
        final_verdict = "fail"
        final_summary = jury.final_summary
        final_severity = jury.final_severity
        final_category = jury.final_category
        final_detail = jury.final_detail
    elif not gen_passes:
        layer_consensus = "generator_only_fail"
        final_verdict = "fail"
        final_summary = generator_judge.summary
        final_severity = generator_judge.severity
        final_category = generator_judge.category
        final_detail = generator_judge.detail
    else:
        layer_consensus = "jury_only_fail"
        final_verdict = "fail"
        final_summary = jury.final_summary
        final_severity = jury.final_severity
        final_category = jury.final_category
        final_detail = jury.final_detail

    return CombinedVerdict(
        generator_judge=generator_judge,
        jury=jury,
        final_verdict=final_verdict,
        final_severity=final_severity,
        final_category=final_category,
        final_summary=final_summary,
        final_detail=final_detail,
        layer_consensus=layer_consensus,
        interesting=generator_judge.interesting or jury.interesting,
    )


def generate_followups(
    tool_name: str,
    case: TestCase,
    result: Any,
    verdict: "Verdict | JuryVerdict | CombinedVerdict",
) -> list[TestCase]:
    """Generate 2-3 follow-up test cases based on an interesting finding."""
    result_repr = json.dumps(result, default=str)[:1000] if result is not None else "null"

    if isinstance(verdict, CombinedVerdict):
        summary = verdict.final_summary
        detail = verdict.final_detail
    elif isinstance(verdict, JuryVerdict):
        summary = verdict.final_summary
        detail = verdict.final_detail
    else:
        summary = verdict.summary
        detail = verdict.detail

    prompt = f"""A test for {tool_name} produced an interesting result. Generate 2 or 3 follow-up test cases to dig deeper.

## Original test
Name: {case.name}
Arguments: {json.dumps(case.arguments)}
Response: {result_repr}

## Finding
{summary}
{detail}

Generate follow-up tests that probe this finding further.
Return a JSON array (same schema as before: name, description, arguments, expected_behavior).
Return ONLY the JSON array."""

    try:
        raw, _ = _chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
            model=config.MODEL,
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
