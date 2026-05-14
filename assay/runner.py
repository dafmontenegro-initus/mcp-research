#!/usr/bin/env python3
"""
assay — autonomous QA agent for trajectory-mcp.

Usage:
  python3 runner.py                  # full run (all tools)
  python3 runner.py --tool list_meetings   # test one specific tool
  python3 runner.py --dry-run        # connect, list tools, then stop (no LLM)
"""
import sys
from agent import run

def main() -> None:
    args = sys.argv[1:]
    tool_filter = None
    dry_run = False

    i = 0
    while i < len(args):
        if args[i] == "--tool" and i + 1 < len(args):
            tool_filter = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"Unknown argument: {args[i]}")
            print(__doc__)
            sys.exit(1)

    run(tool_filter=tool_filter, dry_run=dry_run)


if __name__ == "__main__":
    main()
