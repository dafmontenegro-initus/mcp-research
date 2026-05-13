#!/usr/bin/env python3
"""
Manage trajectory-mcp users.

Usage:
  python3 manage_users.py list
  python3 manage_users.py add john.doe@company.com [--role human|agent|tester] [--description "..."]
  python3 manage_users.py remove john.doe@company.com
  python3 manage_users.py token john.doe@company.com   # show token (to share with user)
"""
import json
import secrets
import sys
from pathlib import Path

USERS_FILE = Path(__file__).parent / "users.json"
ALLOWED_DOMAINS = {"initus.io", "trajectoryinc.com"}
VALID_ROLES = {"human", "agent", "tester"}


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


def _token(entry) -> str:
    return entry if isinstance(entry, str) else entry["token"]


def _role(entry) -> str:
    if isinstance(entry, str):
        return "human"
    return entry.get("role", "human")


def _description(entry) -> str:
    if isinstance(entry, str):
        return ""
    return entry.get("description", "")


def cmd_list(users: dict) -> None:
    if not users:
        print("No users registered.")
        return
    print(f"{'EMAIL':<40}  {'ROLE':<8}  {'TOKEN':<14}  DESCRIPTION")
    print("-" * 90)
    for email, entry in sorted(users.items()):
        masked = _token(entry)[:10] + "…"
        role = _role(entry)
        desc = _description(entry)
        print(f"{email:<40}  {role:<8}  {masked:<14}  {desc}")


def cmd_add(users: dict, email: str, role: str, description: str) -> None:
    validate_email(email)
    if role not in VALID_ROLES:
        print(f"Error: role must be one of {sorted(VALID_ROLES)}. Got: {role}")
        sys.exit(1)

    if email in users:
        existing = users[email]
        print(f"User already exists: {email}")
        print(f"Token: {_token(existing)}")
        return

    token = "traj_" + secrets.token_urlsafe(16)
    users[email] = {"token": token, "role": role, "description": description}
    save(users)
    print(f"User added: {email}  [{role}]")
    if description:
        print(f"  {description}")
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
    token = _token(users[email])
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
    elif args[0] == "add" and len(args) >= 2:
        email = args[1]
        role = "human"
        description = ""
        i = 2
        while i < len(args):
            if args[i] == "--role" and i + 1 < len(args):
                role = args[i + 1]
                i += 2
            elif args[i] == "--description" and i + 1 < len(args):
                description = args[i + 1]
                i += 2
            else:
                i += 1
        cmd_add(users, email, role, description)
    elif args[0] == "remove" and len(args) == 2:
        cmd_remove(users, args[1])
    elif args[0] == "token" and len(args) == 2:
        cmd_token(users, args[1])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
