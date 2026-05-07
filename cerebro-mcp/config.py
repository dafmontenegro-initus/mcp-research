import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Company alias map — same as Cerebro's shared.py
_COMPANY_ID_ALIASES: dict[str, str] = {
    "TJV": "NWN",
    "BMT": "BMC",
}

# Wrike DB alias (company_id → table name segment)
_WRIKE_DB_ALIASES: dict[str, str] = {
    "DAI": "DAI",
}

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
DEV_S3_BUCKET = os.getenv("DEV_S3_BUCKET", "")

MEET_DB = {
    "host": os.getenv("MEET_DEV_DB_HOST"),
    "user": os.getenv("MEET_DEV_DB_USER"),
    "password": os.getenv("MEET_DEV_DB_PASSWORD"),
    "port": int(os.getenv("MEET_DEV_DB_PORT", "3306")),
}

WK_DB = {
    "host": os.getenv("WK_DEV_DB_HOST"),
    "user": os.getenv("WK_DEV_DB_USER"),
    "password": os.getenv("WK_DEV_DB_PASSWORD"),
    "port": int(os.getenv("WK_DEV_DB_PORT", "3306")),
}

_REQUIRED = {
    "MEET_DEV_DB_HOST": MEET_DB["host"],
    "MEET_DEV_DB_USER": MEET_DB["user"],
    "MEET_DEV_DB_PASSWORD": MEET_DB["password"],
    "WK_DEV_DB_HOST": WK_DB["host"],
    "WK_DEV_DB_USER": WK_DB["user"],
    "WK_DEV_DB_PASSWORD": WK_DB["password"],
}

_missing = [k for k, v in _REQUIRED.items() if not v]
if _missing:
    print(f"[cerebro-mcp] ERROR: missing required env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)


def resolve_company(company_id: str) -> str:
    return _COMPANY_ID_ALIASES.get(company_id.upper(), company_id.upper())


def wrike_table(company_id: str) -> str:
    resolved = resolve_company(company_id)
    db_name = _WRIKE_DB_ALIASES.get(resolved, resolved)
    return f"wrike.{db_name}_FULL"
