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

Each tool is tested in isolation with a fresh LLM context:

1. **Discovery** — collects real IDs (meeting UUIDs, ticket IDs, company names, RAG health) from the live server so tests use realistic data
2. **Generation** — asks the LLM to produce 8–12 adversarial test cases per tool, covering happy paths, edge cases, invalid inputs, cross-tenant isolation, and stress
3. **Execution** — calls each tool via MCP and measures duration, errors, and raw output
4. **Evaluation** — the same LLM (now in evaluator mode, with thinking enabled) judges the result against BOTH the test intent AND the documented contract, in a single prompt. Verdict is binary: `pass` or `fail`. Failure type is in `category` (`correctness` | `security` | `performance` | `data_quality` | `error_handling`).
5. **Reflexion** — if a result is interesting, generates 2–3 deeper follow-up tests automatically and adds them to the queue

Tools are tested **sequentially**, one at a time. Within a tool, test cases also run one after another. Concurrent inference on the same Ollama instance crashed the runner on the deployment hardware (Ollama 0.23.2 + 2× RTX A6000) with cgo signal errors — serial execution is the stable choice and is the only knob worth its complexity here.

Infrastructure failures (Ollama runner crashes, evaluator unreachable after retries) are tagged `verdict="unreachable"` and routed to `infrastructure_events` in the report — they never pollute `actionable_findings` as fake bugs in the system under test.

Reports land in `findings/YYYY-MM-DD_HHMM/` as `report.md` (human-readable), `findings.json` (full data), and `claude_context.json` (AI-optimized, priority-ranked).

---

## Model — single evaluator

Assay uses one LLM for everything: **`qwen3.6:27b`** (Alibaba Cloud). It plays two roles:

| Role | What it does |
|------|--------------|
| Generator | Produces 8–12 adversarial test cases per tool. `think=False` — the prompt is prescriptive (fill a JSON template), reasoning adds no value, only latency. |
| Evaluator | Judges each tool call result. `think=True` — the merged intent+contract evaluation is the one place where reasoning materially improves the verdict. The full reasoning chain is captured in the report for root-cause analysis. |

An earlier dual-layer design (generator-judge + independent auditor in a different model) couldn't run reliably on the deployment hardware — multi-GPU coordination in Ollama 0.23.2 left runner processes stranded and crashed every auditor model we tried (granite4.1 in 8b and 30b). Single-evaluator removes the second model load entirely. The merged evaluator prompt preserves both perspectives in one prompt, with explicit "you designed this test, now also check the contract" framing.

---

## Configuration

`.env` (or env vars):

```
MCP_URL=http://localhost:8080
MCP_TOKEN=traj_<token>
OLLAMA_URL=http://localhost:11434
MODEL=qwen3.6:27b
SLOW_MS=5000
CRITICAL_LATENCY_MS=15000
MAX_FOLLOWUPS=3
```

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
