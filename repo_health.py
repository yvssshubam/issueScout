"""
repo_health.py — IssueScout component 2.

For a given issue (and its repository), compute real, deterministic health
and size signals using only code and the GitHub API. No language model.

These raw signals feed scorer.py, which turns them into weighted score
contributions. This module deliberately returns plain numbers and small
normalized 0..1 sub-scores, plus the raw values behind them, so the
explanation layer can cite concrete facts ("last commit 3 days ago").

Signals per repository:
  * liveness          — days since last push/commit, plus a 0..1 score
  * responsiveness    — has a maintainer commented on issues recently
  * issue_balance     — open-to-closed issue ratio, plus a 0..1 score
  * popularity        — star count, plus a damped 0..1 score
Signals per issue (for time-availability matching):
  * size_bucket       — "small" / "medium" / "large"
  * size_score        — 0..1 where 1 == smallest/most-scoped

Everything is wrapped so one repo's failure (deleted, rate-limited) does
not sink a whole batch: callers get a result dict with sensible neutral
defaults and an "ok" flag.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from github_client import GitHubClient, GitHubClientError


# --------------------------------------------------------------------- #
# Tunable thresholds. Kept here (not in scorer) because they describe
# what "healthy" means for a repo, independent of how it's weighted.
# --------------------------------------------------------------------- #

LIVENESS_FRESH_DAYS = 7      # pushed within a week -> full liveness
LIVENESS_DEAD_DAYS = 365     # no push for a year   -> zero liveness
RESPONSIVENESS_WINDOW_DAYS = 30   # "recent" maintainer activity window
STAR_SATURATION = 5000       # stars beyond this add little to popularity score

# Issue-size cutoffs. A rough proxy for how much work an issue is, used to
# match against the user's weekly hours. Tuned to be forgiving: most
# beginner issues land in "small" or "medium".
SIZE_SMALL_MAX_CHARS = 600
SIZE_LARGE_MIN_CHARS = 2000
SIZE_CHECKLIST_MEDIUM = 3     # this many checkboxes nudges toward larger
SIZE_CHECKLIST_LARGE = 7


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse a GitHub ISO-8601 timestamp into an aware datetime."""
    if not ts:
        return None
    try:
        # GitHub uses e.g. "2026-06-01T12:34:56Z"
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _days_since(ts: Optional[str]) -> Optional[float]:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------- #
# Individual signal computations.
# --------------------------------------------------------------------- #

def liveness_score(days_since_push: Optional[float]) -> float:
    """1.0 if pushed very recently, decaying linearly to 0 by DEAD_DAYS."""
    if days_since_push is None:
        return 0.5  # unknown -> neutral
    if days_since_push <= LIVENESS_FRESH_DAYS:
        return 1.0
    if days_since_push >= LIVENESS_DEAD_DAYS:
        return 0.0
    span = LIVENESS_DEAD_DAYS - LIVENESS_FRESH_DAYS
    return _clamp01(1.0 - (days_since_push - LIVENESS_FRESH_DAYS) / span)


def issue_balance_score(open_issues: int, closed_issues: int) -> float:
    """
    Healthy repos close issues. Score rewards a higher closed:open ratio.
    closed_fraction = closed / (open + closed), squashed gently so that a
    repo with some open issues isn't punished too hard.
    """
    total = open_issues + closed_issues
    if total <= 0:
        return 0.5
    closed_fraction = closed_issues / total
    # Gentle curve: 50% closed -> ~0.5, 80% closed -> ~0.8.
    return _clamp01(closed_fraction)


def popularity_score(stars: int) -> float:
    """
    Damped log scale so a 200-star repo isn't drowned by a 150k-star one.
    log1p(stars)/log1p(saturation), clamped to 1.
    """
    if stars <= 0:
        return 0.0
    return _clamp01(math.log1p(stars) / math.log1p(STAR_SATURATION))


def issue_size(body_length: int, checklist_items: int,
               linked_pr: bool) -> tuple[str, float]:
    """
    Estimate issue size from its body. Returns (bucket, size_score) where
    size_score is 1.0 for the smallest, well-scoped issues (best for low
    weekly hours) and approaches 0 for large ones.
    """
    score = 1.0

    # Body length contribution.
    if body_length <= SIZE_SMALL_MAX_CHARS:
        length_penalty = 0.0
    elif body_length >= SIZE_LARGE_MIN_CHARS:
        length_penalty = 0.6
    else:
        span = SIZE_LARGE_MIN_CHARS - SIZE_SMALL_MAX_CHARS
        length_penalty = 0.6 * (body_length - SIZE_SMALL_MAX_CHARS) / span
    score -= length_penalty

    # Checklist contribution: more boxes -> more sub-tasks -> bigger.
    if checklist_items >= SIZE_CHECKLIST_LARGE:
        score -= 0.3
    elif checklist_items >= SIZE_CHECKLIST_MEDIUM:
        score -= 0.15

    # A linked PR often means the work is partly underway / more involved.
    if linked_pr:
        score -= 0.1

    score = _clamp01(score)

    if score >= 0.66:
        bucket = "small"
    elif score >= 0.33:
        bucket = "medium"
    else:
        bucket = "large"
    return bucket, score


