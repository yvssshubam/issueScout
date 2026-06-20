"""
verify_issuescout.py — verification harness for IssueScout.

Runs a few controlled scenarios through your real pipeline and checks
objective properties of the results, so you can confirm the app returns
the right repos rather than eyeballing cards.

Usage:
    # token must be set in this same terminal
    py -3.14 verify_issuescout.py

It makes TWO live pipeline runs (beginner and professional on the same
profile) plus reuses them for all checks, so it stays light on the search
budget. If you get rate-limited, wait ~2 minutes and rerun; the on-disk
cache makes the second attempt cheap.

Exit code is 0 if all hard checks pass, 1 otherwise.
"""

from __future__ import annotations

import os
import sys
import time
import statistics

from pipeline import run_pipeline

# Tune the test scope here. Keep it small so all three discovery paths fit
# inside one minute of search budget.
LANGS = ["python"]
TOPICS = ["web"]
COMPANY = "microsoft"          # a company you KNOW has good-first-issues
MAX = 10
WEAK_STAR_FLOOR = 200          # below this, a repo is flagged as "weak"


# --------------------------------------------------------------------- #
# Tiny check framework.
# --------------------------------------------------------------------- #

class Report:
    def __init__(self):
        self.rows = []  # (scenario, name, status, detail) status in PASS/FAIL/INFO/SKIP

    def add(self, scenario, name, status, detail=""):
        self.rows.append((scenario, name, status, detail))

    def passed(self, scenario, name, detail=""):
        self.add(scenario, name, "PASS", detail)

    def failed(self, scenario, name, detail=""):
        self.add(scenario, name, "FAIL", detail)

    def info(self, scenario, name, detail=""):
        self.add(scenario, name, "INFO", detail)

    def skip(self, scenario, name, detail=""):
        self.add(scenario, name, "SKIP", detail)

    def print(self):
        print("\n" + "=" * 72)
        print("VERIFICATION REPORT")
        print("=" * 72)
        current = None
        for scen, name, status, detail in self.rows:
            if scen != current:
                print(f"\n{scen}")
                current = scen
            mark = {"PASS": "[PASS]", "FAIL": "[FAIL]",
                    "INFO": "[info]", "SKIP": "[skip]"}[status]
            line = f"  {mark} {name}"
            if detail:
                line += f"  ->  {detail}"
            print(line)
        hard = [r for r in self.rows if r[2] in ("PASS", "FAIL")]
        n_pass = sum(1 for r in hard if r[2] == "PASS")
        print("\n" + "-" * 72)
        print(f"SUMMARY: {n_pass}/{len(hard)} hard checks passed")
        print("-" * 72)
        return all(r[2] != "FAIL" for r in self.rows)


# --------------------------------------------------------------------- #
# Helpers to read fields defensively (keys vary slightly by enrichment).
# --------------------------------------------------------------------- #

def repo_of(issue):
    return issue.get("repo_full_name", "?")

def org_of(issue):
    return repo_of(issue).split("/")[0] if "/" in repo_of(issue) else "?"

def stars_of(issue):
    return issue.get("repo_health", {}).get("stars", 0) or 0

def is_target(issue):
    return bool(issue.get("career", {}).get("is_target_company_org")
                or issue.get("from_target_org"))

def approachability_sub(issue):
    return issue.get("subscores", {}).get("approachability", 0.0)

def skill_sub(issue):
    return issue.get("subscores", {}).get("skill_fit", 0.0)

def key_of(issue):
    return (repo_of(issue), issue.get("number"))


def build_profile(level):
    return {
        "stack": {l: "comfortable" for l in LANGS},
        "topics": TOPICS,
        "college_year": "2nd",
        "experience": "none",
        "level": level,
        "companies": [COMPANY],
        "career_goal": "backend",
        "weekly_hours": 5,
        "preferred_types": ["docs", "tests"],
    }


# --------------------------------------------------------------------- #
# Checks shared by any single result set.
# --------------------------------------------------------------------- #

def check_result_set(rep, scenario, issues):
    if not issues:
        rep.failed(scenario, "returned any results",
                   "0 issues (likely rate-limited; wait and rerun)")
        return
    rep.passed(scenario, "returned results", f"{len(issues)} issues")

    # Scores descend.
    scores = [i.get("score", 0) for i in issues]
    if scores == sorted(scores, reverse=True):
        rep.passed(scenario, "scores are descending",
                   f"{scores[0]:.0f} down to {scores[-1]:.0f}")
    else:
        rep.failed(scenario, "scores are descending", f"{scores}")

    # No PRs, nothing already assigned.
    prs = [repo_of(i) for i in issues if i.get("linked_pr")]
    assigned = [repo_of(i) for i in issues if i.get("assignee")]
    rep.add(scenario, "no pull requests in results",
            "PASS" if not prs else "FAIL", f"{len(prs)} PRs" if prs else "clean")
    rep.add(scenario, "no already-assigned issues",
            "PASS" if not assigned else "FAIL",
            f"{len(assigned)} assigned" if assigned else "clean")

    # Diversity cap: no repo more than twice.
    counts = {}
    for i in issues:
        counts[repo_of(i)] = counts.get(repo_of(i), 0) + 1
    over = {r: c for r, c in counts.items() if c > 2}
    rep.add(scenario, "per-repo cap (<=2 each)",
            "PASS" if not over else "FAIL", str(over) if over else "ok")

    # Informational: which orgs, and any weak repos.
    orgs = sorted({org_of(i) for i in issues})
    rep.info(scenario, "orgs represented", ", ".join(orgs))
    weak = sorted({f"{repo_of(i)}({stars_of(i)}*)"
                   for i in issues if stars_of(i) < WEAK_STAR_FLOOR})
    if weak:
        rep.info(scenario, f"weak repos (<{WEAK_STAR_FLOOR} stars)",
                 ", ".join(weak))


