"""Tests for scorer.py (component 4). Pure logic, no network.

Proves each sub-score behaves, the breakdown dict has the readable shape
the model will narrate, and a strong issue outranks a weak one.
"""

from datetime import datetime, timezone, timedelta

from scorer import (
    skill_fit_subscore, approachability_subscore, freshness_subscore,
    repo_health_subscore, career_relevance_subscore, time_fit_subscore,
    score_issue, rank_issues, top_factors, DEFAULT_WEIGHTS,
)


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


print("=== A. skill_fit_subscore ===")
strong = skill_fit_subscore("python", ["web", "api"],
                            {"Python": "strong"}, ["web"])
learning = skill_fit_subscore("python", [], {"Python": "learning"}, [])
none = skill_fit_subscore("rust", [], {"Python": "strong"}, [])
print(f"  strong python+topic={strong:.2f} learning={learning:.2f} "
      f"no-match={none:.2f}")
assert strong > learning > none and none == 0.0

print("=== B. approachability_subscore ===")
friendly = approachability_subscore({
    "labels": ["good first issue"], "body_length": 200, "comments": 1})
harsh = approachability_subscore({
    "labels": [], "body_length": 0, "comments": 40})
print(f"  friendly={friendly:.2f} harsh={harsh:.2f}")
assert friendly > 0.8 and harsh < 0.3

print("=== C. freshness_subscore ===")
fresh = freshness_subscore({"created_at": days_ago(5), "assignee": None})
stale = freshness_subscore({"created_at": days_ago(400), "assignee": "bob"})
print(f"  fresh+unclaimed={fresh:.2f} old+claimed={stale:.2f}")
assert fresh > 0.9 and stale < 0.3

print("=== D. repo_health_subscore ===")
healthy = repo_health_subscore({"repo_health": {"scores": {
    "liveness": 1.0, "issue_balance": 0.8, "responsiveness": 1.0,
    "popularity": 0.9}}})
print(f"  healthy repo avg={healthy:.2f}")
assert healthy > 0.8

print("=== E. career_relevance_subscore ===")
dream = career_relevance_subscore({"career": {
    "is_target_company_org": True, "domain_match": 1.0, "prestige": 1.0}})
irrelevant = career_relevance_subscore({"career": {
    "is_target_company_org": False, "domain_match": 0.0, "prestige": 0.1}})
print(f"  dream job repo={dream:.2f} irrelevant={irrelevant:.2f}")
assert dream > 0.9 and irrelevant < 0.1

print("=== F. time_fit_subscore (small issue, varying hours) ===")
small_issue = {"size_score": 1.0}
big_issue = {"size_score": 0.0}
print(f"  small/2h={time_fit_subscore(small_issue,2):.2f} "
      f"big/2h={time_fit_subscore(big_issue,2):.2f} "
      f"big/15h={time_fit_subscore(big_issue,15):.2f}")
assert time_fit_subscore(small_issue, 2) > time_fit_subscore(big_issue, 2)

print("\n=== G. score_issue: breakdown shape ===")
profile = {
    "stack": {"Python": "strong"}, "topics": ["web", "api"],
    "weekly_hours": 5, "career_goal": "backend",
    "preferred_types": ["docs"],
}
strong_issue = {
    "title": "Document the config loader",
    "repo_language": "python", "repo_topics": ["web", "api"],
    "labels": ["good first issue", "documentation"],
    "body_length": 300, "comments": 2, "created_at": days_ago(7),
    "assignee": None, "size_score": 0.9,
    "repo_health": {"stars": 5000, "scores": {
        "liveness": 1.0, "issue_balance": 0.8, "responsiveness": 1.0,
        "popularity": 0.9}},
    "career": {"is_target_company_org": True, "domain_match": 0.9,
               "prestige": 0.85},
}
result = score_issue(strong_issue, profile)
print(f"  score={result['score']}")
print(f"  breakdown={result['breakdown']}")
print(f"  bonus={result['bonus']}")
# Breakdown must be readable points, one per factor, summing (+bonus) to score.
assert set(result["breakdown"]) == set(DEFAULT_WEIGHTS)
assert abs(sum(result["breakdown"].values())
           + sum(result["bonus"].values()) - result["score"]) < 0.2
assert result["bonus"].get("preferred_type") == 3.0  # docs label matched

print("=== H. ranking: strong issue beats weak issue ===")
weak_issue = {
    "title": "Rewrite the entire async engine",
    "repo_language": "haskell", "repo_topics": [],
    "labels": [], "body_length": 0, "comments": 60,
    "created_at": days_ago(500), "assignee": "someone", "size_score": 0.0,
    "repo_health": {"stars": 3, "scores": {
        "liveness": 0.0, "issue_balance": 0.1, "responsiveness": 0.0,
        "popularity": 0.0}},
    "career": {"is_target_company_org": False, "domain_match": 0.0,
               "prestige": 0.05},
}
ranked = rank_issues([weak_issue, strong_issue], profile)
print(f"  #1 ({ranked[0]['score']}): {ranked[0]['title']!r}")
print(f"  #2 ({ranked[1]['score']}): {ranked[1]['title']!r}")
assert ranked[0]["title"] == strong_issue["title"]

print("=== I. top_factors (what the model will narrate) ===")
tf = top_factors(ranked[0]["breakdown"], n=3)
print(f"  top 3 factors: {tf}")
assert len(tf) == 3 and tf[0][1] >= tf[1][1] >= tf[2][1]

print("\nAll component-4 tests passed.")