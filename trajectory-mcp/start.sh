#!/usr/bin/env bash
# Launches RAG service + MCP server in named tmux sessions.
# Safe to re-run — kills existing sessions first.
# Usage: bash start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Kill previous sessions if running ────────────────────────────────────────
tmux kill-session -t rag 2>/dev/null && echo "[~] Killed previous 'rag' session"
tmux kill-session -t mcp 2>/dev/null && echo "[~] Killed previous 'mcp' session"

# ── RAG service ───────────────────────────────────────────────────────────────
echo "[1/2] Starting RAG service (port 8090)..."
tmux new-session -d -s rag "cd $SCRIPT_DIR && rag_service/.venv/bin/python3 rag_service/app.py"

sleep 6
if curl -s http://localhost:8090/health > /dev/null 2>&1; then
    echo "      ✓ RAG service up"
else
    echo "      ⚠ RAG service not responding yet — check: tmux attach -t rag"
fi

# ── MCP server ────────────────────────────────────────────────────────────────
echo "[2/2] Starting MCP server (port 8080)..."
tmux new-session -d -s mcp "cd $SCRIPT_DIR && .venv/bin/python3 server.py"

sleep 2
if curl -s http://localhost:8080/mcp > /dev/null 2>&1; then
    echo "      ✓ MCP server up"
else
    echo "      ⚠ MCP server not responding yet — check: tmux attach -t mcp"
fi

echo ""
echo "Sessions running:"
tmux ls
echo ""
echo "Logs:  tmux attach -t rag   |   tmux attach -t mcp"
echo "Stop:  tmux kill-session -t rag; tmux kill-session -t mcp"