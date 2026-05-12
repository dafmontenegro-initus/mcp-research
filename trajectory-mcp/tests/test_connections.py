#!/usr/bin/env python3
"""
Connectivity test for trajectory-mcp.

Run from the trajectory-mcp/ directory with the MCP venv active:
    python3 tests/test_connections.py

Does NOT require the RAG service to be running — it reports it as SKIP if unreachable.
"""
from __future__ import annotations

import sys
from pathlib import Path

# .env lives one level up (trajectory-mcp/.env)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import os

PASS = "  ✓"
FAIL = "  ✗"
SKIP = "  -"


def _section(title: str) -> None:
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}")


# ── 1. Meetings DB ───────────────────────────────────────────────────────────

def test_meetings_db() -> None:
    _section("Meetings DB")
    import pymysql
    host = os.getenv("MEET_DB_HOST", "")
    user = os.getenv("MEET_DB_USER", "")
    pw   = os.getenv("MEET_DB_PASSWORD", "")
    port = int(os.getenv("MEET_DB_PORT", "3306"))

    if not host:
        print(f"{FAIL} MEET_DB_HOST not set in .env")
        return

    try:
        conn = pymysql.connect(
            host=host, user=user, password=pw, port=port,
            database="meetings_assets", connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM meetings_assets.meetings LIMIT 1")
            row = cur.fetchone()
        conn.close()
        print(f"{PASS} Connected to meetings DB @ {host}")
        print(f"       meetings table row count: {row['cnt']}")
    except Exception as e:
        print(f"{FAIL} {e}")

    # Verify new tables used by new tools
    try:
        conn = pymysql.connect(
            host=host, user=user, password=pw, port=port,
            database="meetings_assets", connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM meetings_assets.meetings_participants LIMIT 1"
            )
            row = cur.fetchone()
        conn.close()
        print(f"{PASS} meetings_participants table: {row['cnt']} rows")
    except Exception as e:
        print(f"{FAIL} meetings_participants: {e}")

    try:
        conn = pymysql.connect(
            host=host, user=user, password=pw, port=port,
            database="meetings_assets", connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM meetings_assets.meetings "
                "WHERE chat IS NOT NULL AND chat != '' LIMIT 1"
            )
            row = cur.fetchone()
        conn.close()
        print(f"{PASS} meetings.chat column: {row['cnt']} rows with chat data")
    except Exception as e:
        print(f"{FAIL} meetings.chat column: {e}")


# ── 2. Wrike DB ──────────────────────────────────────────────────────────────

def test_wrike_db() -> None:
    _section("Wrike DB")
    import pymysql
    host = os.getenv("WK_DB_HOST", "")
    user = os.getenv("WK_DB_USER", "")
    pw   = os.getenv("WK_DB_PASSWORD", "")
    port = int(os.getenv("WK_DB_PORT", "3306"))

    if not host:
        print(f"{FAIL} WK_DB_HOST not set in .env")
        return

    try:
        conn = pymysql.connect(
            host=host, user=user, password=pw, port=port,
            connect_timeout=5, cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            dbs = [r[list(r.keys())[0]] for r in cur.fetchall()]
        conn.close()
        wrike_dbs = [d for d in dbs if d.lower() == "wrike"]
        if wrike_dbs:
            print(f"{PASS} Connected to Wrike DB @ {host}")
            print(f"       wrike database present")
        else:
            print(f"{FAIL} Connected but 'wrike' database not found. Databases: {dbs[:10]}")
    except Exception as e:
        print(f"{FAIL} {e}")

    # Check for at least one FULL table
    try:
        conn = pymysql.connect(
            host=host, user=user, password=pw, port=port,
            database="wrike", connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE '%_FULL'")
            tables = [list(r.values())[0] for r in cur.fetchall()]
        conn.close()
        if tables:
            print(f"{PASS} FULL tables found: {tables[:5]}")
        else:
            print(f"{FAIL} No *_FULL tables found in wrike database")
    except Exception as e:
        print(f"{FAIL} listing FULL tables: {e}")


# ── 3. S3 — transcripts bucket ───────────────────────────────────────────────

def test_s3_transcripts() -> None:
    _section("S3 — Transcripts bucket (tjzoompr)")
    import boto3
    bucket = os.getenv("S3_BUCKET", "tjzoompr")
    region = os.getenv("AWS_REGION", "us-east-1")

    try:
        s3 = boto3.client("s3", region_name=region)
        resp = s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
        count = resp.get("KeyCount", 0)
        print(f"{PASS} s3://{bucket} accessible ({count} objects sampled)")
    except Exception as e:
        print(f"{FAIL} s3://{bucket}: {e}")


# ── 4. S3 — Wrike pickles bucket ─────────────────────────────────────────────

def test_s3_wrike() -> None:
    _section("S3 — Wrike pickles bucket (assets-tj-prod)")
    import boto3
    bucket = os.getenv("S3_WRIKE_BUCKET", "assets-tj-prod")
    region = os.getenv("AWS_REGION", "us-east-1")

    try:
        s3 = boto3.client("s3", region_name=region)
        resp = s3.list_objects_v2(Bucket=bucket, Prefix="wrike/", MaxKeys=5)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        print(f"{PASS} s3://{bucket}/wrike/ accessible")
        if keys:
            print(f"       first keys: {keys}")
        else:
            print(f"       (no objects found under wrike/ prefix — may be empty for this env)")
    except Exception as e:
        print(f"{FAIL} s3://{bucket}: {e}")


# ── 5. S3 — Meetings pickles bucket (tjzoom) ─────────────────────────────────

def test_s3_meetings_pickles() -> None:
    _section("S3 — Meetings pickles bucket (tjzoom)")
    import boto3
    region = os.getenv("AWS_REGION", "us-east-1")

    try:
        s3 = boto3.client("s3", region_name=region)
        resp = s3.list_objects_v2(Bucket="tjzoom", MaxKeys=3)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        print(f"{PASS} s3://tjzoom accessible")
        if keys:
            print(f"       first keys: {keys}")
    except Exception as e:
        print(f"{FAIL} s3://tjzoom: {e}")


# ── 6. BambooHR ──────────────────────────────────────────────────────────────

def test_bamboohr() -> None:
    _section("BambooHR feeds (iCal/ics)")
    import httpx

    # Note: BambooHR's CDN (Cloudflare) blocks Python's urllib User-Agent.
    # The actual bamboohr.py tool already sets a proper User-Agent — this test mirrors that.
    headers = {"User-Agent": "Mozilla/5.0 (compatible; trajectory-mcp/1.0)"}

    feeds = {
        "time_off":     os.getenv("BAMBOOHR_TIMEOFF_URL", ""),
        "birthdays":    os.getenv("BAMBOOHR_BIRTHDAYS_URL", ""),
        "anniversaries":os.getenv("BAMBOOHR_ANNIVERSARIES_URL", ""),
        "holidays":     os.getenv("BAMBOOHR_HOLIDAYS_URL", ""),
    }

    for name, url in feeds.items():
        if not url:
            print(f"{SKIP} {name}: URL not set in .env")
            continue
        try:
            r = httpx.get(url, headers=headers, timeout=10.0)
            if r.status_code == 200 and "VCALENDAR" in r.text[:100]:
                events = r.text.count("BEGIN:VEVENT")
                print(f"{PASS} {name}: HTTP 200 — {events} events in feed")
            else:
                print(f"{FAIL} {name}: HTTP {r.status_code} — {r.text[:100]}")
        except Exception as e:
            print(f"{FAIL} {name}: {e}")


# ── 7. Ollama (embedding model) ──────────────────────────────────────────────

def test_ollama() -> None:
    _section("Ollama (qwen3-embedding:4b-q4_K_M)")
    import httpx

    try:
        r = httpx.post(
            "http://localhost:11434/api/embeddings",
            json={"model": "qwen3-embedding:4b-q4_K_M", "prompt": "test"},
            timeout=15.0,
        )
        if r.status_code == 200:
            emb = r.json().get("embedding", [])
            print(f"{PASS} Ollama responding — embedding dim: {len(emb)}")
        else:
            print(f"{FAIL} Ollama returned HTTP {r.status_code}: {r.text[:200]}")
    except httpx.ConnectError:
        print(f"{SKIP} Ollama not reachable at localhost:11434 (is it running?)")
    except Exception as e:
        print(f"{FAIL} {e}")


# ── 8. RAG service ───────────────────────────────────────────────────────────

def test_rag_service() -> None:
    _section("RAG service (localhost:8090)")
    import httpx

    url = os.getenv("RAG_SERVICE_URL", "http://localhost:8090")

    try:
        r = httpx.get(f"{url}/health", timeout=5.0)
        data = r.json()
        print(f"{PASS} RAG service up — version {data.get('version', '?')}")
        print(f"       ollama: {data.get('ollama')}   reranker: {data.get('reranker')}")
    except httpx.ConnectError:
        print(f"{SKIP} RAG service not running at {url}")
        print(f"       Start it with: cd rag_service && source .venv/bin/activate && python3 app.py")
    except Exception as e:
        print(f"{FAIL} {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n══════════════════════════════════════════════════")
    print("  trajectory-mcp — connectivity test")
    print("══════════════════════════════════════════════════")

    test_meetings_db()
    test_wrike_db()
    test_s3_transcripts()
    test_s3_wrike()
    test_s3_meetings_pickles()
    test_bamboohr()
    test_ollama()
    test_rag_service()

    print("\n══════════════════════════════════════════════════")
    print("  Done. Fix any ✗ before running the MCP server.")
    print("  ✓ = OK   ✗ = Error   - = Not running / not set")
    print("══════════════════════════════════════════════════\n")
