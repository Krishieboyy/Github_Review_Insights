"""GitHub ingest layer for Review-Insights.

Pulls a repo's PR review activity from the THREE distinct REST endpoints that
each mean something different (see CLAUDE.md section 2), filters out bots at
ingest, and caches the normalized result per repo as JSON.

This module is pure ingest: no rules, no LLM. The rest of the system reads the
cache, never the network.

    reviews        -> GET /repos/{o}/{r}/pulls/{n}/reviews    (verdicts)
    review_comments-> GET /repos/{o}/{r}/pulls/{n}/comments   (inline, diff-anchored)
    issue_comments -> GET /repos/{o}/{r}/issues/{n}/comments  (general conversation)

These are kept SEPARATE end-to-end. One does not cover the others.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from dotenv import load_dotenv

API_ROOT = "https://api.github.com"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
DEFAULT_MAX_PRS = 150
PER_PAGE = 100


# --------------------------------------------------------------------------- #
# Bot filtering
# --------------------------------------------------------------------------- #
def is_bot(user: dict[str, Any] | None) -> bool:
    """True for automation accounts.

    GitHub marks them two ways and we honor both: user.type == "Bot", and the
    login convention of a trailing "[bot]" (dependabot[bot], etc.). Either is
    enough. A missing/null user (ghost / deleted account) is treated as a bot
    so it never pollutes reviewer stats.
    """
    if not user:
        return True
    if user.get("type") == "Bot":
        return True
    login = (user.get("login") or "").lower()
    return login.endswith("[bot]")


# --------------------------------------------------------------------------- #
# HTTP client + pagination
# --------------------------------------------------------------------------- #
class GitHubClient:
    def __init__(self, token: str | None = None, timeout: float = 30.0):
        self.token = token or os.getenv("GITHUB_TOKEN") or ""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "review-insights",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.Client(base_url=API_ROOT, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level ---------------------------------------------------------- #
    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Single GET with primary + secondary rate-limit handling."""
        for attempt in range(4):
            resp = self._http.get(path, params=params)
            if resp.status_code == 403 and _is_rate_limited(resp):
                wait = _rate_limit_wait(resp)
                print(f"  rate-limited, sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def _paginate(
        self, path: str, params: dict[str, Any] | None = None, max_items: int | None = None
    ) -> list[dict[str, Any]]:
        """Walk ?per_page=100&page=N until a short page (or max_items)."""
        params = dict(params or {})
        params["per_page"] = PER_PAGE
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            params["page"] = page
            batch = self._get(path, params).json()
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if max_items is not None and len(out) >= max_items:
                return out[:max_items]
            if len(batch) < PER_PAGE:
                break
            page += 1
        return out

    # -- the four reads ----------------------------------------------------- #
    def list_pull_requests(self, owner: str, repo: str, max_prs: int) -> list[dict[str, Any]]:
        # state=all is essential — most review history is in closed/merged PRs.
        return self._paginate(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "all", "sort": "created", "direction": "desc"},
            max_items=max_prs,
        )

    def reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/reviews")

    def review_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/comments")

    def issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{owner}/{repo}/issues/{number}/comments")


def _is_rate_limited(resp: httpx.Response) -> bool:
    return resp.headers.get("X-RateLimit-Remaining") == "0" or "retry-after" in resp.headers


def _rate_limit_wait(resp: httpx.Response) -> int:
    if "retry-after" in resp.headers:
        return int(resp.headers["retry-after"]) + 1
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        return max(1, int(reset) - int(time.time()) + 1)
    return 60


# --------------------------------------------------------------------------- #
# Normalization — keep only what the rule layer needs, keep endpoints separate
# --------------------------------------------------------------------------- #
def _login(obj: dict[str, Any]) -> str | None:
    user = obj.get("user")
    return user.get("login") if user else None


def _norm_review(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r.get("id"),
        "user": _login(r),
        "state": r.get("state"),  # APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED
        "body": r.get("body") or "",
        "submitted_at": r.get("submitted_at"),
        "commit_id": r.get("commit_id"),
    }


def _norm_review_comment(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c.get("id"),
        "user": _login(c),
        "body": c.get("body") or "",
        "path": c.get("path"),
        "line": c.get("line"),
        "diff_hunk": c.get("diff_hunk"),
        "in_reply_to_id": c.get("in_reply_to_id"),
        "created_at": c.get("created_at"),
    }


