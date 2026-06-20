"""Mocked end-to-end test for pipeline.py.

Proves the wiring (collect -> health -> career -> score -> rank ->
explain) holds together and produces a sensible ordering, without
spending GitHub quota. Live behavior is identical but with real data.
"""

from datetime import datetime, timezone, timedelta

import pipeline
from pipeline import templated_explanation, FACTOR_PHRASES
from scorer import rank_issues
from weights import get_weights


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


print("=== A. templated_explanation only cites real top factors ===")
issue = {
    "score": 88.0,
    "breakdown": {"career_relevance": 25.0, "skill_fit": 18.0,
                  "approachability": 12.0, "repo_health": 5.0,
                  "freshness": 3.0, "time_fit": 2.0},
    "career": {"is_target_company_org": True},
}
exp = templated_explanation(issue)
print(f"  {exp}")
# Must mention the top factors' phrases and not invent anything.
assert FACTOR_PHRASES["career_relevance"] in exp
assert FACTOR_PHRASES["skill_fit"] in exp
assert "Target-company match" in exp
# A zero-score factor (none here in top 3) must never be cited falsely:
low = {"score": 10.0, "breakdown": {"freshness": 4.0, "skill_fit": 0.0,
       "approachability": 0.0, "repo_health": 0.0, "career_relevance": 0.0,
       "time_fit": 0.0}, "career": {}}
low_exp = templated_explanation(low)
print(f"  low-score issue: {low_exp}")
assert FACTOR_PHRASES["skill_fit"] not in low_exp  # 0-pt factor not cited

print("\n=== B. end-to-end ranking with a mocked client ===")

class FakeClient:
    """Returns a fixed candidate pool + repo metadata, no network."""
    def __init__(self):
        self._repos = {
            "vercel/next.js": {"full_name": "vercel/next.js",
                "stargazers_count": 120000, "pushed_at": days_ago(1),
                "open_issues_count": 2000, "language": "JavaScript",
                "topics": ["react", "web", "frontend"], "owner": {"login": "vercel"}},
            "smalldev/utils": {"full_name": "smalldev/utils",
                "stargazers_count": 80, "pushed_at": days_ago(2),
                "open_issues_count": 5, "language": "Python",
                "topics": ["web", "api"], "owner": {"login": "smalldev"}},
        }
    def get_repo(self, name): return self._repos[name]
    def get_repo_issue_comments(self, name, per_page=30): return []
    def _request(self, url, params=None): return {"total_count": 100}
    def search_org(self, name): return None

# Monkeypatch collect_candidate_issues to return a fixed pool.
def fake_collect(langs, topics, target_orgs, client=None, max_per_query=15):
    return [
        {"id": 1, "title": "Add docs for the API route helper",
         "repo_full_name": "smalldev/utils", "html_url": "http://x/1",
         "labels": ["good first issue", "documentation"], "body_length": 200,
         "checklist_items": 0, "linked_pr": False, "comments": 1,
         "created_at": days_ago(5), "assignee": None, "from_target_org": False},
        {"id": 2, "title": "Rework the entire SSR pipeline",
         "repo_full_name": "vercel/next.js", "html_url": "http://x/2",
         "labels": ["help wanted"], "body_length": 3000, "checklist_items": 9,
         "linked_pr": True, "comments": 50, "created_at": days_ago(200),
         "assignee": None, "from_target_org": True},
    ]

pipeline.collect_candidate_issues = fake_collect

profile = {
    "stack": {"python": "comfortable"}, "topics": ["api"],
    "college_year": "1st", "experience": "none",
    "companies": ["vercel"], "career_goal": "backend",
    "weekly_hours": 3, "preferred_types": ["docs"],
}

ranked = pipeline.run_pipeline(profile, max_issues=10, verbose=True)
print()
for i, iss in enumerate(ranked, 1):
    tgt = " [TARGET]" if iss["career"]["is_target_company_org"] else ""
    print(f"  #{i} score={iss['score']:.1f}{tgt}  {iss['title'][:45]}")
    print(f"      {templated_explanation(iss)}")

# For a 1st-year/none with only 3 hrs, the small friendly docs issue should
# win over the giant 200-day-old SSR rework, despite the latter being a
# target org. Approachability + time_fit dominate at this stage.
assert ranked[0]["id"] == 1, "early-stage low-hours user should get the small issue first"
print("\n  -> small approachable issue correctly ranked above the big one")

print("\nAll pipeline (mocked) tests passed.")