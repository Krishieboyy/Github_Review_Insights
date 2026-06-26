"""LLM layer — what the rules genuinely cannot do (CLAUDE.md section 3).

This module does the *meaning* work: classify the tone / substance of a review
comment, and whether it teaches its reasoning. The deterministic facts stay in
rules.py; nothing here is allowed to invent a number.

Uses the Google Gemini SDK (google-generativeai) with a JSON response schema so
every classification is schema-constrained, then validated with the same
pydantic model — the model cannot return a label outside the allowed set.

Graceful degradation: if GEMINI_API_KEY is unset, llm_available() is False
and callers (the /insight endpoint, the pipeline) fall back to the rule layer.
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

MODEL = "gemini-2.5-flash-lite"  # free-tier model with a higher daily request cap

# Retry policy for Gemini rate limits (429 / ResourceExhausted). Free tier is
# ~10-15 req/min, and build_insight() fires a whole batch, so back off and retry.
LLM_MAX_RETRIES = 4
LLM_BASE_DELAY = 2.0  # seconds; doubles each attempt
LLM_MAX_DELAY = 30.0

Tone = Literal["mentoring", "blunt", "neutral", "encouraging", "pedantic"]
Substance = Literal[
    "nit_style", "naming", "logic_bug", "architecture", "test_coverage", "question", "praise"
]


class Classification(BaseModel):
    tone: Tone
    substance: Substance
    teaches_why: bool


_AXES = """TONE — the reviewer's manner. Pick the single dominant one:
- mentoring: patient, guides the author, explains to help them grow
- blunt: terse and direct, no softening ("Unnecessary.", "This is wrong.")
- neutral: matter-of-fact, no strong affect
- encouraging: positive and supportive, praises effort or the work
- pedantic: fixated on minor correctness, rules, or style technicalities

SUBSTANCE — what the comment is fundamentally about. Pick the single dominant
one; when a comment both praises and asks for a change, classify by the
actionable ask, not the praise:
- nit_style: trivial style/formatting/whitespace
- naming: about identifier names
- logic_bug: points out a correctness or logic error
- architecture: design, structure, or abstraction concerns
- test_coverage: about tests or missing tests
- question: primarily asking / seeking information, not asserting
- praise: positive feedback with no actionable request

TEACHES_WHY — true if the comment explains the reasoning behind it (the "why",
a "because", a consequence, a rule); false if it only asserts or directs."""

SYSTEM = (
    "You classify a single code-review comment along three axes. Output "
    "only the structured fields — never invent or restate the comment.\n\n"
    + _AXES
)

BATCH_SYSTEM = (
    "You classify a numbered list of code-review comments along three axes. "
    "Return a JSON array with exactly one object per comment, in the SAME ORDER "
    "as given; the array length must equal the number of comments. Output only "
    "the structured fields — never invent or restate the comments.\n\n"
    + _AXES
)


class LLMUnavailable(RuntimeError):
    """Raised when an LLM call is attempted with no API key configured."""


def llm_available() -> bool:
    load_dotenv()
    return bool(os.getenv("GEMINI_API_KEY"))


def _model(system_instruction: str):
    """Configure Gemini and return a model handle with the given system prompt."""
    load_dotenv()
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise LLMUnavailable("GEMINI_API_KEY not set — the LLM layer is disabled")
    import google.generativeai as genai

    genai.configure(api_key=key)
    return genai.GenerativeModel(MODEL, system_instruction=system_instruction)


def get_client():
    """Return a Gemini model handle preloaded with the classification prompt.

    The system prompt is attached here (system_instruction) so classify() only
    sends the per-comment user text — the prompt itself is unchanged.
    """
    return _model(SYSTEM)


def _generate(model, content: str, generation_config: dict):
    """model.generate_content with exponential backoff on Gemini rate limits.

    Catches 429 / ResourceExhausted, backs off (2s, 4s, 8s, ... capped, +jitter),
    and re-raises after LLM_MAX_RETRIES. Other errors propagate immediately.
    Used by both classify() and synthesize(), so the whole build_insight() batch
    is covered.
    """
    from google.api_core.exceptions import ResourceExhausted

    delay = LLM_BASE_DELAY
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            return model.generate_content(content, generation_config=generation_config)
        except ResourceExhausted:
            if attempt == LLM_MAX_RETRIES:
                raise
            time.sleep(min(delay, LLM_MAX_DELAY) + random.uniform(0, 0.5))
            delay *= 2


def _render(body: str, path: str | None) -> str:
    head = f"File: {path}\n" if path else ""
    return f"{head}Comment:\n{body.strip()}"


def classify(body: str, *, path: str | None = None, client=None) -> Classification:
    """Classify one review comment. Returns a schema-validated Classification."""
    client = client or get_client()
    resp = _generate(
        client,
        _render(body, path),
        {
            "response_mime_type": "application/json",
            "response_schema": Classification,  # constrains generation to the schema
            "temperature": 0,
            # Gemini 2.x flash "thinks" before answering, and thinking tokens
            # count against this cap — give it headroom so the JSON isn't truncated.
            "max_output_tokens": 8192,
        },
    )
    # Gemini returns the JSON as text; pydantic re-validates the label set.
    return Classification.model_validate_json(resp.text)


def classify_many(comments: list[dict], *, client=None) -> list[Classification]:
    """Classify a list of {body, path?} dicts in a SINGLE batched call.

    Returns one Classification per input, in order. This is what the pipeline
    uses: it keeps /insight to ~2 LLM calls total (this + synthesize) instead of
    one per comment — essential under tight free-tier daily quotas.
    """
    if not comments:
        return []
    client = client or _model(BATCH_SYSTEM)

    blocks = []
    for i, c in enumerate(comments, 1):
        path = c.get("path")
        head = f" (file: {path})" if path else ""
        blocks.append(f"[{i}]{head}\n{(c.get('body') or '').strip()}")
    user = (
        f"Classify these {len(comments)} comments. Return exactly {len(comments)} "
        f"objects, one per comment, in the same order:\n\n" + "\n\n".join(blocks)
    )

    resp = _generate(
        client,
        user,
        {
            "response_mime_type": "application/json",
            "response_schema": list[Classification],
            "temperature": 0,
            "max_output_tokens": 8192,
        },
    )
    data = json.loads(resp.text)
    if not isinstance(data, list) or len(data) != len(comments):
        raise ValueError(
            f"batch classify returned {len(data) if isinstance(data, list) else '?'} "
            f"items for {len(comments)} comments"
        )
    return [Classification.model_validate(obj) for obj in data]


# --------------------------------------------------------------------------- #
# Synthesis — the one grounded cross-signal insight (CLAUDE.md section 4)
# --------------------------------------------------------------------------- #
SYNTHESIS_SYSTEM = """You analyze one code reviewer's habits on a single GitHub \
repository. You are given two things: deterministic rule-layer statistics (exact \
facts) and the distribution of an LLM classification over a sample of their \
comments. Produce exactly ONE insight.