def _norm_issue_comment(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c.get("id"),
        "user": _login(c),
        "body": c.get("body") or "",
        "created_at": c.get("created_at"),
    }


def _drop_bots(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if not is_bot(i.get("user"))]


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def cache_path(owner: str, repo: str) -> Path:
    return CACHE_DIR / f"{owner}__{repo}.json"


def load_cache(owner: str, repo: str) -> dict[str, Any] | None:
    path = cache_path(owner, repo)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _write_cache(data: dict[str, Any]) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    owner, repo = data["repo"].split("/")
    path = cache_path(owner, repo)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def ingest(
    repo: str,
    max_prs: int = DEFAULT_MAX_PRS,
    *,
    use_cache: bool = True,
    client: GitHubClient | None = None,
) -> dict[str, Any]:
    """Fetch (or load) a repo's review activity and return the normalized blob.

    Per PR we fetch all three endpoints separately and bot-filter each. The
    PR-level fields (number, created_at) are kept because the latency signal
    needs created_at.
    """
    owner, name = repo.split("/", 1)

    if use_cache:
        cached = load_cache(owner, name)
        if cached is not None:
            print(f"cache hit: {cache_path(owner, name)}", file=sys.stderr)
            return cached

    owns_client = client is None
    client = client or GitHubClient()
    try:
        prs = client.list_pull_requests(owner, name, max_prs)
        print(f"{repo}: {len(prs)} PRs (cap {max_prs})", file=sys.stderr)

        records: list[dict[str, Any]] = []
        for i, pr in enumerate(prs, 1):
            n = pr["number"]
            reviews = _drop_bots(client.reviews(owner, name, n))
            rcomments = _drop_bots(client.review_comments(owner, name, n))
            icomments = _drop_bots(client.issue_comments(owner, name, n))
            records.append(
                {
                    "number": n,
                    "title": pr.get("title"),
                    "author": _login(pr),
                    "state": pr.get("state"),
                    "created_at": pr.get("created_at"),
                    "merged_at": pr.get("merged_at"),
                    "reviews": [_norm_review(r) for r in reviews],
                    "review_comments": [_norm_review_comment(c) for c in rcomments],
                    "issue_comments": [_norm_issue_comment(c) for c in icomments],
                }
            )
            if i % 10 == 0 or i == len(prs):
                print(f"  ...{i}/{len(prs)} PRs", file=sys.stderr)
    finally:
        if owns_client:
            client.close()

    data = {
        "repo": repo,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "max_prs": max_prs,
        "pr_count": len(records),
        "pull_requests": records,
    }
    path = _write_cache(data)
    print(f"cached -> {path}", file=sys.stderr)
    return data


# --------------------------------------------------------------------------- #
# CLI:  python -m app.github_client owner/name [max_prs]
# --------------------------------------------------------------------------- #
def _summary(data: dict[str, Any]) -> dict[str, Any]:
    prs = data["pull_requests"]
    reviewers: set[str] = set()
    n_reviews = n_rc = n_ic = 0
    for pr in prs:
        for r in pr["reviews"]:
            if r["user"]:
                reviewers.add(r["user"])
            n_reviews += 1
        for c in pr["review_comments"]:
            if c["user"]:
                reviewers.add(c["user"])
            n_rc += 1
        for c in pr["issue_comments"]:
            if c["user"]:
                reviewers.add(c["user"])
            n_ic += 1
    return {
        "prs": len(prs),
        "distinct_reviewers": len(reviewers),
        "reviews": n_reviews,
        "review_comments": n_rc,
        "issue_comments": n_ic,
        "reviewers": sorted(reviewers),
    }


def main(argv: list[str]) -> int:
    load_dotenv()
    if not argv:
        print("usage: python -m app.github_client owner/name [max_prs]", file=sys.stderr)
        return 2
    repo = argv[0]
    max_prs = int(argv[1]) if len(argv) > 1 else DEFAULT_MAX_PRS
    if not os.getenv("GITHUB_TOKEN"):
        print("warning: no GITHUB_TOKEN — unauthenticated cap is 60 req/hr", file=sys.stderr)
    data = ingest(repo, max_prs)
    print(json.dumps(_summary(data), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
