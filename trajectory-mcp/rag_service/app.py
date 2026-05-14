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

def _background_warmup(force_check: bool = False):
    """
    Pre-warm ChromaDB for companies listed in WARMUP_COMPANIES (comma-separated).
    force_check=True bypasses the 24h cache and re-checks S3 for every key — used by
    the periodic re-warm so daemon updates are picked up without a full restart.
    """
    companies_raw = os.getenv("WARMUP_COMPANIES", "")
    if not companies_raw.strip():
        return
    companies = [c.strip().upper() for c in companies_raw.split(",") if c.strip()]
    environment = os.getenv("WARMUP_ENVIRONMENT", "prod")
    for company_id in companies:
        for data_type in ("wrike", "meetings"):
            print(f"[rag_service] warming {data_type} for {company_id}{'  (force)' if force_check else ''}...")
            try:
                result = ensure_warm(company_id, data_type, environment, force_check=force_check)
                loaded = result.get("keys_loaded", 0)
                total = result.get("keys_found", 0)
                fresh = total - loaded
                print(f"[rag_service] {data_type}/{company_id}: {loaded} updated, {fresh} already fresh ({total} total)")
            except Exception as e:
                print(f"[rag_service] warmup error {data_type}/{company_id}: {e}")


def _seconds_until_next_rewarm() -> float:
    """Seconds until the next :25 or :55 mark. Daemon finishes ~:20 and ~:50, so
    running at :25/:55 guarantees we always check S3 after the daemon is done."""
    from datetime import datetime, timedelta
    now = datetime.now()
    for minute in (25, 55):
        target = now.replace(minute=minute, second=0, microsecond=0)
        if target > now:
            return (target - now).total_seconds()
    target = (now + timedelta(hours=1)).replace(minute=25, second=0, microsecond=0)
    return (target - now).total_seconds()


async def _periodic_rewarm_task():
    """Async task: re-warm at :25 and :55 past every hour without blocking a thread.
    The actual S3/disk work runs in the thread pool via run_in_executor so search
    requests are never blocked while the re-warm is in progress."""
    import asyncio
    while True:
        wait = _seconds_until_next_rewarm()
        print(f"[rag_service] next re-warm in {int(wait // 60)}m {int(wait % 60)}s")
        await asyncio.sleep(wait)
        print(f"[rag_service] periodic re-warm starting...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _background_warmup(force_check=True))
        print(f"[rag_service] periodic re-warm complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[rag_service] v{RAG_SERVICE_VERSION} starting")
    print(f"[rag_service] Ollama available: {embedder.is_available()}")
    print(f"[rag_service] Reranker loading...")
    reranker.is_available()  # warm up on startup
    # Blocking warmup — server only becomes ready after index cache is hot.
    # On cold start this may take several minutes; on warm restart it's near-instant
    # because the 24h TTL keeps all manifest entries fresh.
    _background_warmup()
    print(f"[rag_service] Ready")
    # Periodic re-warm at :25 and :55 — picks up daemon updates without a restart.
    import asyncio
    asyncio.create_task(_periodic_rewarm_task())
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

    # Deduplicate by item_id — keep only the highest-ranked chunk per ticket.
    # A ticket may have many chunks; without dedup the same ticket appears multiple times.
    seen_ids: set = set()
    deduped = []
    for hit in ranked:
        item_id = hit.get("metadata", {}).get("item_id", "")
        if item_id not in seen_ids:
            seen_ids.add(item_id)
            deduped.append(hit)
    top = deduped[: req.top_k]

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

    # Deduplicate by item_id — keep only the highest-ranked chunk per meeting.
    seen_ids: set = set()
    deduped = []
    for hit in ranked:
        item_id = hit.get("metadata", {}).get("item_id", "")
        if item_id not in seen_ids:
            seen_ids.add(item_id)
            deduped.append(hit)
    top = deduped[: req.top_k]

    results = []
    for hit in top:
        meta = hit.get("metadata", {})
        results.append({
            "rank": hit["rank"],
            "meeting_uuid": meta.get("item_id", ""),
            "excerpt": hit.get("document", "")[:500],
        })

    return {"results": results, "total_candidates": len(candidates)}


# ── /stats/{company_id} ──────────────────────────────────────────────────────

@app.get("/stats/{company_id}")
def index_stats(company_id: str, environment: str = "prod"):
    """Return RAG index stats for a company: file counts, chunk counts, last sync."""
    from datetime import datetime, timezone
    from pickle_sync import (
        _load_manifest, _get_company_meeting_uuids,
        STALENESS_SECONDS, _WRIKE_BUCKETS, _MEETINGS_BUCKET,
    )

    manifest = _load_manifest()
    now = datetime.now(timezone.utc)
    result: dict = {"company_id": company_id.upper()}

    # ── Wrike stats ───────────────────────────────────────────────────────────
    wrike_bucket = _WRIKE_BUCKETS.get(environment, "assets-tj-prod")
    wrike_prefix = f"{wrike_bucket}/wrike/{company_id.upper()}/"
    wrike_entries = [
        v for k, v in manifest.items()
        if k.startswith(wrike_prefix) and not v.get("not_found")
    ]
    wrike_last = max(
        (datetime.fromisoformat(e["ingested_at"]) for e in wrike_entries if e.get("ingested_at")),
        default=None,
    )
    result["wrike"] = {
        "indexed_files": len(wrike_entries),
        "total_chunks": sum(e.get("chunks", 0) for e in wrike_entries),
        "last_synced": wrike_last.isoformat() if wrike_last else None,
        "stale": (now - wrike_last).total_seconds() > STALENESS_SECONDS if wrike_last else True,
    }

    # ── Meetings stats ────────────────────────────────────────────────────────
    uuids = _get_company_meeting_uuids(company_id)
    meet_entries = []
    for uuid in uuids:
        key = f"{_MEETINGS_BUCKET}/{uuid}/{uuid}_embeddings.pkl"
        v = manifest.get(key)
        if v and not v.get("not_found"):
            meet_entries.append(v)
    meet_last = max(
        (datetime.fromisoformat(e["ingested_at"]) for e in meet_entries if e.get("ingested_at")),
        default=None,
    )
    result["meetings"] = {
        "total_meetings_in_db": len(uuids),
        "indexed_files": len(meet_entries),
        "total_chunks": sum(e.get("chunks", 0) for e in meet_entries),
        "last_synced": meet_last.isoformat() if meet_last else None,
        "stale": (now - meet_last).total_seconds() > STALENESS_SECONDS if meet_last else True,
    }

    return result


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
    # Single process — multiple processes would each warm up independently (expensive)
    # and compete for the same Ollama GPU slot anyway (no throughput gain).
    # Concurrency comes from uvicorn's async event loop + anyio thread pool (40 threads
    # by default), which handles 8+ concurrent Assay workers without queuing.
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
