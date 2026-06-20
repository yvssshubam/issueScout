"""
weights.py — IssueScout component 5.

Scoring weight presets keyed by the user's stage (college year) and
open-source experience, so the ranking reflects where they are.

The six weights match scorer.py's factors and each preset sums to 100,
so a score reads as "out of 100" and breakdown points stay comparable
across presets.

Philosophy (from the spec):
  * Early (1st/2nd year, or no OSS experience): approachability and
    repo_health lead. Getting a first contribution merged matters more
    than prestige. Prefer small, well-supported issues. career_relevance
    is present but secondary.
  * Mid (3rd year, or some experience): balanced. skill_fit and
    career_relevance rise as the user can take on more and starts thinking
    about resumes.
  * Late (final year / grad, or experienced): career_relevance and
    prestige lead, because resume-visible contributions to target-company
    orgs matter most. Larger issues are acceptable, so time_fit relaxes.

Use get_weights(college_year, experience) to get the active preset. The
caller may pass overrides to tune any individual factor.
"""

from __future__ import annotations

from typing import Optional

# Canonical factor order, matching scorer.DEFAULT_WEIGHTS.
FACTORS = (
    "skill_fit", "approachability", "repo_health",
    "freshness", "career_relevance", "time_fit",
)

# Three stage presets, each summing to 100.
PRESETS: dict[str, dict[str, float]] = {
    "early": {
        "skill_fit": 15.0,
        "approachability": 28.0,
        "repo_health": 22.0,
        "freshness": 10.0,
        "career_relevance": 13.0,
        "time_fit": 12.0,
    },
    "mid": {
        "skill_fit": 22.0,
        "approachability": 18.0,
        "repo_health": 15.0,
        "freshness": 10.0,
        "career_relevance": 25.0,
        "time_fit": 10.0,
    },
    "late": {
        "skill_fit": 20.0,
        "approachability": 10.0,
        "repo_health": 12.0,
        "freshness": 8.0,
        "career_relevance": 42.0,
        "time_fit": 8.0,
    },
}

# How each college-year value maps to a stage.
YEAR_TO_STAGE = {
    "1st": "early", "first": "early",
    "2nd": "early", "second": "early",
    "3rd": "mid", "third": "mid",
    "final": "late", "4th": "late", "fourth": "late",
    "grad": "late", "graduate": "late", "masters": "late", "phd": "late",
}

# Experience can pull the stage earlier or later than the year alone.
EXPERIENCE_SHIFT = {
    "none": -1,        # no OSS experience -> lean more beginner
    "some": 0,
    "experienced": +1,  # experienced -> lean more career/prestige
}

_STAGE_ORDER = ["early", "mid", "late"]


def _normalize(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def stage_for(college_year: Optional[str],
              experience: Optional[str]) -> str:
    """
    Resolve a stage ("early"/"mid"/"late") from college year and
    experience. Experience shifts the year-derived stage by one step,
    clamped to the valid range. No-experience always lands at "early".
    """
    year = _normalize(college_year)
    exp = _normalize(experience)

    base_stage = YEAR_TO_STAGE.get(year, "mid")

    # A user with no OSS experience is treated as early regardless of year:
    # the priority is getting a first merge, not resume optimization.
    if exp == "none":
        return "early"

    shift = EXPERIENCE_SHIFT.get(exp, 0)
    idx = _STAGE_ORDER.index(base_stage)
    idx = max(0, min(len(_STAGE_ORDER) - 1, idx + shift))
    return _STAGE_ORDER[idx]


def get_weights(
    college_year: Optional[str] = None,
    experience: Optional[str] = None,
    overrides: Optional[dict[str, float]] = None,
) -> dict[str, float]:
    """
    Return the active weight dict for this user.

    Defaults to the preset matched by stage. `overrides` lets the caller
    (or a UI slider) tune individual factors; unknown keys are ignored,
    missing factors keep their preset value. The returned dict always
    contains all six factors.
    """
    stage = stage_for(college_year, experience)
    weights = dict(PRESETS[stage])  # copy so callers can't mutate the preset

    if overrides:
        for factor, value in overrides.items():
            if factor in weights:
                try:
                    weights[factor] = float(value)
                except (TypeError, ValueError):
                    continue
    return weights


def size_preference(college_year: Optional[str],
                    experience: Optional[str]) -> str:
    """
    Issue-size preference for this stage, a hint the UI/search can use.
    Early stages prefer small issues; late stages allow large ones.
    """
    stage = stage_for(college_year, experience)
    return {"early": "small", "mid": "medium", "late": "large"}[stage]


def describe_weights(weights: dict[str, float]) -> str:
    """One-line human summary of which factors lead, for the UI."""
    ordered = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    top = ", ".join(f"{f} ({v:.0f})" for f, v in ordered[:3])
    return f"Top priorities: {top}"