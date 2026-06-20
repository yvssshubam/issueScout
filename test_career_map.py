"""Tests for career_map.py (component 3).

Covers company->org resolution (static + fallback + cache), domain_match,
prestige, and the full career_signal/enrich path. Uses a FakeClient so no
GitHub quota is spent. A LIVE org-lookup line at the end uses your token.
"""

import json
import os
import tempfile
from pathlib import Path

import career_map
from career_map import CareerMap, enrich_issue_with_career


class FakeClient:
    def __init__(self, lookup=None):
        self.lookup = lookup or {}
        self.search_calls = 0

    def search_org(self, name):
        self.search_calls += 1
        return self.lookup.get(name.lower())


def approx(a, b, tol=0.02):
    return abs(a - b) <= tol


print("=== A. company -> org resolution (static file) ===")
cm = CareerMap(client=FakeClient())
orgs = cm.resolve_company_orgs(["Google", "Stripe", "microsoft"])
print(f"  Google/Stripe/microsoft -> {sorted(orgs)[:6]} ...")
assert "google" in orgs and "stripe" in orgs and "microsoft" in orgs

print("=== B. unknown company -> API fallback + cache ===")
# Point the cache file at a temp path so we don't pollute the repo.
tmp = Path(tempfile.mkdtemp())
career_map.COMPANY_CACHE_FILE = tmp / "cache.json"
fake = FakeClient(lookup={"acmecorp": "acme-corp-gh"})
cm2 = CareerMap(client=fake)
cm2.resolved_cache = {}
orgs2 = cm2.resolve_company_orgs(["AcmeCorp"])
print(f"  AcmeCorp -> {orgs2}, api_calls={fake.search_calls}")
assert "acme-corp-gh" in orgs2 and fake.search_calls == 1
# Second call should hit the in-memory cache, no new API call.
cm2.resolve_company_orgs(["AcmeCorp"])
assert fake.search_calls == 1, "fallback not cached"
print(f"  repeat lookup api_calls still {fake.search_calls} (cached)")
# And it was written to disk.
assert career_map.COMPANY_CACHE_FILE.exists()

print("=== C. domain_match ===")
cm3 = CareerMap(client=FakeClient())
# Strong ML repo: ML topics + python + flagship org.
ml = cm3.domain_match(
    repo_topics=["machine-learning", "deep-learning", "transformers"],
    repo_language="python", repo_org="huggingface", career_goal="ml")
print(f"  strong ML repo -> {ml:.2f}")
assert ml >= 0.9
# Unrelated repo for an ML goal.
none = cm3.domain_match(repo_topics=["cooking", "recipes"],
                        repo_language="ruby", repo_org="someone",
                        career_goal="ml")
print(f"  unrelated repo (ML goal) -> {none:.2f}")
assert none == 0.0
# Backend repo, partial match (topics + language, not flagship).
be = cm3.domain_match(repo_topics=["api", "rest"], repo_language="go",
                      repo_org="randomorg", career_goal="backend")
print(f"  backend api/rest/go (non-flagship) -> {be:.2f}")
assert 0.4 < be < 0.9

print("=== D. prestige ===")
# At very high stars the log scale saturates to 1.0 for both, so the
# flagship boost is only visible below saturation. Test it at 2k stars.
p_flag = cm3.prestige(2000, "pytorch", "ml")
p_plain = cm3.prestige(2000, "randomorg", "ml")
p_small = cm3.prestige(120, "randomorg", "ml")
print(f"  2k* flagship={p_flag:.2f}  2k* plain={p_plain:.2f}  "
      f"120* plain={p_small:.2f}")
assert p_flag > p_plain > p_small
assert p_flag <= 1.0
# Sanity: a huge repo still maxes out.
assert cm3.prestige(80000, "randomorg", "ml") == 1.0

print("=== E. career_signal (whole repo) ===")
target = cm3.resolve_company_orgs(["huggingface"])
sig = cm3.career_signal(
    "huggingface/transformers",
    repo_topics=["machine-learning", "nlp", "transformers"],
    repo_language="python", stars=160000,
    target_orgs=target, career_goal="ml")
print(f"  {sig}")
assert sig["is_target_company_org"] is True
assert sig["domain_match"] >= 0.9
assert sig["prestige"] >= 0.9

print("=== F. enrich_issue_with_career ===")
issue = {
    "repo_full_name": "pallets/flask",
    "repo_topics": ["python", "web-framework", "api"],
    "repo_language": "python",
    "repo_health": {"stars": 67000},
    "from_target_org": False,
}
target_be = cm3.resolve_company_orgs(["pallets"])  # not in file -> fallback empty
enrich_issue_with_career(issue, cm3, target_orgs=set(), career_goal="backend")
print(f"  flask career signal -> {issue['career']}")
assert issue["career"]["domain_match"] > 0.5  # api + python + flagship pallets

# from_target_org tag should force is_target even with empty target set.
issue2 = {"repo_full_name": "stripe/stripe-python", "repo_topics": ["api"],
          "repo_language": "python", "repo_health": {"stars": 2000},
          "from_target_org": True}
enrich_issue_with_career(issue2, cm3, target_orgs=set(), career_goal="backend")
assert issue2["career"]["is_target_company_org"] is True
print(f"  stripe issue (from_target_org tag) -> "
      f"is_target={issue2['career']['is_target_company_org']}")

print("\nAll component-3 unit + mocked tests passed.")