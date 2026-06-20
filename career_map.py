"""
career_map.py — IssueScout component 3.

Turns the user's target companies and career goal into a deterministic
career-relevance signal for each repo. No language model.

Outputs, per repo:
  * is_target_company_org (bool) — repo's owner org is one of the user's
    target companies (resolved via company_orgs.json, with a GitHub org
    search fallback for unknown companies, cached to disk).
  * domain_match (0..1) — how well the repo's topics/language fit the
    user's career goal, using domain_topics.json.
  * prestige (0..1) — reputation proxy from stars (and a small boost for
    being a flagship org in the target domain).

These three feed scorer.py's career_relevance factor.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Optional

from github_client import GitHubClient

DATA_DIR = Path(os.environ.get("ISSUESCOUT_DATA_DIR", "."))
COMPANY_ORGS_FILE = DATA_DIR / "company_orgs.json"
DOMAIN_TOPICS_FILE = DATA_DIR / "domain_topics.json"
COMPANY_CACHE_FILE = DATA_DIR / "company_orgs_cache.json"

PRESTIGE_STAR_SATURATION = 50000  # stars beyond this max out the prestige scale


def _load_json(path: Path, default: Any) -> Any:
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data.pop("_comment", None)
        return data
    except (OSError, json.JSONDecodeError):
        return default


class CareerMap:
    """Resolves companies to orgs and scores repos for career relevance."""

    def __init__(self, client: Optional[GitHubClient] = None):
        self.client = client or GitHubClient()
        self.company_orgs: dict[str, list[str]] = _load_json(
            COMPANY_ORGS_FILE, {}
        )
        self.domain_topics: dict[str, dict] = _load_json(
            DOMAIN_TOPICS_FILE, {}
        )
        self.resolved_cache: dict[str, list[str]] = _load_json(
            COMPANY_CACHE_FILE, {}
        )

    # ------------------------------------------------------------------ #
    # Company -> GitHub org resolution.
    # ------------------------------------------------------------------ #

    def resolve_company_orgs(self, companies: Iterable[str]) -> set[str]:
        """
        Map company names to a set of GitHub org logins (lowercased).
        Order: static company_orgs.json, then on-disk resolved cache, then
        the GitHub org-search API (result cached to disk).
        """
        orgs: set[str] = set()
        dirty = False
        for raw in companies:
            name = (raw or "").strip().lower()
            if not name:
                continue
            if name in self.company_orgs:
                orgs.update(o.lower() for o in self.company_orgs[name])
                continue
            if name in self.resolved_cache:
                orgs.update(o.lower() for o in self.resolved_cache[name])
                continue
            login = self.client.search_org(name)
            resolved = [login.lower()] if login else []
            self.resolved_cache[name] = resolved
            dirty = True
            orgs.update(resolved)
        if dirty:
            self._save_cache()
        return orgs

    def _save_cache(self) -> None:
        try:
            COMPANY_CACHE_FILE.write_text(json.dumps(self.resolved_cache,
                                                     indent=2))
        except OSError:
            pass  # cache is best-effort

    # ------------------------------------------------------------------ #
    # Domain match.
    # ------------------------------------------------------------------ #

    def domain_match(
        self,
        repo_topics: Iterable[str],
        repo_language: Optional[str],
        repo_org: str,
        career_goal: str,
    ) -> float:
        """
        0..1 fit between a repo and the user's career goal.

        Components:
          * topic overlap   (up to 0.6) — fraction of the domain's topics
            the repo carries, capped so 3+ matches saturate.
          * language match  (up to 0.25)
          * flagship org    (0.25 bonus) — repo belongs to a landmark org
            for this domain.
        Clamped to 1.0.
        """
        goal = (career_goal or "").strip().lower()
        spec = self.domain_topics.get(goal)
        if not spec:
            return 0.0

        repo_topics_l = {t.lower() for t in repo_topics}
        domain_topics_l = {t.lower() for t in spec.get("topics", [])}
        overlap = len(repo_topics_l & domain_topics_l)
        # 3 matched topics saturate the topic component.
        topic_score = min(overlap / 3.0, 1.0) * 0.6

        lang = (repo_language or "").strip().lower()
        domain_langs = {l.lower() for l in spec.get("languages", [])}
        lang_score = 0.25 if lang and lang in domain_langs else 0.0

        flagship = {o.lower() for o in spec.get("flagship_orgs", [])}
        flagship_score = 0.25 if repo_org.lower() in flagship else 0.0

        return min(topic_score + lang_score + flagship_score, 1.0)

    # ------------------------------------------------------------------ #
    # Prestige.
    # ------------------------------------------------------------------ #

    def prestige(self, stars: int, repo_org: str, career_goal: str) -> float:
        """
        0..1 reputation proxy. Damped log of stars, with a small boost if
        the repo is a flagship org for the target domain.
        """
        if stars <= 0:
            base = 0.0
        else:
            base = min(math.log1p(stars) / math.log1p(PRESTIGE_STAR_SATURATION),
                       1.0)
        goal = (career_goal or "").strip().lower()
        spec = self.domain_topics.get(goal, {})
        flagship = {o.lower() for o in spec.get("flagship_orgs", [])}
        boost = 0.1 if repo_org.lower() in flagship else 0.0
        return min(base + boost, 1.0)

    # ------------------------------------------------------------------ #
    # Top-level: career signal for one repo.
    # ------------------------------------------------------------------ #

    def career_signal(
        self,
        repo_full_name: str,
        repo_topics: Iterable[str],
        repo_language: Optional[str],
        stars: int,
        target_orgs: set[str],
        career_goal: str,
    ) -> dict[str, Any]:
        """Assemble the three career-relevance outputs for one repo."""
        repo_org = repo_full_name.split("/")[0] if "/" in repo_full_name else ""
        is_target = repo_org.lower() in {o.lower() for o in target_orgs}
        dmatch = self.domain_match(repo_topics, repo_language, repo_org,
                                   career_goal)
        prest = self.prestige(stars, repo_org, career_goal)
        return {
            "is_target_company_org": is_target,
            "domain_match": round(dmatch, 3),
            "prestige": round(prest, 3),
            "repo_org": repo_org,
        }


def enrich_issue_with_career(
    issue: dict,
    career_map: "CareerMap",
    target_orgs: set[str],
    career_goal: str,
    repo_meta: Optional[dict] = None,
) -> dict:
    """
    Attach the career signal to one issue in place.

    repo_meta (optional) supplies repo topics/language/stars if already
    fetched (e.g. from repo_health). Falls back to fields on the issue,
    and to the from_target_org tag set by github_client.collect_*.
    """
    repo_name = issue.get("repo_full_name", "")
    meta = repo_meta or {}
    topics = meta.get("topics", issue.get("repo_topics", []))
    language = meta.get("language", issue.get("repo_language"))
    stars = meta.get("stars",
                     issue.get("repo_health", {}).get("stars", 0))

    signal = career_map.career_signal(
        repo_name, topics, language, stars, target_orgs, career_goal
    )
    # Respect the explicit from_target_org tag if github_client set it.
    if issue.get("from_target_org"):
        signal["is_target_company_org"] = True
    issue["career"] = signal
    return issue