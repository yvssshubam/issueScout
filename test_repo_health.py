"""Tests for repo_health.py (component 2).

Two layers:
  A. Pure signal functions, exercised with hand-picked inputs. No network.
  B. Full enrich_issue_with_health against a FakeClient returning canned
     API payloads, so the assembly + caching logic is verified without
     spending GitHub quota. On your machine you can also run the LIVE
     section at the bottom (it uses your real token).
"""

from datetime import datetime, timezone, timedelta

import repo_health as rh
from repo_health import (
    liveness_score, issue_balance_score, popularity_score, issue_size,
    maintainer_responsiveness, enrich_issue_with_health,
)


def approx(a, b, tol=0.02):
    return abs(a - b) <= tol


# ----------------------------- A. pure functions ----------------------- #

print("=== A. liveness_score ===")
assert liveness_score(2) == 1.0          # pushed 2 days ago -> fresh
assert liveness_score(None) == 0.5       # unknown -> neutral
assert liveness_score(400) == 0.0        # over a year -> dead
mid = liveness_score(186)                # ~halfway
print(f"  fresh=1.0 dead=0.0 unknown=0.5 mid(186d)={mid:.2f}")
assert 0.4 < mid < 0.6

print("=== B. issue_balance_score ===")
assert issue_balance_score(0, 0) == 0.5            # no data -> neutral
assert approx(issue_balance_score(20, 80), 0.8)    # 80% closed
assert approx(issue_balance_score(50, 50), 0.5)
print(f"  20/80 -> {issue_balance_score(20,80):.2f}, "
      f"90/10 -> {issue_balance_score(90,10):.2f}")

print("=== C. popularity_score ===")
p_small, p_mid, p_big = (popularity_score(50), popularity_score(2000),
                         popularity_score(150000))
print(f"  50*={p_small:.2f}  2k*={p_mid:.2f}  150k*={p_big:.2f}")
assert p_small < p_mid < p_big
assert p_big <= 1.0 and popularity_score(0) == 0.0

print("=== D. issue_size ===")
b1, s1 = issue_size(body_length=200, checklist_items=0, linked_pr=False)
b2, s2 = issue_size(body_length=1200, checklist_items=4, linked_pr=False)
b3, s3 = issue_size(body_length=3500, checklist_items=9, linked_pr=True)
print(f"  short -> {b1} ({s1:.2f})")
print(f"  medium -> {b2} ({s2:.2f})")
print(f"  large -> {b3} ({s3:.2f})")
assert b1 == "small" and b3 == "large"
assert s1 > s2 > s3

print("=== E. maintainer_responsiveness ===")
now = datetime.now(timezone.utc)
recent = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
old = (now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
comments = [
    {"author_association": "NONE", "user": {"login": "randomdev"},
     "created_at": recent},
    {"author_association": "MEMBER", "user": {"login": "maint1"},
     "created_at": recent},
    {"author_association": "OWNER", "user": {"login": "boss"},
     "created_at": old},
]
responsive, days = maintainer_responsiveness(comments, repo_owner="boss")
print(f"  responsive={responsive} days_since_maint={days:.0f}")
assert responsive is True and days < 5

only_old = [{"author_association": "OWNER", "user": {"login": "boss"},
             "created_at": old}]
resp2, days2 = maintainer_responsiveness(only_old, repo_owner="boss")
print(f"  only-old: responsive={resp2} days={days2:.0f}")
assert resp2 is False


# ----------------------- B. mocked-client assembly --------------------- #

class FakeClient:
    """Stands in for GitHubClient, returning canned payloads + call counts."""
    def __init__(self):
        self.repo_calls = 0
        self.comment_calls = 0
        self.search_calls = 0

    def get_repo(self, name):
        self.repo_calls += 1
        return {
            "full_name": name,
            "stargazers_count": 4200,
            "pushed_at": (datetime.now(timezone.utc) - timedelta(days=2))
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open_issues_count": 120,
            "owner": {"login": "acme"},
        }

    def get_repo_issue_comments(self, name, per_page=30):
        self.comment_calls += 1
        recent = (datetime.now(timezone.utc) - timedelta(days=1)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        return [{"author_association": "MEMBER",
                 "user": {"login": "dev"}, "created_at": recent}]

    def _request(self, url, params=None):
        self.search_calls += 1
        return {"total_count": 480}   # closed issues


print("\n=== F. enrich_issue_with_health (mocked) ===")
fake = FakeClient()
cache = {}
issues = [
    {"repo_full_name": "acme/widgets", "body_length": 300,
     "checklist_items": 0, "linked_pr": False, "title": "Fix typo"},
    {"repo_full_name": "acme/widgets", "body_length": 2500,
     "checklist_items": 8, "linked_pr": True, "title": "Refactor core"},
]
for iss in issues:
    enrich_issue_with_health(iss, fake, cache)

h = issues[0]["repo_health"]
print(f"  repo ok={h['ok']} stars={h['stars']} "
      f"days_push={h['days_since_push']:.0f} closed={h['closed_issues']}")
print(f"  scores: liveness={h['scores']['liveness']:.2f} "
      f"balance={h['scores']['issue_balance']:.2f} "
      f"pop={h['scores']['popularity']:.2f} "
      f"resp={h['scores']['responsiveness']:.2f}")
print(f"  issue1 size={issues[0]['size_bucket']} "
      f"issue2 size={issues[1]['size_bucket']}")

# Caching: both issues share one repo -> exactly ONE set of API calls.
print(f"  api calls: repo={fake.repo_calls} comments={fake.comment_calls} "
      f"search={fake.search_calls}")
assert fake.repo_calls == 1, "repo health not cached across issues!"
assert h["ok"] and h["scores"]["responsiveness"] == 1.0
assert issues[0]["size_bucket"] == "small"
assert issues[1]["size_bucket"] == "large"

# Failure path: a repo that errors should yield neutral, not crash.
class BrokenClient(FakeClient):
    def get_repo(self, name):
        from github_client import GitHubClientError
        raise GitHubClientError("404 gone")

bad = {"repo_full_name": "ghost/repo", "body_length": 100,
       "checklist_items": 0, "linked_pr": False}
enrich_issue_with_health(bad, BrokenClient(), {})
print(f"  broken repo -> ok={bad['repo_health']['ok']} "
      f"(neutral scores, no crash)")
assert bad["repo_health"]["ok"] is False

print("\nAll component-2 mocked + unit tests passed.")