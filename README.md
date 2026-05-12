# mcp-research

Research and implementation of Trajectory's local MCP infrastructure.

## Structure

```
mcp-research/
├── trajectory-mcp/         ← MCP server (production-ready)
│   ├── server.py           ← FastMCP server, 18 tools
│   ├── tools/              ← meetings, wrike, bamboohr tools
│   ├── rag_service/        ← local RAG (ChromaDB + Ollama + reranker)
│   ├── tests/              ← QA and connectivity tests
│   ├── docs/               ← installation guide
│   ├── README.md           ← full API reference and WSR system prompt
│   ├── RUNNING.md          ← how to start everything
│   └── start.sh            ← one-command background launcher
│
└── research_notes/         ← strategy docs (not all files committed)
    └── estrategia_lanchas_rapidas_v2.pdf
```

## Quick start

```bash
# See trajectory-mcp/RUNNING.md for the full guide
cd trajectory-mcp
python3 tests/test_connections.py   # verify all connections first
```

## Architecture

`trajectory-mcp` is a read-only MCP server that powers the WSR investigation flow.
It exposes 18 tools across meetings, Wrike, BambooHR, and semantic search (RAG).
The RAG layer runs locally on machine_20 — Ollama for embeddings, sentence-transformers
for reranking, ChromaDB for persistent vector storage.

The official Wrike connector (separate) handles reading formatted ticket content
and creating new WSR tickets. trajectory-mcp does investigation only.
