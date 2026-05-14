# Assay

**Assay** /ˈæseɪ/ — from Old French *essai*, "to try, to test." In metallurgy, an assay is
the formal process of testing the purity and quality of a precious metal. You place the
sample in a crucible, apply controlled heat, and measure exactly what comes out. The assay
does not guess — it produces a verdict.

This is that process, automated. Assay is an autonomous QA agent that tests every tool
exposed by trajectory-mcp: it applies adversarial inputs, measures real responses against
expected behavior, and issues a structured verdict for each finding. Nothing is assumed
correct until it is tested.

---

## How it works

Assay implements a **Reflexion loop** — each tool is tested in isolation with a fresh LLM context:

1. **Discovery** — collects real IDs (meeting UUIDs, ticket IDs, company names, RAG health) from the live server so tests use realistic data
2. **Generation** — asks a local LLM (generator) to produce 8–12 adversarial test cases per tool, covering happy paths, edge cases, invalid inputs, cross-tenant isolation, and stress
3. **Execution** — calls each tool via MCP and measures duration, errors, and raw output
4. **Evaluation** — a completely independent judge LLM evaluates the result: `pass` / `fail` / `warning` / `security`
5. **Reflexion** — if a result is interesting, generates 2–3 deeper follow-up tests automatically and adds them to the queue

Tools are tested in parallel — up to `MAX_WORKERS` tools run simultaneously, each with its own MCP session. LLM calls queue naturally in Ollama while MCP I/O overlaps.

Reports land in `findings/YYYY-MM-DD_HHMM/report.md`.

---

## Models — Dual-Model Jury

Assay uses two independent models: a **generator** that creates adversarial test cases and a
**judge** that evaluates results. The judge never sees the generator's reasoning or expected
behavior — it evaluates solely from the tool's documented contract. This eliminates
self-evaluation bias: the judge cannot rationalize a bad result just because it generated the test.

| Role | Model | Purpose |
|------|-------|---------|
| Generator | `qwen3.6:27b` | Produces 8–12 adversarial test cases per tool. Shared with the MCP summarizer — Ollama serves both from a single loaded instance on GPU 1. |
| Judge | `deepseek-r1:32b` | Evaluates each tool call result at temperature 0.1. Different architecture and training data from the generator — true independence. Thinking mode (`<think>...</think>`) makes every verdict auditable. Runs on GPU 0 alongside the embedding model. |

To use the same model for both (isolated context still helps), remove `JUDGE_MODEL` from `.env`.

---

## Usage

```bash
bash start.sh                          # full run — all tools
bash start.sh --tool search_tasks      # single tool
bash start.sh --dry-run                # connect and list tools only, no LLM
```

```bash
tmux attach -t assay                   # watch live output
tmux kill-session -t assay             # stop
```
