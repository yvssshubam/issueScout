"""
level_weights.py — scoring weight presets keyed by the explicit level
the user picks (beginner / amateur / professional).

The six scoring factors are the same ones scorer.py already computes:
skill_fit, approachability, repo_health, freshness, career_relevance,
time_fit. Each preset sums to 100, so a score stays on a 0..100 scale.

The important idea: as level rises, approachability stops being a virtue.
A beginner wants the friendly, well-labeled, low-traffic issue. A
professional does not want "fix a typo in the README" floated to the top,
so for the professional preset approachability is both small AND inverted
(see INVERT_APPROACHABILITY): a trivially easy issue is penalised, not
rewarded.

If you already have a weights.py keyed by college year + experience, use
this as the primary selector when an explicit level is set, and let the
year/experience preset be a secondary nudge. resolve_weights() below shows
one clean way to combine them.
"""

from __future__ import annotations

from typing import Optional

LEVEL_WEIGHTS: dict[str, dict[str, float]] = {
    # Beginner: approachability and repo friendliness lead. Career
    # relevance present but secondary. Matches the numbers we tuned earlier.
    "beginner": {
        "approachability": 28,
        "repo_health": 22,
        "skill_fit": 18,
        "career_relevance": 13,
        "freshness": 12,
        "time_fit": 7,
    },
    # Amateur: balanced, with skill fit leading. Career relevance rises.
    "amateur": {
        "skill_fit": 26,
        "career_relevance": 22,
        "approachability": 18,
        "repo_health": 18,
        "freshness": 9,
        "time_fit": 7,
    },
    # Professional: career relevance leads decisively; skill fit second.
    # Approachability is small and inverted (see below).
    "professional": {
        "career_relevance": 42,
        "skill_fit": 22,
        "repo_health": 14,
        "approachability": 8,
        "freshness": 8,
        "time_fit": 6,
    },
}

# Levels for which a high approachability score should COUNT AGAINST an
# issue (the contribution becomes weight * (1 - approachability_subscore)).
INVERT_APPROACHABILITY = {"professional"}

# Preferred issue size per level, for the time_fit / size steering.
LEVEL_ISSUE_SIZE = {
    "beginner": "small",
    "amateur": "any",
    "professional": "large",
}

DEFAULT_LEVEL = "beginner"


def weights_for_level(level: Optional[str]) -> dict[str, float]:
    return LEVEL_WEIGHTS.get((level or DEFAULT_LEVEL).lower(),
                             LEVEL_WEIGHTS[DEFAULT_LEVEL])


def approachability_is_inverted(level: Optional[str]) -> bool:
    return (level or DEFAULT_LEVEL).lower() in INVERT_APPROACHABILITY


def resolve_weights(
    level: Optional[str],
    stage_weights: Optional[dict[str, float]] = None,
    blend: float = 0.75,
) -> dict[str, float]:
    """
    Primary selector is the explicit level. If you still compute a
    year/experience `stage_weights` preset, blend it in as a minority
    voice so an experienced first-year or an inexperienced final-year is
    nudged without overriding the level the user deliberately chose.

    blend=0.75 means 75% level, 25% stage. Pass blend=1.0 to ignore stage.
    """
    base = dict(weights_for_level(level))
    if not stage_weights or blend >= 1.0:
        return base
    keys = set(base) | set(stage_weights)
    mixed = {
        k: blend * base.get(k, 0.0) + (1 - blend) * stage_weights.get(k, 0.0)
        for k in keys
    }
    total = sum(mixed.values()) or 1.0
    return {k: v * 100 / total for k, v in mixed.items()}  # renormalise to 100