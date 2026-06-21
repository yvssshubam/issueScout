"""
pipeline.py — IssueScout deterministic pipeline (components 1-5),
reconstructed from the build history and now LEVEL-AWARE.

Diff this against your local pipeline.py before replacing. The only real
changes over the version we built are marked CHANGED below:

  * reads profile["level"] (beginner / amateur / professional),
  * passes level into collect_candidate_issues so the right label net and
    repo tier are fetched,
  * ranks via level_integration.rank_issues_leveled, so professionals are
    steered away from trivial issues (approachability is inverted),
  * threads a collection `report` so the caller can show counts/warnings,
  * keeps diversify() for the per-repo cap + guaranteed target-org slots.

Run:
    python pipeline.py --level professional --companies microsoft vercel
    python pipeline.py --level beginner --langs python --topics web api
"""

from __future__ import annotations

import argparse

from github_client import GitHubClient, collect_candidate_issues
from career_map import CareerMap, enrich_issue_with_career
from repo_health import enrich_issue_with_health
from scorer import diversify, top_factors
from level_integration import rank_issues_leveled          # CHANGED
from weights import get_weights, stage_for, describe_weights

LEVELS = ("beginner", "amateur", "professional")

# The templated explainer became the single source of truth in llm.py after
# the refactor. Import it if present; otherwise fall back to a local copy so
# this module runs even if llm.py isn't wired up yet.
try:
    from llm import templated_explanation
except Exception:  # pragma: no cover - fallback only
    FACTOR_PHRASES = {
        "skill_fit": "matches your stack",
        "approachability": "is approachable",
        "repo_health": "sits in a healthy, active repo",
        "freshness": "is fresh and unclaimed",
        "career_relevance": "is relevant to your target companies and goal",
        "time_fit": "fits your available time",
    }

    def templated_explanation(issue: dict) -> str:
        breakdown = issue.get("breakdown", {})
        if not breakdown:
            return "Ranked by overall fit."
        factors = [(f, pts) for f, pts in top_factors(breakdown, n=3) if pts > 0]
        if not factors:
            return "A modest overall match across the board."
        phrases = [FACTOR_PHRASES.get(f, f) for f, _ in factors]
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
# The pipeline.
# --------------------------------------------------------------------- #

def run_pipeline(profile: dict, max_issues: int = 10,
                 verbose: bool = True,
                 check_responsiveness: bool = False,
                 max_per_query: int = 15,
                 candidate_cap: int | None = None,
                 check_issue_balance: bool = True,
                 parallel: bool = False,
                 include_topics: bool = True,
                 org_single_query: bool = False) -> list[dict]:
    client = GitHubClient()
    cmap = CareerMap(client=client)

    langs = list(profile.get("stack", {}).keys())
    topics = profile.get("topics", [])
    companies = profile.get("companies", [])
    level = profile.get("level", "beginner")          # CHANGED

    if verbose:
        stage = stage_for(profile.get("college_year"),
                          profile.get("experience"))
        print(f"Level: {level}  |  stage: {stage}  |  languages={langs}  "
              f"topics={topics}  companies={companies}")

    target_orgs = cmap.resolve_company_orgs(companies)
    if verbose:
        print(f"Target orgs resolved: {sorted(target_orgs) or '(none)'}\n")
        print("Fetching candidate issues from GitHub ...")

    report: dict = {}
    issues = collect_candidate_issues(langs, topics, target_orgs,
                                      level=level,                 # CHANGED
                                      client=client,
                                      max_per_query=max_per_query,
                                      report=report,
                                      parallel=parallel,
                                      include_topics=include_topics,
                                      org_single_query=org_single_query)
    profile["_collection_report"] = report

    # Cap the candidate set before the (expensive) per-repo enrichment so a
    # web request stays responsive. collect_candidate_issues already returns
    # target-org issues first, then language, then topic — so a head slice
    # preserves the most relevant candidates. Off by default (CLI/Streamlit
    # keep full breadth); the web server opts in.
    if candidate_cap and len(issues) > candidate_cap:
        issues = issues[:candidate_cap]
        report["candidate_cap"] = candidate_cap
    if verbose:
        print(f"  collected {len(issues)} candidate issues "
              f"(target={report.get('target_issues', 0)}, "
              f"language={report.get('language_issues', 0)}, "
              f"topic={report.get('topic_issues', 0)})")
        if report.get("errors"):
            print(f"  collection warnings: {report['errors']}")
        print("Enriching with repo health + career signals "
              "(this hits the API per repo) ...")

    health_cache: dict[str, dict] = {}
    # When parallel, pre-warm repo health for the unique repos concurrently.
    # These are Core-API GETs (no search pacing, generous quota), so fetching
    # them at once turns a sequential ~N-RTT loop into one short burst. The
    # loop below then hits the cache instead of the network.
    if parallel and issues:
        from concurrent.futures import ThreadPoolExecutor
        from repo_health import compute_repo_health
        repos = list({i.get("repo_full_name", "") for i in issues
                      if i.get("repo_full_name")})

        def _fetch(rn):
            return rn, compute_repo_health(rn, client, check_responsiveness,
                                           check_issue_balance)
        try:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for rn, h in ex.map(_fetch, repos):
                    health_cache[rn] = h
        except Exception:
            health_cache = {}

    for issue in issues:
        enrich_issue_with_health(issue, client, health_cache,
                                 check_responsiveness=check_responsiveness,
                                 check_issue_balance=check_issue_balance)
        enrich_issue_with_career(issue, cmap, target_orgs,
                                 profile.get("career_goal", ""))

    # Quality floor: drop issues whose repo is below the level's star floor,
    # but never drop a target-company issue (those are wanted regardless).
    from github_client import level_config
    min_stars = level_config(level)["min_repo_stars"]
    if min_stars:
        issues = [i for i in issues
                  if i.get("from_target_org")
                  or (i.get("repo_health", {}).get("stars", 0) or 0) >= min_stars]

    # CHANGED: rank by the explicit level. The year/experience preset is
    # passed as a secondary nudge; blend=1.0 lets level fully drive. Set
    # blend=0.75 if you want college year / experience to still matter.
    stage_weights = get_weights(profile.get("college_year"),
                                profile.get("experience"))
    ranked = rank_issues_leveled(issues, profile, level,
                                 stage_weights=stage_weights, blend=1.0)

    final = diversify(ranked, max_per_repo=2, guarantee_target_slots=3,
                      limit=max_issues)
    return final


