import json

MAX_RESPONSE_BYTES = 900_000  # 900 KB — stays under the 1 MB MCP hard limit


def fit_to_limit(items: list, key: str, meta: dict) -> dict:
    """
    Return as many items as fit within MAX_RESPONSE_BYTES.

    When truncation occurs the response includes:
      - truncated: true
      - returned: how many items are in this response
      - total: how many items existed before truncation
      - remaining_ids: list of IDs (ticket_id / meeting_uuid) for the items that were dropped,
        so the caller can request them in a follow-up call.

    The id_field parameter is inferred automatically from the first item's keys.
    """
    if not items:
        return {**meta, key: items}

    # Detect which field to use as the ID for the remaining list
    first = items[0]
    id_field = next(
        (f for f in ("ticket_id", "meeting_uuid") if f in first),
        None,
    )

    full = {**meta, key: items}
    if _byte_size(full) <= MAX_RESPONSE_BYTES:
        return full

    # Binary search for the largest N that fits
    lo, hi = 1, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = _build(meta, key, items, mid, id_field)
        if _byte_size(candidate) <= MAX_RESPONSE_BYTES:
            lo = mid
        else:
            hi = mid - 1

    truncated = _build(meta, key, items, lo, id_field)
    if _byte_size(truncated) > MAX_RESPONSE_BYTES:
        # Even 1 item is too large — return empty with guidance
        return {
            **meta,
            key: [],
            "truncated": True,
            "returned": 0,
            "total": len(items),
            "warning": "Each item exceeds the size limit. Request items one at a time.",
            **({"remaining_ids": [i[id_field] for i in items]} if id_field else {}),
        }
    return truncated


def _build(meta: dict, key: str, items: list, n: int, id_field: str | None) -> dict:
    kept = items[:n]
    dropped = items[n:]
    result = {
        **meta,
        key: kept,
        "truncated": True,
        "returned": n,
        "total": len(items),
    }
    if id_field and dropped:
        result["remaining_ids"] = [i[id_field] for i in dropped]
    return result


def _byte_size(obj: dict) -> int:
    return len(json.dumps(obj, default=str).encode("utf-8"))