# --------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------- #

def main():
    if not os.environ.get("GITHUB_TOKEN"):
        print("GITHUB_TOKEN is not set in this terminal. Set it first:")
        print('  $env:GITHUB_TOKEN="ghp_your_token"   (PowerShell)')
        sys.exit(1)

    rep = Report()

    print(f"Running beginner scenario ({COMPANY}, {LANGS}, {TOPICS}) ...")
    beginner = run_pipeline(build_profile("beginner"), max_issues=MAX,
                            verbose=False)

    time.sleep(5)  # be gentle on the search budget between runs

    print(f"Running professional scenario (same profile) ...")
    professional = run_pipeline(build_profile("professional"), max_issues=MAX,
                                verbose=False)

    # ---- Scenario 1: beginner result-set sanity ----
    s1 = "1. Beginner run sanity"
    check_result_set(rep, s1, beginner)

    # ---- Scenario 2: target company surfaces and resolves ----
    s2 = "2. Target company (microsoft) surfaces"
    targets = [i for i in beginner if is_target(i)]
    if targets:
        rep.passed(s2, "at least one target-company issue",
                   f"{len(targets)} of {len(beginner)}")
        torgs = sorted({org_of(i) for i in targets})
        rep.info(s2, "target issues resolved to orgs", ", ".join(torgs))
        # microsoft resolves to a known family of orgs; flag anything odd.
        expected_family = {"microsoft", "Azure", "dotnet", "azure"}
        unexpected = [o for o in torgs if o not in expected_family]
        rep.add(s2, "target orgs look like microsoft family",
                "PASS" if not unexpected else "INFO",
                "all in microsoft/Azure/dotnet" if not unexpected
                else f"also saw: {unexpected} (check company_orgs.json)")
    else:
        rep.failed(s2, "at least one target-company issue",
                   "none found (org mapping or rate limit)")

    # ---- Scenario 3: language matching works ----
    s3 = "3. Language matching (python)"
    nontarget = [i for i in beginner if not is_target(i)]
    matched = [i for i in nontarget
               if (i.get("repo_language") or "").lower() in
               [l.lower() for l in LANGS] or skill_sub(i) > 0]
    if nontarget:
        if matched:
            rep.passed(s3, "non-target results match your stack",
                       f"{len(matched)} of {len(nontarget)} match {LANGS}")
        else:
            rep.failed(s3, "non-target results match your stack",
                       f"0 of {len(nontarget)} match {LANGS} "
                       "(language path may have been starved)")
    else:
        rep.skip(s3, "non-target results present",
                 "all results were target-company; widen or rerun")

    # ---- Scenario 4: level selector actually changes results ----
    s4 = "4. Level selector changes output"
    if beginner and professional:
        bset = {key_of(i) for i in beginner}
        pset = {key_of(i) for i in professional}
        if bset != pset:
            overlap = len(bset & pset)
            rep.passed(s4, "beginner vs professional differ",
                       f"{overlap}/{len(bset)} overlap, sets are not identical")
        else:
            rep.failed(s4, "beginner vs professional differ",
                       "identical sets -> 'level' likely not reaching profile")

        # Professional should select less-trivial work: lower mean raw
        # approachability, and not-lower mean stars.
        b_appr = statistics.mean([approachability_sub(i) for i in beginner])
        p_appr = statistics.mean([approachability_sub(i) for i in professional])
        rep.add(s4, "professional favors less-trivial issues",
                "PASS" if p_appr <= b_appr + 1e-9 else "FAIL",
                f"mean approachability beginner={b_appr:.2f} "
                f"professional={p_appr:.2f}")

        b_stars = statistics.median([stars_of(i) for i in beginner])
        p_stars = statistics.median([stars_of(i) for i in professional])
        rep.info(s4, "median repo stars",
                 f"beginner={b_stars:.0f}  professional={p_stars:.0f}")
    else:
        rep.skip(s4, "compare beginner vs professional",
                 "one run returned nothing (rate limit); rerun")

    # ---- Scenario 5: professional run sanity ----
    s5 = "5. Professional run sanity"
    check_result_set(rep, s5, professional)

    ok = rep.print()

    print("\nManual spot-check (do this once by hand):")
    print("  - Click 3 result links and confirm each is a REAL, OPEN, "
          "unassigned issue.")
    print("  - Confirm the target-company cards carry the target badge.")
    print("  - In the UI, flip the level dropdown beginner<->professional "
          "and confirm the list visibly changes (same as check 4).")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()