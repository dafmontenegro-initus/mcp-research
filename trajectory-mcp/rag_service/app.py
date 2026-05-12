from __future__ import annotations

import os
from pathlib import Path

# Load .env from the parent trajectory-mcp directory
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import embedder
import reranker
from chroma_store import get_collection, search
from pickle_sync import ensure_warm

RAG_SERVICE_VERSION = "1.0.0"


# ── Request / Response models ────────────────────────────────────────────────

class SearchTasksRequest(BaseModel):
    query: str
    company_id: str
    environment: str = "prod"
    top_k: int = 10


class SearchMeetingsRequest(BaseModel):
    query: str
    company_id: str
    environment: str = "prod"
    top_k: int = 10


class IngestDocumentRequest(BaseModel):
    s3_key: str
    company_id: str
    ticket_id: str
    environment: str = "prod"
    doc_type: str = "pdf"  # pdf | image | text


# ── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[rag_service] v{RAG_SERVICE_VERSION} starting")
    print(f"[rag_service] Ollama available: {embedder.is_available()}")
    print(f"[rag_service] Reranker loading...")
    reranker.is_available()  # warm up on startup
    print(f"[rag_service] Ready")
    yield


app = FastAPI(title="trajectory-mcp RAG Service", version=RAG_SERVICE_VERSION, lifespan=lifespan)


def _unavailable() -> dict:
    return {"error": "RAG service unavailable: Ollama model not loaded. Run: ollama pull qwen3-embedding:4b-q4_K_M"}


# ── /health ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": RAG_SERVICE_VERSION,
        "ollama": embedder.is_available(),
        "reranker": reranker.is_available(),
    }


# ── /search/tasks ────────────────────────────────────────────────────────────

@app.post("/search/tasks")
def search_tasks(req: SearchTasksRequest):
    if not embedder.is_available():
        return _unavailable()

    # 1. Warm up cache (lazy loading from S3)
    sync_result = ensure_warm(req.company_id, "wrike", req.environment)
    if "error" in sync_result:
        return {"error": sync_result["error"], "results": []}

    # 2. Embed the query
    try:
        query_embedding = embedder.embed(req.query)
    except Exception as e:
        return {"error": f"Embedding failed: {e}", "results": []}

    # 3. Search ChromaDB — fetch more candidates than needed for reranking
    collection = get_collection(f"wrike_{req.environment}")
    candidates = search(
        collection,
        query_embedding,
        where={"company_id": req.company_id},
        n_results=min(50, max(req.top_k * 5, 20)),
    )

    if not candidates:
        return {"results": [], "message": "No results found. The index may be empty for this company."}

    # 4. Rerank
    ranked = reranker.rerank(req.query, candidates)
    top = ranked[: req.top_k]

    # 5. Format LLM-friendly output
    results = []
    for hit in top:
        meta = hit.get("metadata", {})
        results.append({
            "rank": hit["rank"],
            "ticket_id": meta.get("item_id", ""),
            "excerpt": hit.get("document", "")[:500],
        })

    return {"results": results, "total_candidates": len(candidates)}


# ── /search/meetings ─────────────────────────────────────────────────────────

@app.post("/search/meetings")
def search_meetings(req: SearchMeetingsRequest):
    if not embedder.is_available():
        return _unavailable()

    sync_result = ensure_warm(req.company_id, "meetings", req.environment)
    if "error" in sync_result:
        return {"error": sync_result["error"], "results": []}

    try:
        query_embedding = embedder.embed(req.query)
    except Exception as e:
        return {"error": f"Embedding failed: {e}", "results": []}

    collection = get_collection(f"meetings_{req.environment}")
    candidates = search(
        collection,
        query_embedding,
        where={"company_id": req.company_id},
        n_results=min(50, max(req.top_k * 5, 20)),
    )

    if not candidates:
        return {"results": [], "message": "No results found. The meetings index may be empty."}

    ranked = reranker.rerank(req.query, candidates)
    top = ranked[: req.top_k]

    results = []
    for hit in top:
        meta = hit.get("metadata", {})
        results.append({
            "rank": hit["rank"],
            "meeting_uuid": meta.get("item_id", ""),
            "excerpt": hit.get("document", "")[:500],
        })

    return {"results": results, "total_candidates": len(candidates)}


# ── /ingest/document ─────────────────────────────────────────────────────────

@app.post("/ingest/document")
def ingest_document(req: IngestDocumentRequest):
    """
    Ingest a document from S3 into ChromaDB on demand.
    Currently supports: pdf (text extraction via PyMuPDF), text files.
    Image support (via Ollama vision) is planned for a future iteration.
    """
    if not embedder.is_available():
        return _unavailable()

    import boto3
    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))

    # Parse bucket and key from s3_key (supports "bucket/key" or just "key")
    parts = req.s3_key.split("/", 1)
    if len(parts) == 2 and not req.s3_key.startswith("/"):
        bucket, key = parts
    else:
        return {"error": "s3_key must be in 'bucket/key' format"}

    try:
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as e:
        return {"error": f"Cannot read {req.s3_key}: {e}"}

    # Extract text
    text = ""
    if req.doc_type == "pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=raw, filetype="pdf")
            text = "\n".join(page.get_text() for page in doc)
        except Exception as e:
            return {"error": f"PDF extraction failed: {e}"}
    elif req.doc_type == "text":
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            return {"error": f"Text decode failed: {e}"}
    elif req.doc_type == "image":
        return {"error": "Image ingestion requires a vision model (planned feature). Use doc_type=pdf or doc_type=text."}
    else:
        return {"error": f"Unknown doc_type: {req.doc_type}. Supported: pdf, text, image"}

    if not text.strip():
        return {"error": "Extracted text is empty"}

    # Chunk text (~500 chars per chunk with overlap)
    chunk_size = 500
    overlap = 100
    chars = list(text)
    chunks_text = []
    i = 0
    while i < len(chars):
        chunk = "".join(chars[i: i + chunk_size])
        if chunk.strip():
            chunks_text.append(chunk)
        i += chunk_size - overlap

    # Embed and upsert
    from chroma_store import upsert_chunks
    collection = get_collection(f"wrike_{req.environment}")
    chunks = []
    for idx, chunk_text in enumerate(chunks_text):
        try:
            emb = embedder.embed(chunk_text)
        except Exception as e:
            continue
        chunks.append({
            "id": f"{req.ticket_id}_doc_{idx}",
            "embedding": emb,
            "document": chunk_text,
            "metadata": {
                "item_id": req.ticket_id,
                "source": req.s3_key,
                "doc_type": req.doc_type,
            },
        })

    upsert_chunks(collection, chunks)
    return {
        "status": "ok",
        "ticket_id": req.ticket_id,
        "chunks_indexed": len(chunks),
        "source": req.s3_key,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("RAG_SERVICE_PORT", "8090"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
