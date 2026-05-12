from __future__ import annotations

import io
import json
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pandas as pd
import pymysql

from chroma_store import get_collection, upsert_chunks

# S3 bucket names per environment
_WRIKE_BUCKETS = {
    "prod": "assets-tj-prod",
    "dev":  "assets-tj-dev",
    "qa":   "assets-tj-qa",
}
_MEETINGS_BUCKET = "tjzoom"

# How old (seconds) a cached entry can be before we re-check S3
STALENESS_SECONDS = 1800  # 30 min

# Manifest: tracks {s3_key -> s3_last_modified ISO} for items already in ChromaDB
MANIFEST_PATH = Path(__file__).parent / "chroma_data" / "manifest.json"


# ── Manifest helpers ────────────────────────────────────────────────────────

def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


def _s3_iso(obj_meta: dict) -> str:
    """Return LastModified as ISO string (UTC)."""
    lm = obj_meta.get("LastModified")
    if lm is None:
        return ""
    if hasattr(lm, "isoformat"):
        if lm.tzinfo is None:
            lm = lm.replace(tzinfo=timezone.utc)
        return lm.isoformat()
    return str(lm)


# ── Pickle parsing ───────────────────────────────────────────────────────────

def _parse_wrike_pickle(data: bytes, company_id: str = "") -> list[dict]:
    """
    Parse a Wrike pickle (pandas DataFrame) into a list of chunk dicts.
    Daemon schema: ticket, chunk_text, embedding, tokens
    """
    df: pd.DataFrame = pickle.loads(data)

    # Defensive column mapping — handle slight variations from daemon versions
    id_col   = next((c for c in ("ticket", "ticket_id") if c in df.columns), None)
    text_col = next((c for c in ("chunk_text", "text", "content") if c in df.columns), None)
    emb_col  = next((c for c in ("embedding",) if c in df.columns), None)

    if not all([id_col, text_col, emb_col]):
        return []  # unknown schema — skip silently

    chunks = []
    for idx, row in df.iterrows():
        emb = row[emb_col]
        if emb is None:
            continue
        if hasattr(emb, "tolist"):
            emb = emb.tolist()
        text = str(row[text_col]) if row[text_col] else ""
        if not text.strip():
            continue
        chunks.append({
            "id":        f"{row[id_col]}_{idx}",
            "embedding": emb,
            "document":  text,
            "metadata": {"item_id": str(row[id_col]), "company_id": company_id},
        })
    return chunks


def _parse_meetings_pickle(data: bytes, company_id: str = "") -> list[dict]:
    """
    Parse a Zoom/meetings pickle (pandas DataFrame) into chunk dicts.
    Daemon schema: meeting_id, text, embedding
    """
    df: pd.DataFrame = pickle.loads(data)

    id_col   = next((c for c in ("meeting_id", "uuid", "meeting_uuid") if c in df.columns), None)
    text_col = next((c for c in ("text", "chunk_text", "content") if c in df.columns), None)
    emb_col  = next((c for c in ("embedding",) if c in df.columns), None)

    if not all([id_col, text_col, emb_col]):
        return []

    chunks = []
    for idx, row in df.iterrows():
        emb = row[emb_col]
        if emb is None:
            continue
        if hasattr(emb, "tolist"):
            emb = emb.tolist()
        text = str(row[text_col]) if row[text_col] else ""
        if not text.strip():
            continue
        chunks.append({
            "id":        f"{row[id_col]}_{idx}",
            "embedding": emb,
            "document":  text,
            "metadata": {"item_id": str(row[id_col]), "company_id": company_id},
        })
    return chunks


# ── Core sync ────────────────────────────────────────────────────────────────

def _make_s3() -> boto3.client:
    return boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))


