#!/usr/bin/env python3
"""
Manage trajectory-mcp users.

Usage:
  python3 manage_users.py list
  python3 manage_users.py add john.doe@company.com
  python3 manage_users.py remove john.doe@company.com
  python3 manage_users.py token john.doe@company.com   # show token (to share with user)
"""
import json
import secrets
import sys
from pathlib import Path

USERS_FILE = Path(__file__).parent / "users.json"
ALLOWED_DOMAINS = {"initus.io", "trajectoryinc.com"}


def validate_email(email: str) -> None:
    parts = email.strip().split("@")
    if len(parts) != 2 or not parts[0] or parts[1] not in ALLOWED_DOMAINS:
        domains = " or ".join(f"@{d}" for d in sorted(ALLOWED_DOMAINS))
        print(f"Error: email must be {domains}. Got: {email}")
        sys.exit(1)


def load() -> dict:
    if not USERS_FILE.exists():
        return {}
    return json.loads(USERS_FILE.read_text())


def save(data: dict) -> None:
    USERS_FILE.write_text(json.dumps(data, indent=2) + "\n")


def cmd_list(users: dict) -> None:
    if not users:
        print("No users registered.")
        return
    print(f"{'EMAIL':<40}  TOKEN")
    print("-" * 70)
    for email, token in sorted(users.items()):
        masked = token[:10] + "…"
        print(f"{email:<40}  {masked}")


def cmd_add(users: dict, email: str) -> None:
    validate_email(email)
    if email in users:
        print(f"User already exists: {email}")
        print(f"Token: {users[email]}")
        return
    token = "traj_" + secrets.token_urlsafe(16)
    users[email] = token
    save(users)
    print(f"User added: {email}")
    print()
    print(f"  Token: {token}")
    print()
    print("claude_desktop_config.json — use this URL in mcp-remote args:")
    print(f'  "http://64.137.145.121:8080/mcp?token={token}"')


def cmd_remove(users: dict, email: str) -> None:
    if email not in users:
        print(f"User not found: {email}")
        return
    del users[email]
    save(users)
    print(f"User removed: {email}")
    print("Their token is now invalid — they will be rejected on next connection.")


def cmd_token(users: dict, email: str) -> None:
    if email not in users:
        print(f"User not found: {email}")
        return
    token = users[email]
    print(f"Token for {email}:")
    print()
    print(f"  {token}")
    print()
    print("claude_desktop_config.json — use this URL in mcp-remote args:")
    print(f'  "http://64.137.145.121:8080/mcp?token={token}"')


def main() -> None:
    users = load()
    args = sys.argv[1:]

    if not args or args[0] == "list":
        cmd_list(users)
    elif args[0] == "add" and len(args) == 2:
        cmd_add(users, args[1])
    elif args[0] == "remove" and len(args) == 2:
        cmd_remove(users, args[1])
    elif args[0] == "token" and len(args) == 2:
        cmd_token(users, args[1])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
