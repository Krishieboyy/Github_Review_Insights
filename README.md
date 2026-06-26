# Review-Insights

Profiles a GitHub repo's pull-request **review activity** per reviewer: deterministic
rules measure their *habits*, an LLM classifies the *meaning* of their comments, and a
single synthesis call connects the two into one cross-signal insight they couldn't see
on their own.

Scope is deliberately **one repo, not one user globally** — bounded data, fast demo,
and "a reviewer can run it on their own repo" stays trivially true.

The repo ships with a **pre-cached pull of `pydantic/pydantic` (150 PRs, 39 reviewers)**,
so the whole thing runs on real data from a clean clone with **no GitHub token and no
API key**.

---

## Quickstart (zero setup)

```bash
git clone <this-repo> review-insights
cd review-insights
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app
```

Then, in another terminal (or open <http://127.0.0.1:8000/docs> for the interactive UI):

```bash
curl http://127.0.0.1:8000/reviewers
curl http://127.0.0.1:8000/reviewers/Viicos/stats
curl http://127.0.0.1:8000/reviewers/Viicos/insight
```

The server auto-loads the single shipped cache, so `?repo=` is optional. Every endpoint
works against real `pydantic/pydantic` data immediately.

**What works with zero keys:** `/reviewers` and `/stats` are fully functional, and
`/insight` returns the complete **rule layer** for the reviewer plus a note that the
synthesized one-sentence insight is disabled. Nothing hard-fails on a missing key.

**To enable the synthesized insight** (the LLM sentence), add a free Gemini key:

```bash
cp .env.example .env          # Windows: copy .env.example .env
# put your key in .env:  GEMINI_API_KEY=...   (free: https://aistudio.google.com/apikey)
# restart uvicorn
curl http://127.0.0.1:8000/reviewers/Viicos/insight   # now includes the grounded insight
```

---

## Endpoints

| Endpoint | What it returns | Needs a key? |
|---|---|---|
| `GET /` | Service info + which repos are cached | no |
| `GET /reviewers` | Roster of reviewers found + review/comment/PR counts | no |
| `GET /reviewers/{login}/stats` | Pure rule-layer numbers for one reviewer | no |
| `GET /reviewers/{login}/insight` | Rule stats + classified comment distribution + the one synthesized insight | LLM part needs `GEMINI_API_KEY`; degrades to rules-only without |

All accept an optional `?repo=owner/name` (defaults to the only cached repo).

---

## How the two layers split the work

The whole design hinges on **both layers being load-bearing** — neither is decorative.

**Rule layer ([`app/rules.py`](app/rules.py)) — deterministic, no LLM, no network.**
These are *facts*, computed straight from the cache and never model-guessed: review count,
first-review latency, verdict mix (% approved / changes-requested / commented),
comments-per-PR, comment length, nit ratio, suggestion usage, question ratio, hour-of-day
and weekday/weekend split, language affinity (from comment file paths), and thread style
(one-and-done vs. back-and-forth). This layer is the dependable core and runs with no key.

**LLM layer ([`app/llm.py`](app/llm.py)) — Google Gemini, the things rules can't do.**
1. **Classify** the *meaning* of sampled comments — `tone` (mentoring / blunt / neutral /
   encouraging / pedantic), `substance` (nit_style / naming / logic_bug / architecture /
   test_coverage / question / praise), and `teaches_why` (does it explain the reasoning,
   or just assert?). A regex can spot a `?`; it can't tell mentoring from curt.
2. **Synthesize** the one insight — fed the *actual numbers*, told to build the insight
   from them, cite a real stat, and explicitly forbidden from inventing figures.

**Token staging:** rules aggregate first, then ~40 comments are sampled **stratified**
(all nits, all questions, the longest few, plus a random fill) so the classifier sees the
range, not 40 rubber-stamp "LGTM"s. Classification is sent as a **single batched call**,
then one synthesis call — so `/insight` is ~2 LLM calls total, not one per comment.

The payoff is a **cross-signal correlation** — e.g. *"You review fast (median 3.1h) but
57% of your comments are `nit_style`, delivered `blunt 45%` and usually without explaining
why (`teaches_why 22%`)."* — the thing no one audits about their own reviewing.

Data comes from the **three distinct GitHub endpoints** (kept separate end-to-end, bots
filtered at ingest): `/pulls/{n}/reviews` (verdicts + latency), `/pulls/{n}/comments`
(inline diff-anchored substance), `/issues/{n}/comments` (conversational comments).

---

## Running against your own repo

Ingest writes a per-repo JSON cache; everything else reads it.

```bash
# add GITHUB_TOKEN to .env first (classic PAT, no scopes needed for public repos)
python -m app.github_client owner/name 150       # fetch + cache up to 150 PRs
curl "http://127.0.0.1:8000/reviewers?repo=owner/name"
```

A token lifts the rate limit from 60 → 5,000 req/hr; the ~150-PR pull is a one-time
~450-request cost, then cached.

---

## Tests

```bash
python -m pytest tests/test_rules.py        # deterministic — no network, no key
python -m pytest tests/test_classifier.py   # runs the classifier against hand-labeled
                                            # fixtures; skips cleanly without GEMINI_API_KEY
```

`tests/test_rules.py` pins every signal against a hand-computed two-reviewer fixture.
`tests/test_classifier.py` checks the classifier against [`data/fixtures.json`](data/fixtures.json)
(10 hand-labeled comments) and prints a per-comment disagreement report with `-s`.

---

## Layout

```
app/
  github_client.py   fetch + paginate the 3 endpoints, bot-filter, cache
  rules.py           deterministic signals (the dependable core)
  llm.py             Gemini classify() + classify_many() + synthesize()
  pipeline.py        ingest -> rules -> sample -> classify -> synthesize
  main.py            FastAPI app + endpoints
cache/
  pydantic__pydantic.json   shipped demo data (150 PRs) — runs token-free
data/
  fixtures.json      hand-labeled comments for the classifier test
tests/
```

---

## Known limitations / what I'd do next

- **`created_at` is a proxy for review-request time (latency signal).** `first_review_latency`
  measures PR `created_at` → first review, but the PR may have been opened long before this
  reviewer was asked. *Production:* use the review-request **timeline event**
  (`GET /repos/{o}/{r}/issues/{n}/timeline`, the `review_requested` event for that reviewer)
  as the true clock start.

- **Per-PR loop instead of the repo-wide bulk comment endpoints.** I fetch reviews +
  comments per PR for simplicity and correctness. *Production:* use the bulk
  `GET /repos/{o}/{r}/pulls/comments` and `/issues/comments` to pull all comments in far
  fewer paginated calls — noting there is **no** repo-wide reviews endpoint, so verdicts
  still require a per-PR loop.

- **No per-contributor tone comparison yet.** Each reviewer is profiled in isolation.
  *Production:* compute a repo-wide baseline and express each reviewer relative to it
  ("you request changes 2× more often than the median maintainer"), and support direct
  reviewer-vs-reviewer tone/substance diffs.

- **Legacy `google-generativeai` SDK.** It works but is frozen (Google's deprecation
  notice points to the successor). *Production:* migrate to the maintained **`google-genai`**
  unified SDK; the call surface differs but the classify/synthesize contracts stay the same.

- **Batched classification + free-tier rate limits.** Classification is one batched call to
  keep `/insight` at ~2 LLM calls, and there's exponential backoff on 429s
  ([`app/llm.py`](app/llm.py) `_generate`). The Gemini **free tier is tiny (~20 req/day on
  this key)**, which is why the live insight is quota-bound. *Production:* honor the server's
  `retry_delay` / `Retry-After` hint instead of a fixed schedule, run on a paid tier (or a
  queue with concurrency limits), and cache classifications so re-runs cost nothing.

---

## Design note (200 words)

_Written separately._
