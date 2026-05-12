# Running trajectory-mcp

```bash
bash ~/workspace/mcp-research/trajectory-mcp/start.sh
```

Two separate processes, two separate virtual environments.

```
trajectory-mcp/
├── .venv/          ← MCP server venv  (pip install -r requirements.txt)
├── server.py       ← starts on :8080 (HTTP) or stdio
└── rag_service/
    ├── .venv/      ← RAG service venv (pip install -r rag_service/requirements.txt)
    └── app.py      ← starts on :8090
```

---

## First-time setup

### 1. Ollama models (do once on machine_20)
```bash
# Embedding model — required for RAG search (search_tasks, search_meetings)
ollama pull qwen3-embedding:4b-q4_K_M

# Summarization model — required for summarize_transcript_for_ticket
# (falls back to raw transcript if not pulled)
# qwen3:30b = MoE 30B/3B-activated, 256K context, 19GB Q4_K_M — think mode disabled at runtime
ollama pull qwen3:30b
```

### 2. MCP server venv
```bash
cd ~/workspace/mcp-research/trajectory-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. RAG service venv
```bash
cd ~/workspace/mcp-research/trajectory-mcp/rag_service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # ~500 MB first time (sentence-transformers, chromadb)
```

### 4. Environment variables
```bash
# Already done — .env is in trajectory-mcp/.env
# Verify with:
python3 tests/test_connections.py
```

---

## Running

Open **two terminals** on machine_20 (or two tmux panes).

### Terminal A — RAG service
```bash
cd ~/workspace/mcp-research/trajectory-mcp/rag_service
source .venv/bin/activate
python3 app.py
# Listening on :8090
# First search for a company will take a few seconds to load its S3 pickles
```

### Terminal B — MCP server
```bash
cd ~/workspace/mcp-research/trajectory-mcp
source .venv/bin/activate
python3 server.py                    # multi-tenant, HTTP :8080
# or
python3 server.py --company NWN      # scoped to one company, HTTP :8080
# or
python3 server.py --stdio            # for Claude Desktop / stdio transport
```

### tmux (recommended — survives disconnects, logs visible on re-attach)
```bash
# Start both services in detached sessions
tmux new-session -d -s rag 'cd /home/daniel/workspace/mcp-research/trajectory-mcp && rag_service/.venv/bin/python3 rag_service/app.py'
tmux new-session -d -s mcp 'cd /home/daniel/workspace/mcp-research/trajectory-mcp && .venv/bin/python3 server.py'

# Check sessions are running
tmux ls

# Attach to see live logs (Ctrl+B then D to detach without killing)
tmux attach -t rag
tmux attach -t mcp

# Stop
tmux kill-session -t rag
tmux kill-session -t mcp
```

> **Note:** if `tmux new-session` exits immediately, the port is likely already in use.
> Check with `ss -tlnp | grep -E "8080|8090"` and kill the existing process first.

### Background (nohup)
```bash
nohup rag_service/.venv/bin/python3 rag_service/app.py > /tmp/rag_service.log 2>&1 &
nohup .venv/bin/python3 server.py > /tmp/trajectory_mcp.log 2>&1 &

tail -f /tmp/rag_service.log /tmp/trajectory_mcp.log   # watch both
pkill -f "rag_service/app.py"    # stop RAG service
pkill -f "python3 server.py"     # stop MCP server
```

---

## Smoke tests

```bash
# All connections green?
python3 tests/test_connections.py

# RAG service health
curl -s http://localhost:8090/health | python3 -m json.tool

# Semantic search (loads NWN pickles on first run)
curl -s -X POST http://localhost:8090/search/tasks \
  -H "Content-Type: application/json" \
  -d '{"query":"deployment risk","company_id":"NWN","top_k":5}' | python3 -m json.tool

# Ingest a document from S3
curl -s -X POST http://localhost:8090/ingest/document \
  -H "Content-Type: application/json" \
  -d '{"s3_key":"assets-tj-prod/wrike/NWN/TICKET_ID/TICKET_ID.pkl",
       "company_id":"NWN","ticket_id":"TICKET_ID"}' | python3 -m json.tool

# MCP Inspector (requires Node.js)
npx @modelcontextprotocol/inspector http://localhost:8080/mcp
```

---

## Claude Desktop config

The MCP server runs persistently on machine_20 (port 8080). Claude Desktop connects via
`mcp-remote`, which bridges HTTP → stdio. No SSH, no local Python required.

**Config file location:**
- Windows: `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "Trajectory": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://64.137.145.121:8080/mcp", "--allow-http"]
    }
  }
}
```

`--allow-http` is required because the server uses plain HTTP (not HTTPS).
Requires Node.js installed on the client machine (`npx` must be available).

After editing the config, restart Claude Desktop.

---

## Ports summary

| Service | Port | Notes |
|---------|------|-------|
| MCP server | 8080 | HTTP streamable-http transport |
| RAG service | 8090 | FastAPI, internal only |
| Ollama | 11434 | Embedding model |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `search_tasks` returns `"RAG service unavailable"` | Start `rag_service/app.py` |
| First search is slow (10–30s) | Normal — loading S3 pickles into ChromaDB for that company |
| `ollama: false` in `/health` | Run `ollama pull qwen3-embedding:4b-q4_K_M` |
| `reranker: false` in `/health` | Run `pip install sentence-transformers` in rag_service venv |
| BambooHR 403 errors | BambooHR restricts by IP — needs access from office network or VPN |
| `No Wrike data available for company X` | Company code not in Wrike DB — check `list_companies` |