# --------------------------------------------------------------------- #
# CLI printing.
# --------------------------------------------------------------------- #

def print_results(ranked: list[dict], desc: str) -> None:
    print(f"\n{'=' * 70}\nTOP {len(ranked)} ISSUES  ({desc})\n{'=' * 70}")
    for i, issue in enumerate(ranked, 1):
        badge = " [TARGET ORG]" if issue.get("career", {}).get(
            "is_target_company_org") else ""
        print(f"\n#{i}  score={issue.get('score', 0):.1f}{badge}")
        print(f"    {issue.get('title', '')[:70]}")
        print(f"    {issue.get('repo_full_name', '')}  "
              f"-> {issue.get('html_url', '')}")
        print(f"    {templated_explanation(issue)}")
        bd = issue.get("breakdown", {})
        bd_str = "  ".join(f"{k}={v}" for k, v in
                           sorted(bd.items(), key=lambda x: -x[1]))
        print(f"    breakdown: {bd_str}")
        if issue.get("bonus"):
            print(f"    bonus: {issue['bonus']}")


def default_profile() -> dict:
    return {
        "stack": {"python": "comfortable", "javascript": "learning"},
        "topics": ["web", "api"],
        "college_year": "2nd",
        "experience": "none",
        "level": "beginner",                          # CHANGED
        "companies": ["microsoft", "vercel"],
        "career_goal": "backend",
        "weekly_hours": 5,
        "preferred_types": ["docs", "tests"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="IssueScout deterministic pipeline")
    ap.add_argument("--year", default="2nd")
    ap.add_argument("--exp", default="none",
                    choices=["none", "some", "experienced"])
    ap.add_argument("--level", default="beginner",    # CHANGED
                    choices=list(LEVELS))
    ap.add_argument("--goal", default="backend")
    ap.add_argument("--langs", nargs="*", default=["python"])
    ap.add_argument("--topics", nargs="*", default=["web", "api"])
    ap.add_argument("--companies", nargs="*", default=["microsoft", "vercel"])
    ap.add_argument("--hours", type=float, default=5)
    ap.add_argument("--max", type=int, default=10)
    args = ap.parse_args()

    profile = {
        "stack": {l: "comfortable" for l in args.langs},
        "topics": args.topics,
        "college_year": args.year,
        "experience": args.exp,
        "level": args.level,                          # CHANGED
        "companies": args.companies,
        "career_goal": args.goal,
        "weekly_hours": args.hours,
        "preferred_types": ["docs", "tests"],
    }

    ranked = run_pipeline(profile, max_issues=args.max)
    print_results(ranked, f"level={args.level}")


if __name__ == "__main__":
    main()