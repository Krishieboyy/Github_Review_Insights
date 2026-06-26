"""Deterministic rule-layer tests. No network, no LLM.

The fixture below is hand-built so every expected number is computed by hand in
the comments — if rules.py drifts, these break.

Calendar facts used:
  2026-01-05 = Monday   (weekday)
  2026-01-06 = Tuesday  (weekday)
  2026-01-10 = Saturday (weekend)
"""

import pytest

from app import rules

# --------------------------------------------------------------------------- #
# Fixture: two reviewers (alice, bob) across two PRs.
# --------------------------------------------------------------------------- #
CACHE = {
    "repo": "test/repo",
    "pull_requests": [
        {
            "number": 1,
            "created_at": "2026-01-05T10:00:00Z",  # Monday 10:00
            "reviews": [
                # alice first pass 3h after open
                {"user": "alice", "state": "APPROVED", "body": "",
                 "submitted_at": "2026-01-05T13:00:00Z"},
                # bob next day -> 24h latency
                {"user": "bob", "state": "CHANGES_REQUESTED", "body": "",
                 "submitted_at": "2026-01-06T10:00:00Z"},
            ],
            "review_comments": [
                {"user": "alice", "body": "nit: rename this", "path": "src/app.py",
                 "line": 5, "in_reply_to_id": None, "created_at": "2026-01-05T13:00:00Z"},
                {"user": "alice", "body": "why not use a dict here?", "path": "src/app.py",
                 "line": 9, "in_reply_to_id": None, "created_at": "2026-01-05T14:00:00Z"},
                {"user": "bob", "body": "```suggestion\nfoo\n```", "path": "README.md",
                 "line": 2, "in_reply_to_id": 999, "created_at": "2026-01-06T10:30:00Z"},
            ],
            "issue_comments": [
                {"user": "alice", "body": "LGTM thanks", "created_at": "2026-01-05T15:00:00Z"},
            ],
        },
        {
            "number": 2,
            "created_at": "2026-01-10T08:00:00Z",  # Saturday 08:00
            "reviews": [
                # alice first pass 4h after open
                {"user": "alice", "state": "COMMENTED", "body": "",
                 "submitted_at": "2026-01-10T12:00:00Z"},
            ],
            "review_comments": [
                {"user": "alice", "body": "nitpick on spacing", "path": "tests/test_x.py",
                 "line": 1, "in_reply_to_id": None, "created_at": "2026-01-10T12:00:00Z"},
            ],
            "issue_comments": [],
        },
    ],
}


@pytest.fixture
def alice():
    return rules.compute_stats(CACHE, "alice")


@pytest.fixture
def bob():
    return rules.compute_stats(CACHE, "bob")


# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #
def test_all_reviewers():
    assert rules.all_reviewers(CACHE) == ["alice", "bob"]


def test_reviewers_summary():
    summ = {r["login"]: r for r in rules.reviewers_summary(CACHE)}
    # alice: 2 reviews, 4 comments (3 inline [2 in PR1 + 1 in PR2] + 1 issue), PRs 1 & 2
    assert summ["alice"]["review_count"] == 2
    assert summ["alice"]["comment_count"] == 4
    assert summ["alice"]["prs_touched"] == 2
    # bob: 1 review, 1 inline comment, 1 PR
    assert summ["bob"]["review_count"] == 1
    assert summ["bob"]["comment_count"] == 1
    assert summ["bob"]["prs_touched"] == 1


# --------------------------------------------------------------------------- #
# Alice — the rich reviewer
# --------------------------------------------------------------------------- #
def test_alice_counts(alice):
    assert alice["review_count"] == 2          # APPROVED + COMMENTED
    assert alice["prs_touched"] == 2
    assert alice["total_text_comments"] == 4   # 3 inline/issue text + ... (bodies empty on reviews)


def test_alice_latency(alice):
    # PR1: 13:00 - 10:00 = 3h ; PR2: 12:00 - 08:00 = 4h ; median = 3.5
    assert alice["first_review_latency_hours"]["median"] == 3.5
    assert alice["first_review_latency_hours"]["n"] == 2


def test_alice_verdict_mix(alice):
    vm = alice["verdict_mix"]
    assert vm["approved"] == 0.5
    assert vm["commented"] == 0.5
    assert vm["changes_requested"] == 0.0
    assert vm["total"] == 2


def test_alice_comments_per_pr(alice):
    # 4 text comments / 2 PRs
    assert alice["comments_per_pr"] == 2.0


def test_alice_text_ratios(alice):
    # bodies: "nit: rename this"(3w,nit), "why not use a dict here?"(6w,?),
    #         "nitpick on spacing"(3w,nit), "LGTM thanks"(2w)
    assert alice["nit_ratio"] == 0.5            # 2/4
    assert alice["question_ratio"] == 0.25      # 1/4
    assert alice["suggestion_usage"] == 0.0     # 0/4
    # word counts sorted [2,3,3,6] -> median 3.0
    assert alice["comment_length_words"]["median"] == 3.0
    assert alice["comment_length_words"]["min"] == 2
    assert alice["comment_length_words"]["max"] == 6


def test_alice_temporal(alice):
    t = alice["temporal"]
    # weekday events (PR1, Mon): 2 reviews? only 1 review(13:00)+2 inline(13,14)+1 issue(15)=4
    assert t["weekday"] == 4
    # weekend events (PR2, Sat): 1 review(12)+1 inline(12)=2
    assert t["weekend"] == 2
    assert t["weekend_ratio"] == round(2 / 6, 3)
    assert t["hour_histogram"][12] == 2   # 1 review + 1 inline at 12:00 (PR2)


def test_alice_language_affinity(alice):
    # 3 inline comments, all .py -> Python 1.0
    assert alice["language_affinity"] == {"Python": 1.0}


def test_alice_thread_style(alice):
    ts = alice["thread_style"]
    assert ts["replies"] == 0
    assert ts["one_and_done"] == 3
    assert ts["reply_ratio"] == 0.0


# --------------------------------------------------------------------------- #
# Bob — single suggestion reply on docs
# --------------------------------------------------------------------------- #
def test_bob(bob):
    assert bob["review_count"] == 1
    assert bob["prs_touched"] == 1
    # PR1 created 10:00, bob review next day 10:00 -> 24h
    assert bob["first_review_latency_hours"]["median"] == 24.0
    assert bob["verdict_mix"]["changes_requested"] == 1.0
    # one inline text comment with a ```suggestion block
    assert bob["total_text_comments"] == 1
    assert bob["suggestion_usage"] == 1.0
    assert bob["nit_ratio"] == 0.0
    assert bob["question_ratio"] == 0.0
    # README.md -> Markdown
    assert bob["language_affinity"] == {"Markdown": 1.0}
    # in_reply_to_id set -> a reply
    assert bob["thread_style"]["replies"] == 1
    assert bob["thread_style"]["reply_ratio"] == 1.0
    # bob events both on Tue (weekday)
    assert bob["temporal"]["weekend"] == 0
