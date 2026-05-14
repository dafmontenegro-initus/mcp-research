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
2. **Generation** — asks a local LLM (DeepSeek-R1:32b) to generate 8–12 adversarial test cases per tool, covering happy paths, edge cases, invalid inputs, cross-tenant isolation, and stress
3. **Execution** — calls each tool via MCP and measures duration, errors, and raw output
4. **Evaluation** — asks the LLM to judge the result: `pass` / `fail` / `warning` / `security`
5. **Reflexion** — if a result is interesting, generates 2–3 deeper follow-up tests automatically and adds them to the queue

Reports land in `findings/YYYY-MM-DD_HHMM/report.md`.

---

## Model

| Component | Model | Purpose |
|-----------|-------|---------|
| Test generation & evaluation | `deepseek-r1:32b` | Generates adversarial test cases and evaluates results. DeepSeek-R1's explicit `<think>` reasoning makes its evaluations auditable — you can read exactly why it flagged something. |

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
