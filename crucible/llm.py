"""
LLM interface for crucible — all calls go through Ollama's OpenAI-compatible API.

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
You are crucible, an autonomous QA agent testing the trajectory-mcp server.

## About the system under test
trajectory-mcp is a read-only MCP server that exposes data from:
- Zoom meetings (transcripts, participants, chat, summaries)
- Wrike project management (tasks, timelines, attachments)
- BambooHR (time off, birthdays, anniversaries, holidays)
- Semantic search via RAG (ChromaDB + Ollama embeddings)

It is a multi-tenant system: each tool requires a `company_id` to scope the query
to a specific client. The system serves ~31 companies.

## NWN — primary stress-test target
NWN is by far the largest client. It has:
- The most meetings, the longest transcripts
- The most Wrike tickets and project history
- The deepest data in semantic search

When you need to test for latency, volume, or real-data behavior, use NWN.
For multi-tenant isolation tests, compare NWN data against a smaller company.

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
    resp = _client.chat.completions.create(
        model=config.MODEL,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content or ""


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

IMPORTANT: All string values in "arguments" must use only double quotes.
Avoid apostrophes or special characters inside string values.

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
