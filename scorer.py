"""
scorer.py — IssueScout component 4.

The deterministic, explainable core. For each issue, compute a single
numeric score AND a human-readable breakdown of which factors contributed
and by how much. No language model anywhere here.

ARCHITECTURAL RULE 1: this code decides the ranking. The model never does.
The breakdown dict produced here is the ONLY thing the model is later
allowed to narrate (component 6), and it may cite nothing that isn't in it.

Six factors, each a 0..1 sub-score multiplied by a tunable weight:
  * skill_fit        — issue's repo languages/topics vs the user's stack
  * approachability   — beginner label, issue age, comments, has a body
  * repo_health       — liveness, responsiveness, issue balance (component 2)
  * freshness         — recently opened and unclaimed
  * career_relevance  — target-org match, domain match, prestige (component 3)
  * time_fit          — issue size vs the user's weekly hours (component 2)

Score = sum(weight[factor] * subscore[factor]). The breakdown reports the
points each factor added (weight * subscore, rounded), so the numbers are
directly readable, e.g. {"career_relevance": 18.0, "skill_fit": 12.0, ...}.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# Default weights. Component 5 (weights.py) supplies presets keyed by the
# user's college year + experience and can override these. Kept here so the
# scorer runs standalone. They sum to 100 for readable point totals.
DEFAULT_WEIGHTS: dict[str, float] = {
    "skill_fit": 20.0,
    "approachability": 20.0,
    "repo_health": 15.0,
    "freshness": 10.0,
    "career_relevance": 25.0,
    "time_fit": 10.0,
}

BEGINNER_LABELS = {
    "good first issue", "good-first-issue", "help wanted", "help-wanted",
    "beginner", "beginner-friendly", "first-timers-only", "easy",
    "low-hanging-fruit",
}

CONTRIB_TYPE_LABELS = {
    "docs": {"documentation", "docs", "doc"},
    "tests": {"test", "tests", "testing"},
    "bug fix": {"bug", "bugfix", "fix"},
    "small feature": {"enhancement", "feature", "feature-request"},
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _days_since(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


# --------------------------------------------------------------------- #
# Individual 0..1 sub-scores.
# --------------------------------------------------------------------- #

def skill_fit_subscore(
    repo_language: Optional[str],
    repo_topics: Iterable[str],
    user_stack: dict[str, str],
    user_topics: Iterable[str],
) -> float:
    """
    Overlap between the repo's language/topics and what the user knows.
    user_stack maps language -> level ("learning"/"comfortable"/"strong").
    A strong-level language match counts more than a learning-level one.
    Topic overlap adds a smaller secondary signal.
    """
    level_weight = {"learning": 0.5, "comfortable": 0.8, "strong": 1.0}
    stack_l = {k.lower(): v for k, v in user_stack.items()}

    lang_score = 0.0
    lang = (repo_language or "").strip().lower()
    if lang and lang in stack_l:
        lang_score = level_weight.get(stack_l[lang], 0.7)

    repo_topics_l = {t.lower() for t in repo_topics}
    user_topics_l = {t.lower() for t in user_topics}
    topic_overlap = len(repo_topics_l & user_topics_l)
    topic_score = min(topic_overlap / 2.0, 1.0)  # 2 shared topics saturate

    # Language is the stronger signal (0.7), topics secondary (0.3).
    return _clamp01(0.7 * lang_score + 0.3 * topic_score)


def approachability_subscore(issue: dict) -> float:
    """
    How friendly the issue is to a newcomer.
    + beginner label, + has a real description, gentle penalty for very
    high comment counts (long heated threads are intimidating / contested),
    gentle penalty for issues with no body.
    """
    labels = set(issue.get("labels", []))
    score = 0.0

    if labels & BEGINNER_LABELS:
        score += 0.5

    body_len = issue.get("body_length", 0)
    if body_len >= 80:
        score += 0.3      # has a meaningful description
    elif body_len > 0:
        score += 0.1

    comments = issue.get("comments", 0)
    if comments <= 3:
        score += 0.2      # quiet, likely still grabbable
    elif comments <= 10:
        score += 0.1
    # > 10 comments adds nothing (possibly contested / already in progress)

    return _clamp01(score)


def freshness_subscore(issue: dict) -> float:
    """
    Recently opened and unclaimed issues are better targets.
    Full marks if opened within ~30 days and no assignee; decays with age.
    """
    days = _days_since(issue.get("created_at"))
    if days is None:
        age_score = 0.5
    elif days <= 30:
        age_score = 1.0
    elif days >= 365:
        age_score = 0.1
    else:
        age_score = _clamp01(1.0 - (days - 30) / 335.0)

    unclaimed = 1.0 if issue.get("assignee") in (None, "") else 0.3
    return _clamp01(0.6 * age_score + 0.4 * unclaimed)


def repo_health_subscore(issue: dict) -> float:
    """Average of the repo-health sub-scores attached by component 2."""
    scores = issue.get("repo_health", {}).get("scores", {})
    if not scores:
        return 0.5
    parts = [
        scores.get("liveness", 0.5),
        scores.get("issue_balance", 0.5),
        scores.get("responsiveness", 0.0),
        scores.get("popularity", 0.0),
    ]
    return _clamp01(sum(parts) / len(parts))


def career_relevance_subscore(issue: dict) -> float:
    """
    Combine the career signals from component 3.
    Target-company-org match is the headline (0.5), then domain match
    (0.3) and prestige (0.2).
    """
    career = issue.get("career", {})
    target = 1.0 if career.get("is_target_company_org") else 0.0
    domain = career.get("domain_match", 0.0)
    prestige = career.get("prestige", 0.0)
    return _clamp01(0.5 * target + 0.3 * domain + 0.2 * prestige)


def time_fit_subscore(issue: dict, weekly_hours: float) -> float:
    """
    Match issue size to available hours. Low hours -> reward small issues;
    plenty of hours -> larger issues are fine too.
    size_score (from component 2) is 1.0 for the smallest issues.
    """
    size_score = issue.get("size_score", 0.5)  # 1.0 == smallest
    if weekly_hours <= 3:
        # tight on time: strongly prefer small
        return _clamp01(size_score)
    if weekly_hours <= 8:
        # moderate: mild preference for small, but mid is fine
        return _clamp01(0.4 + 0.6 * size_score)
    # lots of time: size barely matters, slight nod to anything scoped
    return _clamp01(0.7 + 0.3 * size_score)


# --------------------------------------------------------------------- #
# Preferred-contribution-type boost (small, additive, outside the weights).
# --------------------------------------------------------------------- #

def contrib_type_bonus(issue: dict, preferred_types: Iterable[str]) -> float:
    """Small additive bonus if the issue's labels match a preferred type."""
    labels = set(issue.get("labels", []))
    for pref in preferred_types:
        wanted = CONTRIB_TYPE_LABELS.get(pref.lower(), set())
        if labels & wanted:
            return 3.0  # flat readable bonus
    return 0.0


