# IssueScout

**Find a real, beginner-friendly (or career-relevant) open-source issue to work
on — matched to your skills, stage, and target companies.**

The thing that makes IssueScout different from a generic "ask an LLM for issues"
tool is a deliberate architectural line:

> **Scoring and ranking are pure, deterministic Python. The LLM is confined to
> two narrow jobs: parsing free-text interests, and phrasing an explanation that
> was already computed by the rules.**

Nothing about *which* issues you see, or *in what order*, is decided by a model.
Given the same inputs and the same GitHub state, you get the same ranking every
time. The scoring spine (`scorer.py`, `weights.py`, `level_weights.py`,
`repo_health.py`, `career_map.py`) is testable and reproducible; the model is a
presentation layer, not the brain.

## How it works

1. **Collect** candidate issues from three sources, scaled to your career stage:
   your target companies' orgs, your languages, and topic-driven repos
   (`github_client.py`).
2. **Enrich** each with repo-health signals — activity, issue balance,
   popularity, maintainer responsiveness (`repo_health.py`).
3. **Score** deterministically across skill fit, approachability, stage fit,
   repo health, and target-company match (`scorer.py` + the weight tables).
4. **Explain** the top matches. The factors are pre-computed; the LLM (or a
   built-in template) just turns them into a sentence (`llm.py`).

A single-page web UI (`web/index.html`) walks you through an adaptive onboarding
flow and renders the ranked results; `server.py` serves it and exposes one JSON
endpoint, `POST /api/match`.

## Run

```bash
pip install -r requirements.txt          # only dependency is `requests`
cp env.issuescout.example env.issuescout  # then add your GitHub token
python server.py                          # open http://localhost:8000
```

- **GitHub token:** create a classic PAT (no scopes needed for public data) at
  https://github.com/settings/tokens and put it in `env.issuescout`. It raises
  your search rate limit from ~10/min to ~30/min. `env.issuescout` is gitignored.
- **LLM backend:** set `LLM_BACKEND=ollama` (local dev) or `gemini` (deploy) in
  `env.issuescout`. If no model is reachable, explanations fall back to a
  deterministic template automatically — the ranking is identical either way.

The Streamlit app (`app.py`) still works for the original sidebar workflow.

## Design notes

- **Career-stage stratification.** Early-career and late-career users get
  different searches and weightings, not one averaged flow. The newcomer barrier
  to open source is real and well-documented; the onboarding is built around
  "finding a way to start."
- **Adaptive onboarding.** The contribution path you choose (code, design, docs,
  translation, data, community, accessibility, infra) changes the questions —
  e.g. translation asks for human languages, design for tools, docs skips the
  language question entirely.
- **Repo-health signals** surface the two things contributors complain about
  most: confusing docs and unresponsive maintainers.

## Performance

A search returns in roughly **5–9 seconds cold** and is **near-instant once
cached**. Getting there was the bulk of the engineering work:

- The web path **collects searches in parallel** (capped thread pool) instead of
  serially, and does **one search per target org** instead of two.
- The per-issue LLM call was removed from the results list (it was the dominant
  latency); explanations are templated there, with the model reserved for
  free-text parsing.
- Per-candidate closed-issue and responsiveness Searches are deferred to only the
  issues actually shown.
- Inputs are capped (4 languages / 5 topics / 5 targets) so a heavy query can't
  exhaust GitHub's per-minute search budget; the client keeps a circuit breaker
  that degrades to partial results instead of hanging.

The floor is the number of live, rate-limited GitHub round-trips times your
network latency, so sub-5s on a cold heavy query isn't reachable without
searching less — a deliberate, explainable tradeoff rather than a bug.

## Known limitations / next

- **Not deployed yet.** Runs locally; a hosted link is the next milestone.
- **Contribution-path matching is soft.** The chosen path biases ranking and
  search, but does not yet hard-filter to, say, only `documentation`-labeled
  issues (many repos label inconsistently, so a strict filter often returns
  nothing).

## Project layout

```
server.py            stdlib HTTP server + POST /api/match (web path)
app.py               Streamlit UI (original workflow)
pipeline.py          orchestration: collect -> enrich -> score -> rank
github_client.py     GitHub REST/Search client, caching, rate-limit handling
repo_health.py       repo-level health signals
scorer.py            deterministic scoring
weights.py / level_weights.py   weight tables per career stage
career_map.py        company -> GitHub org resolution, domain mapping
llm.py               interest parsing + explanation phrasing (templated fallback)
storage.py           small "already seen" store
web/index.html       single-file onboarding + results UI
tests/ (test_*.py)   unit tests for the deterministic core
```
