from __future__ import annotations

import httpx

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "qwen3-embedding:4b-q4_K_M"
_TIMEOUT = 30.0


def embed(text: str) -> list[float]:
    """Generate an embedding for text using Ollama. Raises on error."""
    r = httpx.post(
        OLLAMA_URL,
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def is_available() -> bool:
    try:
        embed("ping")
        return True
    except Exception:
        return False