# --------------------------------------------------------------------- #
# Top-level scoring.
# --------------------------------------------------------------------- #

def score_issue(
    issue: dict,
    profile: dict,
    weights: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """
    Compute the score + breakdown for one issue.

    profile keys used:
      stack (dict lang->level), topics (list), weekly_hours (float),
      career_goal (str), preferred_types (list).
      repo_language / repo_topics may live on the issue or be passed via
      profile-independent enrichment done earlier.

    Returns a dict: {"score": float, "breakdown": {factor: points, ...},
                     "bonus": {...}}.
    The breakdown values are the POINTS each factor contributed
    (weight * subscore), already rounded for display and narration.
    """
    w = weights or DEFAULT_WEIGHTS
    stack = profile.get("stack", {})
    topics = profile.get("topics", [])
    weekly_hours = float(profile.get("weekly_hours", 5))
    preferred = profile.get("preferred_types", [])

    repo_language = issue.get("repo_language")
    repo_topics = issue.get("repo_topics", [])

    subscores = {
        "skill_fit": skill_fit_subscore(repo_language, repo_topics, stack,
                                        topics),
        "approachability": approachability_subscore(issue),
        "repo_health": repo_health_subscore(issue),
        "freshness": freshness_subscore(issue),
        "career_relevance": career_relevance_subscore(issue),
        "time_fit": time_fit_subscore(issue, weekly_hours),
    }

    breakdown = {
        factor: round(w.get(factor, 0.0) * sub, 1)
        for factor, sub in subscores.items()
    }

    bonus = {}
    ctype = contrib_type_bonus(issue, preferred)
    if ctype:
        bonus["preferred_type"] = ctype

    total = round(sum(breakdown.values()) + sum(bonus.values()), 1)

    return {
        "score": total,
        "breakdown": breakdown,
        "subscores": {k: round(v, 3) for k, v in subscores.items()},
        "bonus": bonus,
    }


def rank_issues(
    issues: list[dict],
    profile: dict,
    weights: Optional[dict[str, float]] = None,
) -> list[dict]:
    """
    Score every issue and return them sorted best-first. Each issue gets
    'score', 'breakdown', 'subscores', 'bonus' keys added in place.
    Ties broken by career relevance, then stars, then freshness.
    """
    for issue in issues:
        result = score_issue(issue, profile, weights)
        issue.update(result)

    def sort_key(i: dict):
        return (
            i.get("score", 0.0),
            i.get("subscores", {}).get("career_relevance", 0.0),
            i.get("repo_health", {}).get("stars", 0),
            i.get("subscores", {}).get("freshness", 0.0),
        )

    return sorted(issues, key=sort_key, reverse=True)


def diversify(
    ranked: list[dict],
    max_per_repo: int = 2,
    guarantee_target_slots: int = 3,
    limit: Optional[int] = None,
) -> list[dict]:
    """
    Apply presentation policy on top of the pure ranking:

      * max_per_repo — no single repo may take more than this many slots,
        so one repo's batch of near-identical chore issues can't dominate.
      * guarantee_target_slots — ensure at least this many target-company
        issues appear (if any exist), since surfacing target-org work is
        the app's headline value. They keep their score order.

    Input must already be scored+sorted by rank_issues. Returns a new list.
    """
    target = [i for i in ranked
              if i.get("career", {}).get("is_target_company_org")]
    chosen: list[dict] = []
    repo_counts: dict[str, int] = {}
    chosen_ids = set()

    def try_add(issue: dict, ignore_cap: bool = False) -> bool:
        repo = issue.get("repo_full_name", "")
        if id(issue) in chosen_ids:
            return False
        if not ignore_cap and repo_counts.get(repo, 0) >= max_per_repo:
            return False
        chosen.append(issue)
        chosen_ids.add(id(issue))
        repo_counts[repo] = repo_counts.get(repo, 0) + 1
        return True

    # First, reserve guaranteed target-org slots (still respecting the
    # per-repo cap so one target repo doesn't fill them all).
    reserved = 0
    for issue in target:
        if reserved >= guarantee_target_slots:
            break
        if try_add(issue):
            reserved += 1

    # Then fill the rest in score order under the per-repo cap.
    for issue in ranked:
        try_add(issue)

    # Re-sort the final selection by score so guaranteed targets sit in
    # their rightful place rather than jammed at the top.
    chosen.sort(key=lambda i: i.get("score", 0.0), reverse=True)
    return chosen[:limit] if limit else chosen


def top_factors(breakdown: dict[str, float], n: int = 3) -> list[tuple[str, float]]:
    """The n highest-contributing factors, for the explanation layer."""
    return sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)[:n]