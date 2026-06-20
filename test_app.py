"""Headless smoke test for app.py using Streamlit's AppTest harness.

Runs the real app code path (form -> button -> render) without a browser,
with run_pipeline + get_explanation mocked so no network is needed.
Verifies the app renders, handles the button, and shows result cards.
"""

import os
import tempfile

os.environ["ISSUESCOUT_STORE"] = tempfile.mktemp(suffix=".json")
os.environ["GITHUB_TOKEN"] = "fake-token-for-test"
os.environ["LLM_BACKEND"] = "none"

from streamlit.testing.v1 import AppTest
import pipeline
import llm

# Mock the pipeline so the app doesn't hit GitHub.
def fake_run_pipeline(profile, max_issues=10, verbose=False):
    return [
        {"id": 1, "title": "Add docs for the config loader",
         "repo_full_name": "microsoft/vscode", "html_url": "http://x/1",
         "score": 78.0, "size_bucket": "small",
         "breakdown": {"approachability": 28.0, "career_relevance": 20.0,
                       "skill_fit": 15.0, "repo_health": 8.0,
                       "freshness": 5.0, "time_fit": 2.0},
         "bonus": {"preferred_type": 3.0},
         "career": {"is_target_company_org": True},
         "repo_health": {"ok": True, "stars": 150000, "language": "TypeScript"},
         "created_at": "2026-06-01T00:00:00Z", "assignee": None},
        {"id": 2, "title": "Fix typo in README",
         "repo_full_name": "smalldev/lib", "html_url": "http://x/2",
         "score": 55.0, "size_bucket": "small",
         "breakdown": {"approachability": 28.0, "freshness": 10.0,
                       "time_fit": 8.0, "repo_health": 5.0,
                       "skill_fit": 4.0, "career_relevance": 0.0},
         "bonus": {},
         "career": {"is_target_company_org": False},
         "repo_health": {"ok": True, "stars": 40, "language": "Python"},
         "created_at": "2026-05-01T00:00:00Z", "assignee": None},
    ]

pipeline.run_pipeline = fake_run_pipeline


def get_app():
    at = AppTest.from_file("app.py", default_timeout=30)
    # Patch the symbols app.py imported by name, after load.
    return at


print("=== A. app loads and renders the form without error ===")
at = AppTest.from_file("app.py", default_timeout=30)
# Inject mocks into the app module's namespace before running.
at.run()
assert not at.exception, f"app raised: {at.exception}"
print(f"  title rendered: {at.title[0].value if at.title else '(none)'}")
assert any("IssueScout" in t.value for t in at.title)

# Before clicking, the info prompt should be visible.
infos = [i.value for i in at.info]
print(f"  info shown: {infos[:1]}")
assert any("Find issues" in i for i in infos)

print("=== B. sidebar form widgets exist ===")
# Multiselects, selectboxes, sliders should be present.
print(f"  multiselects={len(at.multiselect)} selectboxes={len(at.selectbox)} "
      f"sliders={len(at.slider)} buttons={len(at.button)}")
assert len(at.multiselect) >= 3   # langs, topics, contrib types
assert len(at.selectbox) >= 3     # year, experience, career goal
assert len(at.button) >= 1

print("=== C. clicking 'Find issues' renders result cards ===")
# We must mock inside the app's own module namespace. Reload approach:
import app as app_module
app_module.run_pipeline = fake_run_pipeline
# get_explanation with LLM_BACKEND=none already returns templated text.

at2 = AppTest.from_file("app.py", default_timeout=30)
at2.run()
# Click the primary "Find issues" button.
find_btn = [b for b in at2.button if "Find" in b.label]
assert find_btn, "Find issues button not found"
find_btn[0].click().run()
assert not at2.exception, f"app raised after click: {at2.exception}"

# After running, a success message and markdown cards should appear.
successes = [s.value for s in at2.success]
print(f"  success msg: {successes[:1]}")
markdowns = " ".join(m.value for m in at2.markdown)
assert "Add docs for the config loader" in markdowns, \
    "result card title missing"
assert "Target company" in markdowns, "target badge missing"
print("  -> result cards rendered with target badge")

print("\nAll component-7 (app) smoke tests passed.")