def maintainer_responsiveness(
    comments: list[dict],
    repo_owner: str,
    window_days: int = RESPONSIVENESS_WINDOW_DAYS,
) -> tuple[bool, Optional[float]]:
    """
    Did someone with write-ish association comment on an issue recently?

    GitHub issue comments carry author_association: OWNER, MEMBER,
    COLLABORATOR indicate a maintainer. We also count the repo owner's
    login as a maintainer. Returns (is_responsive, days_since_last_maint).
    """
    maintainer_assocs = {"OWNER", "MEMBER", "COLLABORATOR"}
    best_days: Optional[float] = None
    for c in comments:
        assoc = (c.get("author_association") or "").upper()
        login = (c.get("user") or {}).get("login", "")
        is_maint = assoc in maintainer_assocs or login == repo_owner
        if not is_maint:
            continue
        d = _days_since(c.get("created_at"))
        if d is None:
            continue
        if best_days is None or d < best_days:
            best_days = d
    if best_days is None:
        return False, None
    return best_days <= window_days, best_days


# --------------------------------------------------------------------- #
# Top-level: assemble all repo signals for one issue.
# --------------------------------------------------------------------- #

def _neutral_health(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "stars": 0,
        "language": None,
        "topics": [],
        "days_since_push": None,
        "open_issues": None,
        "closed_issues": None,
        "is_responsive": False,
        "days_since_maintainer": None,
        "scores": {
            "liveness": 0.5,
            "issue_balance": 0.5,
            "popularity": 0.0,
            "responsiveness": 0.0,
        },
    }


def compute_repo_health(
    repo_full_name: str,
    client: GitHubClient,
    check_responsiveness: bool = True,
    check_issue_balance: bool = True,
) -> dict[str, Any]:
    """
    Fetch repo metadata + recent comments and compute all repo-level
    health signals. Never raises for one repo: returns ok=False with
    neutral scores on failure so a batch keeps going.

    `check_issue_balance` gates the closed-issue Search call (one paced
    Search per repo). The web server turns it off for the candidate loop to
    stay fast; CLI/Streamlit keep it on. When off, issue_balance is neutral.
    """
    try:
        repo = client.get_repo(repo_full_name)
    except GitHubClientError as e:
        return _neutral_health(f"repo fetch failed: {e}")
    except Exception as e:  # noqa: BLE001 - defensive, keep batch alive
        return _neutral_health(f"unexpected: {e}")

    stars = repo.get("stargazers_count", 0)
    pushed_at = repo.get("pushed_at")
    days_push = _days_since(pushed_at)
    open_issues = repo.get("open_issues_count", 0)
    language = repo.get("language")
    topics = repo.get("topics", []) or []

    # GitHub's repo object only gives open_issues_count (which includes PRs).
    # Closed count needs a paced Search call, so it's optional.
    closed_issues = _closed_issue_count(repo_full_name, client) if check_issue_balance else 0

    owner = (repo.get("owner") or {}).get("login", "")

    is_responsive, days_maint = False, None
    if check_responsiveness:
        try:
            comments = client.get_repo_issue_comments(repo_full_name,
                                                      per_page=30)
            is_responsive, days_maint = maintainer_responsiveness(
                comments, owner
            )
        except GitHubClientError:
            pass  # leave as not-responsive/unknown

    return {
        "ok": True,
        "reason": "",
        "stars": stars,
        "language": language,
        "topics": topics,
        "days_since_push": days_push,
        "open_issues": open_issues,
        "closed_issues": closed_issues,
        "is_responsive": is_responsive,
        "days_since_maintainer": days_maint,
        "scores": {
            "liveness": liveness_score(days_push),
            "issue_balance": (issue_balance_score(open_issues, closed_issues)
                              if check_issue_balance else 0.5),
            "popularity": popularity_score(stars),
            "responsiveness": 1.0 if is_responsive else 0.0,
        },
    }


def _closed_issue_count(repo_full_name: str, client: GitHubClient) -> int:
    """
    Cheap closed-issue count via the search API's total_count. Falls back
    to 0 if unavailable. Cached by the client like any other GET.
    """
    try:
        data = client._request(  # reuse the rate-limit-aware path
            "https://api.github.com/search/issues",
            {"q": f"repo:{repo_full_name} is:issue is:closed", "per_page": 1},
        )
        return int(data.get("total_count", 0))
    except GitHubClientError:
        return 0


def enrich_issue_with_health(
    issue: dict,
    client: GitHubClient,
    repo_health_cache: Optional[dict[str, dict]] = None,
    check_responsiveness: bool = True,
    check_issue_balance: bool = True,
) -> dict:
    """
    Attach repo health + issue size signals to one issue dict in place.
    Reuses a per-run repo_health_cache so many issues from the same repo
    only trigger one set of API calls.
    """
    repo_name = issue.get("repo_full_name", "")
    cache = repo_health_cache if repo_health_cache is not None else {}

    if repo_name and repo_name in cache:
        health = cache[repo_name]
    elif repo_name:
        health = compute_repo_health(repo_name, client, check_responsiveness,
                                     check_issue_balance)
        cache[repo_name] = health
    else:
        health = _neutral_health("missing repo name")

    bucket, size_score = issue_size(
        issue.get("body_length", 0),
        issue.get("checklist_items", 0),
        issue.get("linked_pr", False),
    )

    issue["repo_health"] = health
    issue["size_bucket"] = bucket
    issue["size_score"] = size_score
    # Surface repo language/topics on the issue so scorer + career_map can
    # read them without another fetch. Don't clobber values already present.
    if not issue.get("repo_language"):
        issue["repo_language"] = health.get("language")
    if not issue.get("repo_topics"):
        issue["repo_topics"] = health.get("topics", [])
    return issue