Requirements:
- Build the insight FROM the numbers provided. Find a CROSS-SIGNAL correlation —
  connect two different signals, e.g. review latency vs. comment substance,
  verdict mix vs. substance, or tone vs. whether they explain their reasoning.
  A single restated stat is not an insight.
- Quote at least one actual statistic VERBATIM from the data (e.g. "median 3.1h",
  "78% nit_style", "changes_requested 40%").
- DO NOT invent, estimate, extrapolate, or round any figure that is not in the
  data. Use only the numbers given. Every number in your output must appear in
  the data above.
- If the data is too thin to support a correlation (very few reviews or an empty
  comment sample), say that plainly instead of forcing one.
- Address the reviewer directly ("You ..."). One or two sentences. No preamble,
  no bullet points, no headings — just the insight."""


def _pct(x: float | None) -> str:
    return f"{round(x * 100)}%" if x is not None else "n/a"


def _safe_div(num: int, denom: int) -> float | None:
    return num / denom if denom else None


def _dist_line(counts: dict[str, int], total: int) -> str:
    if not total:
        return "none"
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{label} {_pct(n / total)} ({n})" for label, n in ordered)


def render_synthesis_facts(stats: dict, distribution: dict) -> str:
    """Render the exact user-message facts block sent to the synthesis call.

    Public so the prompt can be inspected without making an API call.
    """
    s = stats
    lat = s["first_review_latency_hours"]
    vm = s["verdict_mix"]
    lang = (
        ", ".join(f"{k} {_pct(v)}" for k, v in list(s["language_affinity"].items())[:3])
        or "n/a"
    )
    hh = s["temporal"]["hour_histogram"]
    busy = sorted(hh.items(), key=lambda kv: -kv[1])[:3]
    busy_str = ", ".join(f"{h}:00 ({c})" for h, c in busy if c) or "n/a"

    return "\n".join(
        [
            f"REVIEWER: {s['login']}",
            "",
            "RULE-LAYER STATS (deterministic, exact):",
            f"- reviews: {s['review_count']}; PRs touched: {s['prs_touched']}",
            f"- first-review latency: median {lat['median']}h (n={lat['n']})",
            f"- verdict mix: approved {_pct(vm['approved'])}, "
            f"changes_requested {_pct(vm['changes_requested'])}, "
            f"commented {_pct(vm['commented'])}, "
            f"dismissed {_pct(vm['dismissed'])} (total {vm['total']})",
            f"- comments per PR: {s['comments_per_pr']}",
            f"- comment length: median {s['comment_length_words']['median']} words",
            f"- nit ratio: {_pct(s['nit_ratio'])}; "
            f"suggestion usage: {_pct(s['suggestion_usage'])}; "
            f"question ratio: {_pct(s['question_ratio'])}",
            f"- weekend ratio: {_pct(s['temporal']['weekend_ratio'])}; "
            f"busiest hours: {busy_str}",
            f"- language affinity: {lang}",
            f"- thread style: {s['thread_style']['replies']} replies / "
            f"{s['thread_style']['one_and_done']} one-and-done",
            "",
            f"CLASSIFIED COMMENT SAMPLE (n={distribution['sample_size']}):",
            f"- tone: {_dist_line(distribution['tone'], distribution['sample_size'])}",
            f"- substance: {_dist_line(distribution['substance'], distribution['sample_size'])}",
            f"- teaches_why: "
            f"{_pct(_safe_div(distribution['teaches_why_true'], distribution['sample_size']))} true",
        ]
    )


def synthesize(stats: dict, distribution: dict, *, client=None) -> str:
    """Produce the one grounded insight from rule stats + classified distribution."""
    client = client or _model(SYNTHESIS_SYSTEM)
    resp = _generate(
        client,
        render_synthesis_facts(stats, distribution),
        {
            "temperature": 0.3,
            "max_output_tokens": 8192,  # thinking-token headroom (see classify)
        },
    )
    return resp.text.strip()
