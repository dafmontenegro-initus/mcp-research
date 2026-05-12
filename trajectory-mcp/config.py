import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
RAG_SERVICE_URL = os.getenv("RAG_SERVICE_URL", "http://localhost:8090")
S3_WRIKE_BUCKET = os.getenv("S3_WRIKE_BUCKET", "")
BAMBOOHR_TIMEOFF_URL = os.getenv("BAMBOOHR_TIMEOFF_URL", "")
BAMBOOHR_BIRTHDAYS_URL = os.getenv("BAMBOOHR_BIRTHDAYS_URL", "")
BAMBOOHR_ANNIVERSARIES_URL = os.getenv("BAMBOOHR_ANNIVERSARIES_URL", "")
BAMBOOHR_HOLIDAYS_URL = os.getenv("BAMBOOHR_HOLIDAYS_URL", "")

MEET_DB = {
    "host": os.getenv("MEET_DB_HOST"),
    "user": os.getenv("MEET_DB_USER"),
    "password": os.getenv("MEET_DB_PASSWORD"),
    "port": int(os.getenv("MEET_DB_PORT", "3306")),
}

WK_DB = {
    "host": os.getenv("WK_DB_HOST"),
    "user": os.getenv("WK_DB_USER"),
    "password": os.getenv("WK_DB_PASSWORD"),
    "port": int(os.getenv("WK_DB_PORT", "3306")),
}

_REQUIRED = {
    "MEET_DB_HOST": MEET_DB["host"],
    "MEET_DB_USER": MEET_DB["user"],
    "MEET_DB_PASSWORD": MEET_DB["password"],
    "WK_DB_HOST": WK_DB["host"],
    "WK_DB_USER": WK_DB["user"],
    "WK_DB_PASSWORD": WK_DB["password"],
}

_missing = [k for k, v in _REQUIRED.items() if not v]
if _missing:
    print(f"[trajectory-mcp] ERROR: missing required env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

# Set by server.py at startup when --company flag is provided.
SCOPED_COMPANY: str | None = None


def validate_company(company_id: str) -> str | None:
    """Return an error string if this server instance is scoped and company_id doesn't match."""
    if SCOPED_COMPANY and company_id.upper() != SCOPED_COMPANY:
        return (
            f"This server instance is scoped to company {SCOPED_COMPANY}. "
            f"Requested: {company_id.upper()}"
        )
    return None


def wrike_table(company_id: str) -> str:
    return f"wrike.{company_id.upper()}_FULL"
