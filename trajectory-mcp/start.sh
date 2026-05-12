#!/usr/bin/env bash
# Quick-start: launches RAG service + MCP server in two background processes.
# Run from trajectory-mcp/  (bash start.sh)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── RAG service ───────────────────────────────────────────────────────────────
echo "[1/2] Starting RAG service (port 8090)..."
nohup rag_service/.venv/bin/python3 rag_service/app.py \
    > /tmp/rag_service.log 2>&1 &
RAG_PID=$!
echo "      PID $RAG_PID  →  logs: /tmp/rag_service.log"

# Give it a moment to bind the port
sleep 2
if curl -s http://localhost:8090/health > /dev/null 2>&1; then
    echo "      ✓ RAG service up"
else
    echo "      ⚠ RAG service not responding yet — check /tmp/rag_service.log"
fi

# ── MCP server ────────────────────────────────────────────────────────────────
echo "[2/2] Starting MCP server (port 8000)..."
nohup .venv/bin/python3 server.py \
    > /tmp/trajectory_mcp.log 2>&1 &
MCP_PID=$!
echo "      PID $MCP_PID  →  logs: /tmp/trajectory_mcp.log"

sleep 1
if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo "      ✓ MCP server up"
else
    echo "      ⚠ MCP server not responding yet — check /tmp/trajectory_mcp.log"
fi

echo ""
echo "Both services started. Stop with:"
echo "  kill $RAG_PID $MCP_PID"
echo "  # or: pkill -f 'rag_service/app.py'; pkill -f 'server.py'"
