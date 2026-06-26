"""Deterministic reviewer signals — the dependable core.

NO LLM, NO NETWORK lives here. Every number is computed straight from the
cached blob produced by github_client. These are facts; they must never be
model-guessed (CLAUDE.md section 3).

The three GitHub endpoints stay distinct end-to-end:
  - reviews          -> verdict_mix, first_review_latency
  - review_comments  -> inline substance: nits, suggestions, language, threads
  - issue_comments   -> conversational comments
Text signals (nit/suggestion/question/length) pool all human-authored text;
language_affinity and thread_style use only the inline comments that carry a
`path` / `in_reply_to_id`.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime
from typing import Any

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
NIT_PREFIX = re.compile(r"^\s*nit\b", re.IGNORECASE)
SUGGESTION_BLOCK = "```suggestion"

# extension -> language bucket for language_affinity
_EXT_LANG = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JS/TS",
    ".jsx": "JS/TS",
    ".ts": "JS/TS",
    ".tsx": "JS/TS",
    ".md": "Markdown",
    ".rst": "Docs",
    ".txt": "Docs",
    ".json": "Config",
    ".toml": "Config",
    ".yaml": "Config",
    ".yml": "Config",
    ".cfg": "Config",
    ".ini": "Config",
    ".c": "C/C++",
    ".h": "C/C++",
    ".cpp": "C/C++",
    ".cc": "C/C++",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".rb": "Ruby",
    ".html": "HTML",
    ".css": "CSS",
    ".sh": "Shell",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _word_count(body: str) -> int:
    return len(body.split())


def _is_nit(body: str) -> bool:
    return bool(NIT_PREFIX.search(body)) or "nitpick" in body.lower()


def _lang_bucket(path: str | None) -> str:
    if not path:
        return "other"
    dot = path.rfind(".")
    if dot == -1:
        return "other"
    return _EXT_LANG.get(path[dot:].lower(), "other")


def _ratio(num: int, denom: int) -> float:
    return num / denom if denom else 0.0


def _round(x: float | None, n: int = 3) -> float | None:
    return round(x, n) if x is not None else None


# --------------------------------------------------------------------------- #
# Per-reviewer event collection
# --------------------------------------------------------------------------- #
def _collect(cache: dict[str, Any], login: str) -> dict[str, list]:
    """Gather every event authored by `login`, grouped by source."""
    reviews: list[dict] = []       # verdicts with submitted_at + PR created_at
    text_items: list[dict] = []    # pooled text: inline + issue + non-empty review bodies
    inline: list[dict] = []        # inline review_comments only (path / in_reply_to_id)
    event_ts: list[datetime] = []  # all timestamps for temporal histogram
    prs_touched: set[int] = set()
    # per-PR earliest review submit time for this reviewer
    pr_first_review: dict[int, datetime] = {}
    pr_created: dict[int, datetime | None] = {}

    for pr in cache.get("pull_requests", []):
        n = pr.get("number")
        created = _parse_ts(pr.get("created_at"))
        pr_created[n] = created

        for r in pr.get("reviews", []):
            if r.get("user") != login:
                continue
            prs_touched.add(n)
            reviews.append(r)
            sub = _parse_ts(r.get("submitted_at"))
            if sub:
                event_ts.append(sub)
                if n not in pr_first_review or sub < pr_first_review[n]:
                    pr_first_review[n] = sub
            body = (r.get("body") or "").strip()
            if body:
                text_items.append({"body": body, "ts": sub, "source": "review"})

        for c in pr.get("review_comments", []):
            if c.get("user") != login:
                continue
            prs_touched.add(n)
            ts = _parse_ts(c.get("created_at"))
            if ts:
                event_ts.append(ts)
            inline.append(c)
            text_items.append({"body": c.get("body") or "", "ts": ts, "source": "inline"})

        for c in pr.get("issue_comments", []):
            if c.get("user") != login:
                continue
            prs_touched.add(n)
            ts = _parse_ts(c.get("created_at"))
            if ts:
                event_ts.append(ts)
            text_items.append({"body": c.get("body") or "", "ts": ts, "source": "issue"})

    # latency per PR: hours from PR created_at -> reviewer's first review on it
    latencies: list[float] = []
    for n, first in pr_first_review.items():
        created = pr_created.get(n)
        if created and first:
            latencies.append((first - created).total_seconds() / 3600.0)

    return {
        "reviews": reviews,
        "text_items": text_items,
        "inline": inline,
        "event_ts": event_ts,
        "prs_touched": prs_touched,
        "latencies": latencies,
    }


# --------------------------------------------------------------------------- #
# Public: stats for one reviewer
# --------------------------------------------------------------------------- #
def compute_stats(cache: dict[str, Any], login: str) -> dict[str, Any]:
    ev = _collect(cache, login)
    reviews = ev["reviews"]
    text_items = ev["text_items"]
    inline = ev["inline"]
    prs_touched = ev["prs_touched"]

    # --- verdict mix ---
    verdicts = {"approved": 0, "changes_requested": 0, "commented": 0, "dismissed": 0}
    _state_key = {
        "APPROVED": "approved",
        "CHANGES_REQUESTED": "changes_requested",
        "COMMENTED": "commented",
        "DISMISSED": "dismissed",
    }
    for r in reviews:
        key = _state_key.get(r.get("state"))
        if key:
            verdicts[key] += 1
    total_reviews = sum(verdicts.values())
    verdict_mix = {k: _round(_ratio(v, total_reviews)) for k, v in verdicts.items()}

    # --- text signals (pooled) ---
    bodies = [t["body"] for t in text_items]
    word_counts = [_word_count(b) for b in bodies]
    n_text = len(bodies)
    length_buckets = {"1-5": 0, "6-20": 0, "21-50": 0, "51+": 0}
    for w in word_counts:
        if w <= 5:
            length_buckets["1-5"] += 1
        elif w <= 20:
            length_buckets["6-20"] += 1
        elif w <= 50:
            length_buckets["21-50"] += 1
        else:
            length_buckets["51+"] += 1

    nit = sum(1 for b in bodies if _is_nit(b))
    suggestion = sum(1 for b in bodies if SUGGESTION_BLOCK in b)
    question = sum(1 for b in bodies if "?" in b)

    # --- temporal ---
    hour_hist = {h: 0 for h in range(24)}
    weekday = weekend = 0
    for ts in ev["event_ts"]:
        hour_hist[ts.hour] += 1
        if ts.weekday() >= 5:
            weekend += 1
        else:
            weekday += 1
    total_events = weekday + weekend

    # --- language affinity (inline only) ---
    lang_counts: dict[str, int] = {}
    for c in inline:
        b = _lang_bucket(c.get("path"))
        lang_counts[b] = lang_counts.get(b, 0) + 1
    n_inline_paths = sum(lang_counts.values())
    language_affinity = {
        k: _round(_ratio(v, n_inline_paths))
        for k, v in sorted(lang_counts.items(), key=lambda kv: -kv[1])
    }

    # --- thread style (inline only) ---
    replies = sum(1 for c in inline if c.get("in_reply_to_id"))
    one_and_done = len(inline) - replies

    return {
        "login": login,
        "review_count": total_reviews,
        "prs_touched": len(prs_touched),
        "first_review_latency_hours": {
            "median": _round(statistics.median(ev["latencies"]) if ev["latencies"] else None, 2),
            "n": len(ev["latencies"]),
        },
        "verdict_mix": {**verdict_mix, "counts": verdicts, "total": total_reviews},
        "comments_per_pr": _round(_ratio(n_text, len(prs_touched))),
        "comment_length_words": {
            "median": _round(statistics.median(word_counts) if word_counts else None, 1),
            "min": min(word_counts) if word_counts else None,
            "max": max(word_counts) if word_counts else None,
            "distribution": length_buckets,
        },
        "nit_ratio": _round(_ratio(nit, n_text)),
        "suggestion_usage": _round(_ratio(suggestion, n_text)),
        "question_ratio": _round(_ratio(question, n_text)),
        "total_text_comments": n_text,
        "temporal": {
            "hour_histogram": hour_hist,
            "weekday": weekday,
            "weekend": weekend,
            "weekend_ratio": _round(_ratio(weekend, total_events)),
        },
        "language_affinity": language_affinity,
        "thread_style": {
            "replies": replies,
            "one_and_done": one_and_done,
            "reply_ratio": _round(_ratio(replies, len(inline))),
        },
    }


# --------------------------------------------------------------------------- #
# Public: roster of reviewers
# --------------------------------------------------------------------------- #
def all_reviewers(cache: dict[str, Any]) -> list[str]:
    found: set[str] = set()
    for pr in cache.get("pull_requests", []):
        for key in ("reviews", "review_comments", "issue_comments"):
            for item in pr.get(key, []):
                u = item.get("user")
                if u:
                    found.add(u)
    return sorted(found)


def reviewers_summary(cache: dict[str, Any]) -> list[dict[str, Any]]:
    """Lightweight per-reviewer counts for the roster endpoint."""
    counts: dict[str, dict[str, Any]] = {}

    def bump(login: str | None, field: str, pr_number: int) -> None:
        if not login:
            return
        rec = counts.setdefault(
            login, {"login": login, "review_count": 0, "comment_count": 0, "_prs": set()}
        )
        rec[field] += 1
        rec["_prs"].add(pr_number)

    for pr in cache.get("pull_requests", []):
        n = pr.get("number")
        for r in pr.get("reviews", []):
            bump(r.get("user"), "review_count", n)
        for c in pr.get("review_comments", []):
            bump(c.get("user"), "comment_count", n)
        for c in pr.get("issue_comments", []):
            bump(c.get("user"), "comment_count", n)

    out = []
    for rec in counts.values():
        rec["prs_touched"] = len(rec.pop("_prs"))
        out.append(rec)
    out.sort(key=lambda r: (r["review_count"], r["comment_count"]), reverse=True)
    return out
