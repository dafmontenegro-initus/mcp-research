from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from threading import Lock

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "qwen3-embedding:4b-q4_K_M"
_TIMEOUT = 30.0
_RETRIES = 2  # 3 attempts total (initial + 2 retries) with exponential backoff

# Embedding model is deterministic for the same input; cache to skip Ollama on repeat queries.
# Set EMBED_CACHE_SIZE=0 to disable.
_CACHE_SIZE = int(os.getenv("EMBED_CACHE_SIZE", "256"))
_cache: "OrderedDict[str, list[float]]" = OrderedDict()
_cache_lock = Lock()
_cache_hits = 0
_cache_misses = 0


def _cache_get(key: str) -> list[float] | None:
    global _cache_hits, _cache_misses
    if _CACHE_SIZE <= 0:
        return None
    with _cache_lock:
        v = _cache.get(key)
        if v is None:
            _cache_misses += 1
            return None
        _cache.move_to_end(key)
        _cache_hits += 1
        return v


def _cache_put(key: str, value: list[float]) -> None:
    if _CACHE_SIZE <= 0:
        return
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def cache_stats() -> dict:
    with _cache_lock:
        return {"size": len(_cache), "capacity": _CACHE_SIZE,
                "hits": _cache_hits, "misses": _cache_misses}


def embed(text: str) -> list[float]:
    """Generate an embedding for text using Ollama. Retries transient failures."""
    cached = _cache_get(text)
    if cached is not None:
        return cached
    for attempt in range(_RETRIES + 1):
        try:
            r = httpx.post(
                OLLAMA_URL,
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            emb = r.json()["embedding"]
            _cache_put(text, emb)
            return emb
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
