"""
level_integration.py — glue between the level selector and the existing
scorer, with ZERO edits to scorer.py.

It reuses scorer.score_issue (so every subscore you already tuned and
tested is untouched) and only:

  1. feeds it the level-appropriate weight preset, and
  2. for professionals, INVERTS the approachability term so a trivially
     easy "fix a typo" issue is penalised instead of floated to the top.

The inversion is done purely from the values score_issue already returns
(it hands back both 'breakdown' and 'subscores'), so nothing inside the
scorer has to change.

Drop-in usage:
    from level_integration import rank_issues_leveled
    from scorer import diversify          # unchanged

    ranked = rank_issues_leveled(issues, profile, profile.get("level"))
    final  = diversify(ranked, max_per_repo=2, guarantee_target_slots=3,
                       limit=max_issues)
"""

from __future__ import annotations

from typing import Optional

from scorer import score_issue
from level_weights import (
    resolve_weights,
    approachability_is_inverted,
)


def score_issue_leveled(
    issue: dict,
    profile: dict,
    level: Optional[str] = None,
    stage_weights: Optional[dict[str, float]] = None,
    blend: float = 1.0,
) -> dict:
    """
    Score one issue using the level's weight preset. For inverted-
    approachability levels (professional), rewrite the approachability
    contribution to weight * (1 - subscore) and fix the total.

    Returns the same dict shape score_issue does:
    {"score", "breakdown", "subscores", "bonus"}.
    """
    level = level or profile.get("level")
    weights = resolve_weights(level, stage_weights, blend=blend)
    result = score_issue(issue, profile, weights)

    if approachability_is_inverted(level):
        sub = result["subscores"].get("approachability", 0.0)
        w_appr = weights.get("approachability", 0.0)
        old_pts = result["breakdown"].get("approachability", 0.0)
        new_pts = round(w_appr * (1.0 - sub), 1)
        result["breakdown"]["approachability"] = new_pts
        result["score"] = round(result["score"] - old_pts + new_pts, 1)

    return result


def rank_issues_leveled(
    issues: list[dict],
    profile: dict,
    level: Optional[str] = None,
    stage_weights: Optional[dict[str, float]] = None,
    blend: float = 1.0,
) -> list[dict]:
    """
    Score and sort every issue by level. Mirrors scorer.rank_issues'
    sort key exactly (score, then career_relevance, stars, freshness),
    so downstream diversify() behaves identically. Issues are updated in
    place with score/breakdown/subscores/bonus.
    """
    level = level or profile.get("level")
    for issue in issues:
        issue.update(
            score_issue_leveled(issue, profile, level, stage_weights, blend)
        )

    def sort_key(i: dict):
        return (
            i.get("score", 0.0),
            i.get("subscores", {}).get("career_relevance", 0.0),
            i.get("repo_health", {}).get("stars", 0),
            i.get("subscores", {}).get("freshness", 0.0),
        )

    return sorted(issues, key=sort_key, reverse=True)