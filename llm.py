"""
llm.py — IssueScout component 6.

The ONLY place a language model touches the app. Two narrow jobs:

  1. parse_interests(free_text, profile) — turn the user's free-text
     interests into structured search filters (languages, topics,
     contribution types). Pure helper; the scorer never sees the model.

  2. get_explanation(issue) — write ONE friendly sentence describing why
     an already-scored issue fits, citing ONLY the factors present in the
     score breakdown my code produced (architectural rule 4).

Both go through one interface selected by env var LLM_BACKEND:
  * "ollama" (default, local)  — OpenAI-compatible at http://localhost:11434/v1
  * "gemini" (deploy)          — Google's OpenAI-compatible endpoint
  * "none"/unset/unreachable   — templated fallback, no model

Switching backends is just a base URL + model name. If the model is
removed or unreachable, the app still returns ranked results with plain
templated explanations. The model NEVER decides ranking or order.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import requests

from scorer import top_factors

# --------------------------------------------------------------------- #
# Backend configuration. All OpenAI-compatible /chat/completions.
# --------------------------------------------------------------------- #

def _backend() -> str:
    return os.environ.get("LLM_BACKEND", "ollama").strip().lower()


def _backend_config() -> Optional[dict]:
    """Return base_url/model/api_key for the active backend, or None."""
    backend = _backend()
    if backend == "ollama":
        return {
            "base_url": os.environ.get("OLLAMA_BASE_URL",
                                       "http://localhost:11434/v1"),
            "model": os.environ.get("OLLAMA_MODEL", "llama3.2"),
            "api_key": "ollama",  # ollama ignores the key but the field is required
        }
    if backend == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "")
        return {
            "base_url": os.environ.get(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta/openai"),
            "model": os.environ.get("GEMINI_MODEL", "gemini-1.5-flash"),
            "api_key": key,
        }
    return None  # "none" / unset-to-none / unknown -> templated fallback


def model_available() -> bool:
    """True if a backend is configured (does not ping the server)."""
    cfg = _backend_config()
    if cfg is None:
        return False
    if _backend() == "gemini" and not cfg["api_key"]:
        return False
    return True


def _chat(messages: list[dict], max_tokens: int = 200,
          temperature: float = 0.3, timeout: int = 20) -> Optional[str]:
    """
    One OpenAI-compatible chat call. Returns the assistant text, or None
    on any failure (so callers fall back to templates rather than error).
    """
    cfg = _backend_config()
    if cfg is None:
        return None
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None


# --------------------------------------------------------------------- #
# Templated explanation (canonical model-free fallback). The pipeline
# imports these. Cites ONLY factors present in the breakdown.
# --------------------------------------------------------------------- #

FACTOR_PHRASES = {
    "career_relevance": "lines up with your target companies and career goal",
    "skill_fit": "matches the skills you already have",
    "approachability": "looks beginner-friendly and well-described",
    "repo_health": "is an active, well-maintained repo",
    "freshness": "looks unclaimed and still open to take on",
    "time_fit": "is sized to fit your weekly hours",
}


def _freshness_phrase(issue: dict) -> str:
    from scorer import _days_since
    days = _days_since(issue.get("created_at"))
    unclaimed = issue.get("assignee") in (None, "")
    if days is not None and days <= 30 and unclaimed:
        return "was opened recently and looks unclaimed"
    if unclaimed:
        return "looks unclaimed and still open to take on"
    return "is still open"


def _top_factor_names(issue: dict, n: int = 3) -> list[str]:
    breakdown = issue.get("breakdown", {})
    return [f for f, pts in top_factors(breakdown, n=n) if pts > 0]


def templated_explanation(issue: dict) -> str:
    """One plain sentence citing only the real top factors for this issue."""
    breakdown = issue.get("breakdown", {})
    if not breakdown:
        return "Ranked by overall fit."

    factor_names = _top_factor_names(issue, n=3)
    if not factor_names:
        return "A modest overall match across the board."

    phrases = []
    for f in factor_names:
        if f == "freshness":
            phrases.append(_freshness_phrase(issue))
        else:
            phrases.append(FACTOR_PHRASES.get(f, f))

    is_target = issue.get("career", {}).get("is_target_company_org")
    lead = "Strong match" if issue.get("score", 0) >= 60 else "Possible match"
    if is_target:
        lead = "Target-company match"

    if len(phrases) == 1:
        body = phrases[0]
    elif len(phrases) == 2:
        body = f"{phrases[0]} and {phrases[1]}"
    else:
        body = f"{phrases[0]}, {phrases[1]}, and {phrases[2]}"
    return f"{lead}: this issue {body}."


# --------------------------------------------------------------------- #
# Job 2: model-written explanation (with grounding guard + fallback).
# --------------------------------------------------------------------- #

# Maps a factor key to words the model may legitimately use for it, so we
# can detect (and reject) an explanation that invents an absent factor.
_FACTOR_KEYWORDS = {
    "career_relevance": ["career", "company", "target", "resume", "domain",
                         "prestige", "goal"],
    "skill_fit": ["skill", "language", "stack", "python", "javascript",
                  "experience with", "know"],
    "approachability": ["beginner", "friendly", "approachable", "described",
                        "good first", "newcomer", "documented"],
    "repo_health": ["active", "maintained", "healthy", "responsive",
                    "maintainer", "well-kept", "alive"],
    "freshness": ["recent", "unclaimed", "newly", "fresh", "open", "just opened"],
    "time_fit": ["time", "hours", "small", "scoped", "sized", "quick", "weekly"],
}


def _explanation_is_grounded(text: str, allowed_factors: list[str]) -> bool:
    """
    Reject an explanation that clearly references a factor NOT in the
    allowed (top) set. Conservative: only fails on an obvious foreign
    factor, otherwise trusts the model. Empty/oversized -> not grounded.
    """
    if not text or len(text) > 320:
        return False
    lowered = text.lower()
    foreign = set(_FACTOR_KEYWORDS) - set(allowed_factors)
    for factor in foreign:
        # Distinctive keywords only (skip generic ones shared across factors).
        distinctive = {"career", "company", "target", "resume", "prestige",
                       "beginner", "maintainer", "unclaimed", "hours"}
        for kw in _FACTOR_KEYWORDS[factor]:
            if kw in distinctive and kw in lowered:
                return False
    return True


def get_explanation(issue: dict, use_model: Optional[bool] = None) -> str:
    """
    One friendly sentence on why this issue fits. Uses the model when
    available and grounded; otherwise the templated fallback. The model
    is given ONLY the real top factors and forbidden from inventing more.
    """
    if use_model is None:
        use_model = model_available()
    if not use_model:
        return templated_explanation(issue)

    factor_names = _top_factor_names(issue, n=3)
    if not factor_names:
        return templated_explanation(issue)

    breakdown = issue.get("breakdown", {})
    facts = {f: breakdown.get(f, 0) for f in factor_names}
    is_target = bool(issue.get("career", {}).get("is_target_company_org"))

    system = (
        "You explain why a GitHub issue is a good contribution match. "
        "Write exactly ONE friendly sentence, under 30 words. You may ONLY "
        "mention the scoring factors provided. Do NOT invent any reason that "
        "is not in the factor list. Be concrete and encouraging."
    )
    user = (
        f"Issue title: {issue.get('title', '')}\n"
        f"Repo: {issue.get('repo_full_name', '')}\n"
        f"Target-company repo: {is_target}\n"
        f"Top scoring factors (the ONLY things you may cite): "
        f"{json.dumps(facts)}\n"
        f"Factor meanings: career_relevance=fits target company/career goal; "
        f"skill_fit=matches user's known languages; "
        f"approachability=beginner-friendly; repo_health=active/maintained; "
        f"freshness=recent and unclaimed; time_fit=fits weekly hours.\n"
        f"Write the one-sentence explanation."
    )
    out = _chat([{"role": "system", "content": system},
                 {"role": "user", "content": user}], max_tokens=80)

    if out and _explanation_is_grounded(out, factor_names):
        # Strip surrounding quotes a model sometimes adds.
        return out.strip().strip('"').strip()
    return templated_explanation(issue)


# --------------------------------------------------------------------- #
# Job 1: parse free-text interests into structured search filters.
# --------------------------------------------------------------------- #

# Known languages/topics for the regex fallback and for validating model
# output. Not exhaustive; the model handles the long tail when available.
_KNOWN_LANGUAGES = {
    "python", "javascript", "typescript", "java", "go", "golang", "rust",
    "c", "c++", "c#", "ruby", "php", "swift", "kotlin", "scala", "dart",
    "r", "elixir", "haskell", "html", "css", "shell", "lua", "zig",
}
_LANG_ALIASES = {"golang": "go", "js": "javascript", "ts": "typescript",
                 "py": "python", "cpp": "c++", "csharp": "c#"}

_KNOWN_TOPICS = {
    "web", "api", "ml", "machine-learning", "deep-learning", "data",
    "data-science", "devtools", "devops", "security", "frontend", "backend",
    "mobile", "cli", "database", "cloud", "kubernetes", "docker", "nlp",
    "computer-vision", "blockchain", "game", "gamedev", "testing",
}

_CONTRIB_TYPES = {"docs", "tests", "bug fix", "small feature"}


def _regex_parse_interests(free_text: str, profile: dict) -> dict:
    """Model-free extraction of filters from free text + existing profile."""
    text = (free_text or "").lower()

    langs = set()
    for word in re.findall(r"[a-z0-9+#]+", text):
        canon = _LANG_ALIASES.get(word, word)
        if canon in _KNOWN_LANGUAGES:
            langs.add(_LANG_ALIASES.get(canon, canon))
    # Fold in languages already in the profile stack.
    for l in profile.get("stack", {}):
        langs.add(l.lower())

    topics = set()
    for t in _KNOWN_TOPICS:
        if t.replace("-", " ") in text or t in text:
            topics.add(t)
    for t in profile.get("topics", []):
        topics.add(t.lower())

    contrib = set()
    for c in _CONTRIB_TYPES:
        if c.split()[0] in text:
            contrib.add(c)
    for c in profile.get("preferred_types", []):
        contrib.add(c.lower())

    return {
        "languages": sorted(langs),
        "topics": sorted(topics),
        "contribution_types": sorted(contrib),
    }


def parse_interests(free_text: str, profile: Optional[dict] = None,
                    use_model: Optional[bool] = None) -> dict:
    """
    Turn free-text interests into {languages, topics, contribution_types}.
    Uses the model when available; always merges/validates against the
    known sets and the profile, and falls back to regex if the model is
    unavailable or returns junk.
    """
    profile = profile or {}
    if use_model is None:
        use_model = model_available()

    fallback = _regex_parse_interests(free_text, profile)
    if not use_model or not (free_text or "").strip():
        return fallback

    system = (
        "Extract structured GitHub search filters from a user's free-text "
        "interests. Return ONLY a JSON object with keys: languages (array "
        "of lowercase programming languages), topics (array of lowercase "
        "GitHub topic slugs), contribution_types (subset of "
        '["docs","tests","bug fix","small feature"]). No prose, JSON only.'
    )
    user = f"User interests: {free_text}"
    out = _chat([{"role": "system", "content": system},
                 {"role": "user", "content": user}], max_tokens=200,
                temperature=0.1)
    if not out:
        return fallback

    # Strip code fences if present, then parse.
    cleaned = re.sub(r"^```(?:json)?|```$", "", out.strip(),
                     flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return fallback

    # Validate + merge with fallback so we never lose profile-derived data.
    langs = set(fallback["languages"])
    for l in parsed.get("languages", []):
        c = _LANG_ALIASES.get(str(l).lower(), str(l).lower())
        if c in _KNOWN_LANGUAGES:
            langs.add(c)

    topics = set(fallback["topics"])
    for t in parsed.get("topics", []):
        topics.add(str(t).lower().replace(" ", "-"))

    contrib = set(fallback["contribution_types"])
    for c in parsed.get("contribution_types", []):
        if str(c).lower() in _CONTRIB_TYPES:
            contrib.add(str(c).lower())

    return {
        "languages": sorted(langs),
        "topics": sorted(topics),
        "contribution_types": sorted(contrib),
    }