"""Classifier verification against hand-labeled fixtures.

This is an integration test: it makes real Google Gemini API calls, so it is
SKIPPED when GEMINI_API_KEY is unset (the rule layer and its tests don't
need a key). Run it with -s to see the full disagreement report:

    pytest tests/test_classifier.py -s

The point isn't a green checkmark — it's the per-comment report showing exactly
where the model disagrees with the labels in data/fixtures.json, so the SYSTEM
prompt in llm.py can be tuned against actual misses.
"""

import json
from pathlib import Path

import pytest

from app import llm

FIXTURES_PATH = Path(__file__).resolve().parent.parent / "data" / "fixtures.json"

requires_key = pytest.mark.skipif(
    not llm.llm_available(), reason="GEMINI_API_KEY not set — LLM layer disabled"
)


@pytest.fixture(scope="module")
def fixtures():
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def predictions(fixtures):
    # One batched call (the path the pipeline uses) — also keeps the test to a
    # single request, friendly to the free-tier daily quota.
    return llm.classify_many(fixtures)


@requires_key
def test_classifier_against_fixtures(fixtures, predictions):
    n = len(fixtures)
    tone_hits = sub_hits = tw_hits = 0
    disagreements = []

    for f, p in zip(fixtures, predictions):
        tone_ok = p.tone == f["tone"]
        sub_ok = p.substance == f["substance"]
        tw_ok = p.teaches_why == f["teaches_why"]
        tone_hits += tone_ok
        sub_hits += sub_ok
        tw_hits += tw_ok
        if not (tone_ok and sub_ok and tw_ok):
            disagreements.append((f, p, tone_ok, sub_ok, tw_ok))

    # --- the report (visible with -s) ---
    print("\n" + "=" * 72)
    print(f"CLASSIFIER vs FIXTURES  ({n} comments)")
    print(
        f"  tone        {tone_hits}/{n} = {tone_hits / n:.0%}\n"
        f"  substance   {sub_hits}/{n} = {sub_hits / n:.0%}\n"
        f"  teaches_why {tw_hits}/{n} = {tw_hits / n:.0%}"
    )
    if disagreements:
        print("\nDISAGREEMENTS (expected -> got):")
        for f, p, tone_ok, sub_ok, tw_ok in disagreements:
            print(f"\n  [#{f['id']}] {f['body'][:70]!r}")
            if not tone_ok:
                print(f"      tone:        {f['tone']} -> {p.tone}")
            if not sub_ok:
                print(f"      substance:   {f['substance']} -> {p.substance}")
            if not tw_ok:
                print(f"      teaches_why: {f['teaches_why']} -> {p.teaches_why}")
    else:
        print("\nNo disagreements — perfect match.")
    print("=" * 72)

    # Modest floors so the test stays informative, not flaky. Tone is the most
    # subjective axis, so it gets the lowest bar.
    assert sub_hits / n >= 0.7, "substance accuracy below 70% — tune the prompt"
    assert tw_hits / n >= 0.7, "teaches_why accuracy below 70% — tune the prompt"
    assert tone_hits / n >= 0.6, "tone accuracy below 60% — tune the prompt"
