"""Tests for weights.py (component 5). Pure logic, no network.

Proves stage resolution from year + experience, that presets sum to 100,
overrides apply, and (the real point) that the SAME issues rank differently
for a first-year vs a final-year because the weights differ.
"""

from weights import (
    PRESETS, FACTORS, stage_for, get_weights, size_preference,
    describe_weights,
)
from scorer import rank_issues


print("=== A. every preset sums to 100 and has all six factors ===")
for name, preset in PRESETS.items():
    total = sum(preset.values())
    print(f"  {name}: sum={total:.0f} factors={set(preset) == set(FACTORS)}")
    assert abs(total - 100) < 0.01
    assert set(preset) == set(FACTORS)

print("=== B. stage resolution from year + experience ===")
cases = [
    ("1st", "none", "early"),
    ("2nd", "some", "early"),
    ("3rd", "some", "mid"),
    ("3rd", "experienced", "late"),
    ("final", "some", "late"),
    ("grad", "experienced", "late"),
    ("1st", "experienced", "mid"),   # experience pulls a 1st-year up one
    ("final", "none", "early"),      # no experience always lands early
]
for year, exp, expected in cases:
    got = stage_for(year, exp)
    print(f"  year={year:5} exp={exp:11} -> {got:5} (expected {expected})")
    assert got == expected, f"{year}/{exp}: got {got}, want {expected}"

print("=== C. early preset leads with approachability; late with career ===")
early = get_weights("1st", "none")
late = get_weights("final", "some")
print(f"  early: {describe_weights(early)}")
print(f"  late:  {describe_weights(late)}")
assert early["approachability"] > early["career_relevance"]
assert late["career_relevance"] > late["approachability"]
assert late["career_relevance"] == max(late.values())

print("=== D. overrides apply, unknown keys ignored ===")
tuned = get_weights("3rd", "some", overrides={"skill_fit": 40, "bogus": 99})
print(f"  skill_fit overridden to {tuned['skill_fit']}, "
      f"'bogus' present? {'bogus' in tuned}")
assert tuned["skill_fit"] == 40.0 and "bogus" not in tuned

print("=== E. size_preference by stage ===")
print(f"  1st/none -> {size_preference('1st','none')}, "
      f"3rd/some -> {size_preference('3rd','some')}, "
      f"final/some -> {size_preference('final','some')}")
assert size_preference("1st", "none") == "small"
assert size_preference("final", "some") == "large"

print("\n=== F. the real test: same issues, different ranking by stage ===")
# Two issues:
#   beginner_issue — super approachable, healthy repo, NOT career-relevant
#   career_issue   — at a target company, prestigious, but harder/less friendly
beginner_issue = {
    "title": "Fix a typo in the README",
    "repo_language": "python", "repo_topics": ["docs"],
    "labels": ["good first issue", "documentation"],
    "body_length": 150, "comments": 1,
    "created_at": "2026-06-01T00:00:00Z", "assignee": None,
    "size_score": 1.0,
    "repo_health": {"stars": 1500, "scores": {
        "liveness": 1.0, "issue_balance": 0.9, "responsiveness": 1.0,
        "popularity": 0.7}},
    "career": {"is_target_company_org": False, "domain_match": 0.1,
               "prestige": 0.6},
}
career_issue = {
    "title": "Implement retry logic in the API client",
    "repo_language": "python", "repo_topics": ["api", "backend"],
    "labels": ["help wanted"],
    "body_length": 2800, "comments": 45,
    "created_at": "2026-04-01T00:00:00Z", "assignee": None,
    "size_score": 0.2,
    "repo_health": {"stars": 60000, "scores": {
        "liveness": 1.0, "issue_balance": 0.7, "responsiveness": 0.0,
        "popularity": 1.0}},
    "career": {"is_target_company_org": True, "domain_match": 0.95,
               "prestige": 1.0},
}

profile = {"stack": {"Python": "comfortable"}, "topics": ["api"],
           "weekly_hours": 5, "career_goal": "backend",
           "preferred_types": []}

# First-year, no experience: approachability-led weights.
early_w = get_weights("1st", "none")
early_ranked = rank_issues([dict(beginner_issue), dict(career_issue)],
                           profile, early_w)
print(f"  EARLY  #1: {early_ranked[0]['title']!r} "
      f"({early_ranked[0]['score']})")
print(f"  EARLY  #2: {early_ranked[1]['title']!r} "
      f"({early_ranked[1]['score']})")

# Final-year: career-led weights.
late_w = get_weights("final", "some")
late_ranked = rank_issues([dict(beginner_issue), dict(career_issue)],
                          profile, late_w)
print(f"  LATE   #1: {late_ranked[0]['title']!r} "
      f"({late_ranked[0]['score']})")
print(f"  LATE   #2: {late_ranked[1]['title']!r} "
      f"({late_ranked[1]['score']})")

# The whole point: the ordering flips between stages.
assert early_ranked[0]["title"] == beginner_issue["title"], \
    "early stage should favor the approachable issue"
assert late_ranked[0]["title"] == career_issue["title"], \
    "late stage should favor the career-relevant issue"
print("  -> ranking correctly flips between early and late stage")

print("\nAll component-5 tests passed.")