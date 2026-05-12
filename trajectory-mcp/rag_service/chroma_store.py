from __future__ import annotations

import chromadb
from pathlib import Path

CHROMA_PATH = Path(__file__).parent / "chroma_data"
_client: chromadb.PersistentClient | None = None


def _get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    return _client


def get_collection(name: str) -> chromadb.Collection:
    """Get or create a collection with cosine similarity."""
    return _get_client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def upsert_chunks(collection: chromadb.Collection, chunks: list[dict]) -> None:
    """
    Upsert a batch of chunks into ChromaDB.

    Each chunk dict: {id, embedding, document, metadata}
    Uses upsert so re-ingesting a stale pickle replaces old chunks for the same item.
    """
    if not chunks:
        return
    collection.upsert(
        ids=[c["id"] for c in chunks],
        embeddings=[c["embedding"] for c in chunks],
        documents=[c["document"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )


def search(
    collection: chromadb.Collection,
    query_embedding: list[float],
    where: dict,
    n_results: int = 50,
) -> list[dict]:
    """
    Run a semantic search and return raw hits (before reranking).

    Returns a list of dicts with keys: id, document, metadata, distance.
    distance is cosine distance (lower = more similar when using cosine space).
    """
    try:
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception:
        return []

    hits = []
    for i, doc_id in enumerate(results["ids"][0]):
        hits.append({
            "id": doc_id,
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i],
        })
    return hits
