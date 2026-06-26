"""Review-Insights API.

Rule-layer endpoints only (for now): /reviewers and /reviewers/{login}/stats.
These are fully deterministic and need NO LLM key — the system stands on its
own as a working product without a model involved.

Data comes from the per-repo cache written by github_client. Either pass
?repo=owner/name, or omit it when exactly one repo is cached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from . import pipeline, rules
from .github_client import CACHE_DIR, load_cache

app = FastAPI(title="Review-Insights", version="0.1")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/app", include_in_schema=False)
def frontend() -> FileResponse:
    """Serve the single-file static frontend (consumes the JSON API below)."""
    return FileResponse(STATIC_DIR / "index.html")


def _resolve_cache(repo: str | None) -> dict[str, Any]:
    """Load a cached repo blob, or 404 with a helpful message."""
    if repo:
        if "/" not in repo:
            raise HTTPException(400, "repo must be 'owner/name'")
        owner, name = repo.split("/", 1)
        data = load_cache(owner, name)
        if data is None:
            raise HTTPException(
                404, f"no cache for {repo}; run: python -m app.github_client {repo} 150"
            )
        return data

    caches = sorted(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
    if not caches:
        raise HTTPException(404, "no cached repos; run github_client first")
    if len(caches) > 1:
        names = ", ".join(p.stem.replace("__", "/") for p in caches)
        raise HTTPException(400, f"multiple repos cached, pass ?repo=: {names}")
    return _load_path(caches[0])


def _load_path(path: Path) -> dict[str, Any]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/")
def root() -> dict[str, Any]:
    repos = (
        [p.stem.replace("__", "/") for p in sorted(CACHE_DIR.glob("*.json"))]
        if CACHE_DIR.exists()
        else []
    )
    return {"service": "review-insights", "cached_repos": repos}


@app.get("/reviewers")
def list_reviewers(repo: str | None = Query(None)) -> dict[str, Any]:
    cache = _resolve_cache(repo)
    return {"repo": cache.get("repo"), "reviewers": rules.reviewers_summary(cache)}


@app.get("/reviewers/{login}/stats")
def reviewer_stats(login: str, repo: str | None = Query(None)) -> dict[str, Any]:
    cache = _resolve_cache(repo)
    if login not in rules.all_reviewers(cache):
        raise HTTPException(404, f"reviewer '{login}' not found in {cache.get('repo')}")
    return rules.compute_stats(cache, login)


@app.get("/reviewers/{login}/insight")
def reviewer_insight(login: str, repo: str | None = Query(None)) -> dict[str, Any]:
    cache = _resolve_cache(repo)
    if login not in rules.all_reviewers(cache):
        raise HTTPException(404, f"reviewer '{login}' not found in {cache.get('repo')}")
    return pipeline.build_insight(cache, login)