def _get_company_meeting_uuids(company_id: str) -> list[str]:
    """
    Query meetings_projects to get the meeting UUIDs that belong to this company.

    tjzoom bucket has NO company prefix — pickles are stored as {UUID}/{UUID}_embeddings.pkl.
    The only way to find which meetings belong to a company is via the DB.
    """
    try:
        conn = pymysql.connect(
            host=os.getenv("MEET_DB_HOST", ""),
            user=os.getenv("MEET_DB_USER", ""),
            password=os.getenv("MEET_DB_PASSWORD", ""),
            port=int(os.getenv("MEET_DB_PORT", "3306")),
            database="meetings_assets",
            connect_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT meeting_id FROM meetings_assets.meetings_projects WHERE project_id = %s",
                [company_id.upper()],
            )
            rows = cur.fetchall()
        conn.close()
        return [r["meeting_id"] for r in rows]
    except Exception as e:
        print(f"[pickle_sync] cannot get meeting UUIDs for {company_id}: {e}")
        return []


def _ingest_key(
    s3,
    bucket: str,
    key: str,
    collection,
    data_type: str,
    manifest: dict,
    company_id: str = "",
) -> None:
    """Download one pickle, parse it, upsert into ChromaDB, update manifest."""
    try:
        obj_meta = s3.head_object(Bucket=bucket, Key=key)
        s3_modified = _s3_iso(obj_meta)

        cached = manifest.get(f"{bucket}/{key}")
        if cached and cached.get("s3_last_modified") == s3_modified:
            return  # still fresh

        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as e:
        print(f"[pickle_sync] skip {bucket}/{key}: {e}")
        return

    if data_type == "wrike":
        chunks = _parse_wrike_pickle(raw, company_id)
    else:
        chunks = _parse_meetings_pickle(raw, company_id)

    if chunks:
        upsert_chunks(collection, chunks)

    manifest[f"{bucket}/{key}"] = {
        "s3_last_modified": s3_modified,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "chunks": len(chunks),
    }


def ensure_warm(company_id: str, data_type: str, environment: str = "prod") -> dict:
    """
    Lazy-load pickles for (company_id, data_type) from S3 into ChromaDB.

    - On first call for a company: loads all pickles from S3.
    - On subsequent calls: only re-loads pickles whose S3 LastModified changed.
    - Returns a summary dict with counts.
    """
    s3 = _make_s3()
    manifest = _load_manifest()
    collection_name = f"{data_type}_{environment}"
    collection = get_collection(collection_name)

    if data_type == "wrike":
        bucket = _WRIKE_BUCKETS.get(environment, "assets-tj-prod")
        # S3 structure: wrike/{COMPANY}/{ticket_id}/{ticket_id}.pkl
        prefix = f"wrike/{company_id.upper()}/"
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        try:
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(".pkl"):
                        keys.append(obj["Key"])
        except Exception as e:
            return {"error": f"Cannot list S3 {bucket}/{prefix}: {e}"}

    elif data_type == "meetings":
        # tjzoom has NO company prefix — structure is {UUID}/{UUID}_embeddings.pkl
        # We identify company meetings via the DB and download by UUID.
        bucket = _MEETINGS_BUCKET
        prefix = ""  # no prefix — kept for reporting only
        uuids = _get_company_meeting_uuids(company_id)
        if not uuids:
            return {"status": "empty", "bucket": bucket, "company_id": company_id, "keys_found": 0}
        keys = [f"{uuid}/{uuid}_embeddings.pkl" for uuid in uuids]

    else:
        return {"error": f"Unknown data_type: {data_type}"}

    if not keys:
        return {"status": "empty", "bucket": bucket, "prefix": prefix, "keys_found": 0}

    loaded = 0
    for key in keys:
        cached = manifest.get(f"{bucket}/{key}")
        if cached:
            try:
                obj_meta = s3.head_object(Bucket=bucket, Key=key)
                s3_modified = _s3_iso(obj_meta)
                if cached.get("s3_last_modified") == s3_modified:
                    continue  # still fresh, skip
            except Exception:
                pass  # if head fails, re-ingest to be safe

        _ingest_key(s3, bucket, key, collection, data_type, manifest, company_id)
        loaded += 1

    _save_manifest(manifest)
    return {
        "status": "ok",
        "bucket": bucket,
        "prefix": prefix or f"(DB-resolved UUIDs for {company_id})",
        "keys_found": len(keys),
        "keys_loaded": loaded,
    }
