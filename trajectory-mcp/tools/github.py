"""
GitHub read-only tools.

The server holds a single fine-grained PAT (config.GITHUB_TOKEN) and exposes
the repos that PAT can see to whoever queries the MCP. There is no per-user
identity reconciliation — every MCP caller sees the same view, which is the
explicit design choice given the deployment (one person, two emails, one
GitHub account with org access).

Four tools at launch:
  list_repos       — discover what's available before asking about a specific repo
  list_commits     — the main workhorse: commits with author / since / until filters
  get_commit       — detail of one commit, including changed files and patches
  list_pull_requests — feature-grouped work, better than raw commits for
                       "what did X implement" questions

If GITHUB_TOKEN is unset, all tools return {"error": "GITHUB_TOKEN not
configured"} so the server still boots and the rest of the stack works.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from config import GITHUB_API_BASE, GITHUB_TOKEN

log = logging.getLogger(__name__)

_TIMEOUT = 15.0
_RETRIES = 2  # 3 attempts total on transient errors
_MAX_PATCH_BYTES = 50_000  # per-file patch cap in get_commit


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, params: dict | None = None) -> tuple[Any | None, dict | None]:
    """
    GET a GitHub API path with retry on transient 5xx / transport errors.
    Returns (json_body, error_dict). Exactly one is non-None.

    4xx errors (401/403/404) are not retried — they reflect input or auth state,
    not infrastructure flakiness.
    """
    if not GITHUB_TOKEN:
        return None, {"error": "GITHUB_TOKEN not configured on the server"}

    url = f"{GITHUB_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    last_err: str | None = None

    for attempt in range(_RETRIES + 1):
        try:
            r = httpx.get(url, headers=_headers(), params=params or {}, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json(), None
            if r.status_code == 401:
                return None, {"error": "GitHub auth failed — token invalid or expired"}
            if r.status_code == 403:
                # Could be rate limit or pending-approval scope. Surface the body.
                remaining = r.headers.get("x-ratelimit-remaining")
                if remaining == "0":
                    reset = r.headers.get("x-ratelimit-reset", "?")
                    return None, {"error": f"GitHub rate limit exceeded (resets at {reset} epoch)"}
                msg = r.json().get("message", r.text[:200]) if r.content else "forbidden"
                return None, {"error": f"GitHub 403: {msg}"}
            if r.status_code == 404:
                return None, {"error": "repo or resource not found — check name and token access"}
            if 500 <= r.status_code < 600:
                last_err = f"GitHub {r.status_code}"
                # fall through to retry
            else:
                return None, {"error": f"GitHub {r.status_code}: {r.text[:200]}"}
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_err = str(e)

        if attempt < _RETRIES:
            wait = 2 ** attempt
            log.warning("github GET %s transient failure (attempt %d), retry in %ds: %s",
                        path, attempt + 1, wait, last_err)
            time.sleep(wait)

    return None, {"error": f"GitHub unreachable after {_RETRIES + 1} attempts: {last_err}"}


def list_repos(query: str | None = None, limit: int = 20) -> dict:
    """
    List repositories the configured GitHub PAT can see, most recently updated first.

    Use this as the entry point when you don't know the exact `owner/name` of a
    repo. Once you have a `full_name` from the response, pass it as `repo` to
    list_commits, get_commit, or list_pull_requests.

    Parameters
    ----------
    query : Optional substring filter applied client-side against `full_name`
            and `description`. Case-insensitive. Use it to narrow when the
            account has access to many repos.
    limit : Max repos to return (1–100). Default 20.

    Returns
    -------
    {
      "repos": [
        {"full_name": "owner/name", "private": bool, "description": str,
         "updated_at": ISO8601, "default_branch": str, "url": str},
        ...
      ],
      "total": int  # number returned, NOT total across the account
    }

    ## Inference / cross-reference
    Whenever a user asks about a developer's activity without naming a repo, run
    this first to see what's available, then pick repos by name relevance to the
    question (e.g. ask about the WSR work → repos with "wsr" or relevant scope).
    If the user names a repo directly, you can skip this and go straight to
    list_commits.
    """
    body, err = _get("/user/repos", params={"per_page": max(1, min(100, limit)), "sort": "updated"})
    if err:
        return err

    repos = body or []
    if query:
        q = query.lower()
        repos = [r for r in repos if q in (r.get("full_name") or "").lower()
                 or q in ((r.get("description") or "").lower())]

    out = [
        {
            "full_name": r.get("full_name"),
            "private": bool(r.get("private")),
            "description": r.get("description") or "",
            "updated_at": r.get("updated_at"),
            "default_branch": r.get("default_branch"),
            "url": r.get("html_url"),
        }
        for r in repos[:limit]
    ]
    return {"repos": out, "total": len(out)}


def list_commits(
    repo: str,
    author: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 30,
) -> dict:
    """
    List commits in a repository, optionally filtered by author and date range.

    This is the main workhorse for questions about who did what and when.

    Parameters
    ----------
    repo  : Repository in `owner/name` form (e.g. "initus/mcp-research"). Get
            it from list_repos if you don't know it.
    author: GitHub username OR email. Filters server-side. If the user gave
            you a full name like "Juan Rocha", first call list_commits without
            this filter and look at author_name in the first few results to
            discover the matching username or email.
    since : ISO 8601 date or timestamp (e.g. "2026-05-15" or
            "2026-05-15T00:00:00Z"). Only commits at or after this time.
    until : Same format as `since`. Only commits strictly before this time.
    limit : Max commits to return (1–100). Default 30.

    Returns
    -------
    {
      "repo": "owner/name",
      "total": int,
      "commits": [
        {"sha": "<8-char short>", "full_sha": "<40-char>",
         "author_name": str, "author_email": str, "date": ISO8601,
         "subject": str,  # first line of the commit message
         "url": str},
        ...
      ]
    }

    ## Inference / cross-reference
    To answer "what did X do this week / on day Y":
      1. list_commits(repo, author=<github-username-or-email>, since=<iso-date>)
      2. If the user gave a name not a username, list_commits without filter and
         pick the matching author_name/author_email from the first hits.

    To correlate code with discussions ("what was decided AND implemented"):
      - search_meetings(query=<topic>, company_id=<C>) for what was discussed
      - list_commits(repo, since=<meeting-date>) for what was implemented after
      - get_meeting_details for decisions, then check the commits match

    To find "implementations" (grouped feature work), prefer list_pull_requests
    over list_commits — PRs aggregate related commits into a single feature
    that's easier to summarize.

    Empty result is not "no record" — broaden the date range or check other
    repos via list_repos first.
    """
    params: dict[str, Any] = {"per_page": max(1, min(100, limit))}
    if author:
        params["author"] = author
    if since:
        params["since"] = since
    if until:
        params["until"] = until

    body, err = _get(f"/repos/{repo}/commits", params=params)
    if err:
        return err

    commits = [
        {
            "sha": (c.get("sha") or "")[:8],
            "full_sha": c.get("sha"),
            "author_name": ((c.get("commit") or {}).get("author") or {}).get("name"),
            "author_email": ((c.get("commit") or {}).get("author") or {}).get("email"),
            "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            "subject": (((c.get("commit") or {}).get("message") or "")).split("\n", 1)[0],
            "url": c.get("html_url"),
        }
        for c in (body or [])
    ]
    return {"repo": repo, "total": len(commits), "commits": commits}


def get_commit(repo: str, sha: str) -> dict:
    """
    Fetch the full detail of a single commit, including changed files and patches.

    Parameters
    ----------
    repo : Repository in `owner/name` form.
    sha  : Commit SHA — accepts both short (≥7 chars) and full 40-char form.

    Returns
    -------
    {
      "sha": "<full sha>", "url": str,
      "author":    {"name": str, "email": str, "date": ISO8601},
      "committer": {"name": str, "email": str, "date": ISO8601},
      "message": str,  # full commit message, including body
      "stats": {"total": int, "additions": int, "deletions": int},
      "files": [
        {"filename": str, "status": "added|modified|removed|renamed",
         "additions": int, "deletions": int, "patch": str | None,
         "patch_truncated": bool},
        ...
      ]
    }

    Patches larger than 50KB per file are truncated and `patch_truncated=True`
    is set so the consumer knows to fetch the file directly if it needs the
    full diff.

    ## Inference / cross-reference
    Use this when list_commits surfaced an interesting subject and you need to
    know WHAT changed, not just THAT it changed. For a feature summary, prefer
    list_pull_requests on the same repo — PRs aggregate multiple commits and
    usually have a richer description.
    """
    body, err = _get(f"/repos/{repo}/commits/{sha}")
    if err:
        return err

    commit_info = body.get("commit") or {}
    author = commit_info.get("author") or {}
    committer = commit_info.get("committer") or {}
    stats = body.get("stats") or {}

    files_out: list[dict] = []
    for f in body.get("files") or []:
        patch = f.get("patch")
        truncated = False
        if isinstance(patch, str) and len(patch) > _MAX_PATCH_BYTES:
            patch = patch[:_MAX_PATCH_BYTES]
            truncated = True
        files_out.append({
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "patch": patch,
            "patch_truncated": truncated,
        })

    return {
        "sha": body.get("sha"),
        "url": body.get("html_url"),
        "author": {"name": author.get("name"), "email": author.get("email"),
                   "date": author.get("date")},
        "committer": {"name": committer.get("name"), "email": committer.get("email"),
                      "date": committer.get("date")},
        "message": commit_info.get("message") or "",
        "stats": {
            "total": stats.get("total", 0),
            "additions": stats.get("additions", 0),
            "deletions": stats.get("deletions", 0),
        },
        "files": files_out,
    }


def list_pull_requests(
    repo: str,
    author: str | None = None,
    state: str = "all",
    since: str | None = None,
    limit: int = 30,
) -> dict:
    """
    List pull requests in a repository, optionally filtered by author, state,
    and date.

    Prefer this over list_commits when the question is about *implementations* /
    *features shipped*. A PR groups N commits into one logical change with a
    title and description, so it's a better unit than raw commits for narrating
    "what did X build" or "what shipped this week".

    Parameters
    ----------
    repo  : Repository in `owner/name` form.
    author: GitHub username. Filtered client-side (the PR list endpoint does not
            accept an author query). If you don't know it, list a few PRs first
            without filter and match.
    state : "open", "closed", or "all" (default "all").
    since : ISO 8601 date or timestamp. Filtered client-side against
            `created_at`.
    limit : Max PRs to return (1–100). Default 30.

    Returns
    -------
    {
      "repo": "owner/name",
      "total": int,
      "pull_requests": [
        {"number": int, "title": str, "author": str, "state": str,
         "draft": bool, "created_at": ISO8601, "updated_at": ISO8601,
         "merged_at": ISO8601 | None, "url": str},
        ...
      ]
    }

    ## Inference / cross-reference
    "What did X implement this sprint?" → list_pull_requests(repo, author=<X>,
    state="all", since=<sprint-start>). Then for each PR worth a closer look,
    take its number and fetch the commits via the GitHub UI URL (we don't
    expose a per-PR commit list yet — open a follow-up if needed).

    To turn a PR title into shipped value, the title itself usually suffices;
    if not, the underlying commit subjects from list_commits in the same
    timeframe fill the gap.
    """
    if state not in ("open", "closed", "all"):
        return {"error": f"invalid state '{state}' — use open, closed, or all"}

    params: dict[str, Any] = {
        "state": state,
        "per_page": max(1, min(100, limit)),
        "sort": "updated",
        "direction": "desc",
    }
    body, err = _get(f"/repos/{repo}/pulls", params=params)
    if err:
        return err

    items = body or []
    if author:
        a = author.lower()
        items = [p for p in items if ((p.get("user") or {}).get("login") or "").lower() == a]
    if since:
        items = [p for p in items if (p.get("created_at") or "") >= since]

    out = [
        {
            "number": p.get("number"),
            "title": p.get("title"),
            "author": (p.get("user") or {}).get("login"),
            "state": p.get("state"),
            "draft": bool(p.get("draft")),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
            "merged_at": p.get("merged_at"),
            "url": p.get("html_url"),
        }
        for p in items[:limit]
    ]
    return {"repo": repo, "total": len(out), "pull_requests": out}
