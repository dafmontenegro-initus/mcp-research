from __future__ import annotations

import httpx
from config import RAG_SERVICE_URL, OLLAMA_BASE_URL, OLLAMA_SUMMARIZE_MODEL

# These match the values in rag_service/embedder.py and rag_service/reranker.py.
# Kept in sync manually — if those change, update here too.
_EMBED_MODEL = "qwen3-embedding:4b-q4_K_M"
_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"

_MODELS = [
    {
        "name": _EMBED_MODEL,
        "type": "embedding",
        "purpose": "Generate vector embeddings for semantic search (RAG)",
        "justification": (
            "Qwen3 4B embedding model — multilingual (EN/ES), fast on A6000, "
            "~2 GB VRAM. Purpose-built for retrieval tasks."
        ),
        "hardware": "RTX A6000 via Ollama",
        "used_by": ["search_tasks", "search_meetings", "get_meeting_ticket_links", "ingest_document"],
    },
    {
        "name": _RERANKER_MODEL,
        "type": "reranker",
        "purpose": "Cross-encoder reranking of top-k RAG candidates",
        "justification": (
            "BAAI/bge-reranker-v2-m3 — multilingual cross-encoder that improves "
            "result relevance without requiring GPU. Runs on CPU via sentence-transformers."
        ),
        "hardware": "CPU (sentence-transformers)",
        "used_by": ["search_tasks", "search_meetings"],
    },
    {
        "name": OLLAMA_SUMMARIZE_MODEL,
        "type": "llm",
        "purpose": "Meeting transcript summarization focused on a specific Wrike ticket",
        "justification": (
            "Qwen3 30B — strong instruction-following, multilingual EN/ES, fits in one "
            "A6000 (48 GB VRAM). Runs locally with no external API calls."
        ),
        "hardware": "RTX A6000 via Ollama",
        "used_by": ["summarize_transcript_for_ticket"],
    },
]


def get_rag_health() -> dict:
    """
    Check the health of all RAG pipeline subsystems.

    Returns status for: RAG service (FastAPI + ChromaDB), Ollama embedding model,
    cross-encoder reranker, and Ollama LLM summarizer. Use this to diagnose
    search failures — if a subsystem is down, failures in search_tasks /
    search_meetings / get_meeting_ticket_links are infrastructure issues, not bugs.
    """
    result: dict = {
        "rag_service": {"available": False, "url": RAG_SERVICE_URL},
        "ollama_embedding": {"available": False, "model": _EMBED_MODEL},
        "reranker": {"available": False, "model": _RERANKER_MODEL},
        "ollama_summarizer": {"available": False, "model": OLLAMA_SUMMARIZE_MODEL},
    }

    # RAG service health (covers embedder + reranker)
    try:
        r = httpx.get(f"{RAG_SERVICE_URL}/health", timeout=5.0)
        r.raise_for_status()
        h = r.json()
        result["rag_service"]["available"] = h.get("status") == "ok"
        result["rag_service"]["version"] = h.get("version")
        result["ollama_embedding"]["available"] = bool(h.get("ollama"))
        result["reranker"]["available"] = bool(h.get("reranker"))
    except Exception as e:
        result["rag_service"]["error"] = str(e)

    # Ollama LLM summarizer (separate from RAG service embedding)
    try:
        r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5.0)
        r.raise_for_status()
        loaded = [m.get("name", "") for m in r.json().get("models", [])]
        result["ollama_summarizer"]["available"] = any(
            OLLAMA_SUMMARIZE_MODEL in m for m in loaded
        )
        result["ollama_summarizer"]["loaded_models"] = loaded
    except Exception as e:
        result["ollama_summarizer"]["error"] = str(e)

    return result


def get_index_stats(company_id: str) -> dict:
    """
    Return RAG index statistics for a company.

    Reports how many files and embedding chunks are indexed for the given company,
    when the index was last synced from S3, and whether it is considered stale.
    Useful for diagnosing empty search results — if indexed_files is 0, there are
    no embeddings to search against.

    Parameters
    ----------
    company_id : Company identifier (e.g. "NWN", "ADV").
    """
    try:
        r = httpx.get(
            f"{RAG_SERVICE_URL}/stats/{company_id.upper()}",
            timeout=30.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        return {"error": "RAG service unavailable. Start rag_service/app.py first."}
    except Exception as e:
        return {"error": f"Stats request failed: {e}"}


def list_models() -> dict:
    """
    Return the complete inventory of AI models used by this server.

    Lists all local models (embedding, reranker, LLM), their purpose,
    technical justification, hardware requirements, and which MCP tools
    depend on them. Also checks live availability for each model.

    Use this before any session involving semantic search or summarization
    to verify the AI stack is operational.
    """
    health = get_rag_health()

    availability: dict[str, bool] = {
        _EMBED_MODEL: health.get("ollama_embedding", {}).get("available", False),
        _RERANKER_MODEL: health.get("reranker", {}).get("available", False),
        OLLAMA_SUMMARIZE_MODEL: health.get("ollama_summarizer", {}).get("available", False),
    }

    models_with_status = [
        {**m, "available": availability.get(m["name"], False)}
        for m in _MODELS
    ]

    return {
        "total": len(models_with_status),
        "all_available": all(m["available"] for m in models_with_status),
        "models": models_with_status,
    }
