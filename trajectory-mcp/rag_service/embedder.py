from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "qwen3-embedding:4b-q4_K_M"
_TIMEOUT = 30.0
_RETRIES = 2  # 3 attempts total (initial + 2 retries) with exponential backoff


def embed(text: str) -> list[float]:
    """Generate an embedding for text using Ollama. Retries transient failures."""
    for attempt in range(_RETRIES + 1):
        try:
            r = httpx.post(
                OLLAMA_URL,
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()["embedding"]
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if attempt < _RETRIES:
                wait = 2 ** attempt
                log.warning("ollama embed transient failure (attempt %d), retry in %ds: %s",
                            attempt + 1, wait, e)
                time.sleep(wait)
            else:
                raise


def is_available() -> bool:
    """Liveness probe — single attempt, no retries. A healthcheck must be fast
    and honest about the current state; hiding transient failures behind retries
    defeats the purpose of probing."""
    try:
        r = httpx.post(
            OLLAMA_URL,
            json={"model": EMBED_MODEL, "prompt": "ping"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return "embedding" in r.json()
    except Exception:
        return False
