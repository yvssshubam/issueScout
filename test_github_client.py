"""Smoke test for github_client.py (component 1).

Runs unauthenticated, so it sticks to the search API (10 req/min budget)
and leans on the on-disk cache. With GITHUB_TOKEN set you can also
exercise get_repo / get_repo_commits / get_repo_issue_comments.
"""

import time
from github_client import GitHubClient, GitHubClientError

client = GitHubClient()

print("=== 1. Beginner issues, language=python ===")
issues = client.search_beginner_issues(
    languages=["python"], max_per_query=5, labels=["good first issue"]
)
print(f"got {len(issues)} issues")
for i in issues[:3]:
    print(f"  [{i['repo_full_name']}] #{i['number']} {i['title'][:55]!r} "
          f"comments={i['comments']} assignee={i['assignee']}")
assert issues and all(i["assignee"] is None for i in issues)

print("\n=== 2. Target-org issues (org=microsoft) ===")
org_issues = client.fetch_org_issues(["microsoft"], max_per_org=5,
                                     labels=["good first issue"])
print(f"got {len(org_issues)} issues")
for i in org_issues[:3]:
    print(f"  [{i['repo_full_name']}] #{i['number']} {i['title'][:55]!r}")
assert all(i["repo_full_name"].startswith("microsoft/") for i in org_issues)

print("\n=== 3. Repos by topic (machine-learning, python) ===")
repos = client.search_repos_by_topics(["machine-learning"], ["python"],
                                      max_per_query=3)
for r in repos[:3]:
    print(f"  {r['full_name']} stars={r['stargazers_count']}")
assert repos

print("\n=== 4. Org-login fallback lookup ===")
print(f"  'Stripe' -> {client.search_org('Stripe')}")

print("\n=== 5. Cache hit (same query, should be instant) ===")
t0 = time.time()
client.search_beginner_issues(languages=["python"], max_per_query=5,
                              labels=["good first issue"])
print(f"  returned in {time.time() - t0:.3f}s")
assert time.time() - t0 < 0.5

print("\nAll component-1 smoke tests passed.")