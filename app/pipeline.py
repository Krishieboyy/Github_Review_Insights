"""Insight pipeline: rules -> sample -> classify -> synthesize (CLAUDE.md §3-4).

Ties the deterministic and LLM layers together for the /insight endpoint, with
graceful degradation: no key -> rule-layer stats + a clear note, never an error.
"""

from __future__ import annotations

import random
import re
from typing import Any

from . import llm, rules

NIT_RE = re.compile(r"^\s*nit\b", re.IGNORECASE)
SAMPLE_SIZE = 40


def _reviewer_text_comments(cache: dict[str, Any], login: str) -> list[dict[str, Any]]:
    """All human-authored text by `login`: inline + issue + non-empty review bodies."""
    out: list[dict[str, Any]] = []
    for pr in cache.get("pull_requests", []):
        for r in pr.get("reviews", []):
            if r.get("user") == login and (r.get("body") or "").strip():
                out.append({"body": r["body"], "path": None})
        for c in pr.get("review_comments", []):
            if c.get("user") == login:
                out.append({"body": c.get("body") or "", "path": c.get("path")})
        for c in pr.get("issue_comments", []):
            if c.get("user") == login:
                out.append({"body": c.get("body") or "", "path": None})
    return [c for c in out if c["body"].strip()]


def sample_comments(
    cache: dict[str, Any], login: str, k: int = SAMPLE_SIZE, seed: int = 0
) -> list[dict[str, Any]]:
    """Stratified sample: all nits + all questions + longest few + random fill.

    Guarantees coverage of the range instead of k rubber-stamp "LGTM"s, and is
    deterministic (fixed seed) so /insight is reproducible.
    """
    items = _reviewer_text_comments(cache, login)
    if len(items) <= k:
        return items

    rng = random.Random(seed)
    nits = [c for c in items if NIT_RE.search(c["body"]) or "nitpick" in c["body"].lower()]
    questions = [c for c in items if "?" in c["body"]]
    longest = sorted(items, key=lambda c: -len(c["body"].split()))[:5]

    chosen: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(group: list[dict[str, Any]]) -> None:
        for c in group:
            if id(c) not in seen and len(chosen) < k:
                seen.add(id(c))
                chosen.append(c)

    add(nits)
    add(questions)
    add(longest)
    rest = [c for c in items if id(c) not in seen]
    rng.shuffle(rest)
    add(rest)
    return chosen[:k]


def classify_distribution(comments: list[dict[str, Any]], *, client=None) -> dict[str, Any]:
    """Classify a sample (one batched LLM call) and aggregate the distribution."""
    results = llm.classify_many(comments, client=client)
    tone: dict[str, int] = {}
    substance: dict[str, int] = {}
    teaches_why_true = 0
    for r in results:
        tone[r.tone] = tone.get(r.tone, 0) + 1
        substance[r.substance] = substance.get(r.substance, 0) + 1
        teaches_why_true += int(r.teaches_why)
    return {
        "sample_size": len(comments),
        "tone": tone,
        "substance": substance,
        "teaches_why_true": teaches_why_true,
    }


def build_insight(cache: dict[str, Any], login: str) -> dict[str, Any]:
    """Rule stats + (if a key is set) the classified distribution and synthesized insight.

    No key -> rule layer only, with an explicit note. Never raises on a missing key.
    """
    stats = rules.compute_stats(cache, login)

    if not llm.llm_available():
        return {
            "login": login,
            "llm_enabled": False,
            "insight": None,
            "note": (
                "LLM insight disabled — no GEMINI_API_KEY set. Returning the "
                "deterministic rule layer only. Set GEMINI_API_KEY in .env to "
                "enable the synthesized insight."
            ),
            "stats": stats,
        }

    sample = sample_comments(cache, login)
    distribution = classify_distribution(sample)  # one batched classify call
    insight = llm.synthesize(stats, distribution)  # + one synthesis call
    return {
        "login": login,
        "llm_enabled": True,
        "insight": insight,
        "classification": distribution,
        "stats": stats,
    }
