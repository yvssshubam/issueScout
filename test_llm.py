"""Tests for llm.py (component 6).

No real model is contacted. Tests the templated fallbacks, the grounding
guard that rejects invented factors, and parse_interests in both regex and
(mocked) model modes.
"""

import os
import llm
from llm import (
    templated_explanation, get_explanation, parse_interests,
    _explanation_is_grounded, _regex_parse_interests, model_available,
)


# A representative scored issue (as rank_issues would produce).
issue = {
    "title": "Add docs for the config loader",
    "repo_full_name": "acme/widgets",
    "score": 82.0,
    "breakdown": {"career_relevance": 22.0, "skill_fit": 18.0,
                  "approachability": 14.0, "repo_health": 6.0,
                  "freshness": 3.0, "time_fit": 2.0},
    "career": {"is_target_company_org": True},
    "created_at": "2026-06-01T00:00:00Z", "assignee": None,
}


print("=== A. backend selection ===")
os.environ["LLM_BACKEND"] = "none"
print(f"  LLM_BACKEND=none -> model_available={model_available()}")
assert model_available() is False
os.environ["LLM_BACKEND"] = "ollama"
print(f"  LLM_BACKEND=ollama -> model_available={model_available()}")
assert model_available() is True
os.environ["LLM_BACKEND"] = "gemini"   # no key set
print(f"  LLM_BACKEND=gemini (no key) -> model_available={model_available()}")
assert model_available() is False
os.environ["LLM_BACKEND"] = "none"


print("\n=== B. templated_explanation cites only top factors ===")
exp = templated_explanation(issue)
print(f"  {exp}")
assert "Target-company match" in exp
assert llm.FACTOR_PHRASES["career_relevance"] in exp
assert llm.FACTOR_PHRASES["skill_fit"] in exp
# A bottom factor (time_fit at 2.0, not top-3) must not appear.
assert llm.FACTOR_PHRASES["time_fit"] not in exp


print("\n=== C. get_explanation falls back to template with no model ===")
# LLM_BACKEND=none -> model_available False -> templated.
g = get_explanation(issue)
print(f"  {g}")
assert g == templated_explanation(issue)


print("\n=== D. grounding guard rejects invented factors ===")
allowed = ["career_relevance", "skill_fit", "approachability"]
good = "Strong match: fits your target company and your Python skills."
# Mentions 'maintainer' (repo_health) which is NOT in allowed -> reject.
invented = "Great because the maintainer is responsive and it fits your skills."
# Mentions 'hours' (time_fit) not allowed -> reject.
invented2 = "Nice small task that fits your weekly hours nicely."
print(f"  grounded(good)={_explanation_is_grounded(good, allowed)}")
print(f"  grounded(invented maintainer)="
      f"{_explanation_is_grounded(invented, allowed)}")
print(f"  grounded(invented hours)="
      f"{_explanation_is_grounded(invented2, allowed)}")
assert _explanation_is_grounded(good, allowed) is True
assert _explanation_is_grounded(invented, allowed) is False
assert _explanation_is_grounded(invented2, allowed) is False
# Oversized output rejected.
assert _explanation_is_grounded("x" * 400, allowed) is False


print("\n=== E. get_explanation uses model output when grounded (mocked) ===")
def fake_chat_good(messages, **kw):
    return '"Strong match: this fits your target company and Python skills."'
llm._chat = fake_chat_good
out = get_explanation(issue, use_model=True)
print(f"  model output used: {out}")
assert "target company" in out.lower() and out.count('"') == 0  # quotes stripped

print("=== F. get_explanation rejects ungrounded model output (mocked) ===")
def fake_chat_bad(messages, **kw):
    # Invents 'maintainer responsiveness' though repo_health isn't top-3.
    return "This repo's maintainers are super responsive and active!"
llm._chat = fake_chat_bad
out2 = get_explanation(issue, use_model=True)
print(f"  fell back to template: {out2}")
assert out2 == templated_explanation(issue)


print("\n=== G. parse_interests regex fallback ===")
prof = {"stack": {"Python": "strong"}, "topics": ["api"],
        "preferred_types": ["tests"]}
parsed = _regex_parse_interests(
    "I love building web apps and ML models in Go and TypeScript, "
    "want to write docs", prof)
print(f"  {parsed}")
assert "go" in parsed["languages"] and "typescript" in parsed["languages"]
assert "python" in parsed["languages"]            # from profile
assert "web" in parsed["topics"] and "ml" in parsed["topics"]
assert "api" in parsed["topics"]                  # from profile
assert "docs" in parsed["contribution_types"]
assert "tests" in parsed["contribution_types"]    # from profile


print("=== H. parse_interests with mocked model output ===")
def fake_chat_json(messages, **kw):
    return ('```json\n{"languages":["rust"],"topics":["devtools","cli"],'
            '"contribution_types":["bug fix"]}\n```')
llm._chat = fake_chat_json
os.environ["LLM_BACKEND"] = "ollama"   # make model_available True
merged = parse_interests("I want to build CLI tools in Rust", prof,
                         use_model=True)
print(f"  {merged}")
# Model-added rust + profile python both present; junk-free.
assert "rust" in merged["languages"] and "python" in merged["languages"]
assert "devtools" in merged["topics"] and "cli" in merged["topics"]
assert "bug fix" in merged["contribution_types"]
os.environ["LLM_BACKEND"] = "none"


print("\nAll component-6 tests passed.")