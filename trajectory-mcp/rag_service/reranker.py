from __future__ import annotations

_model = None
MODEL_NAME = "BAAI/bge-reranker-v2-m3"


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder
        _model = CrossEncoder(MODEL_NAME)
    return _model


def rerank(query: str, hits: list[dict], text_key: str = "document") -> list[dict]:
    """
    Rerank ChromaDB hits using a cross-encoder.

    Returns the same dicts sorted by relevance, with a `rank` field added
    (1 = most relevant). The raw cross-encoder score is NOT exposed — callers
    receive only the ordered list so LLMs don't over-interpret raw floats.
    """
    if not hits:
        return []
    model = _get_model()
    pairs = [(query, h[text_key]) for h in hits]
    scores = model.predict(pairs)
    ranked = sorted(zip(scores, hits), key=lambda x: float(x[0]), reverse=True)
    result = []
    for rank, (_, hit) in enumerate(ranked, start=1):
        hit = dict(hit)
        hit["rank"] = rank
        hit.pop("distance", None)
        result.append(hit)
    return result


def is_available() -> bool:
    try:
        _get_model()
        return True
    except Exception:
        return False
