
from __future__ import annotations
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

API_ROOT = "https://api.github.com"

# The GitHub search API never returns more than 1000 results per query.
SEARCH_HARD_CAP = 1000

# Only ride out a SHORT transient throttle. A sustained secondary limit
# asks for ~60s; waiting that long mid-run is worse than failing fast and
# letting the user retry in a minute, so we cap the wait low.
MAX_RATE_LIMIT_SLEEP = 8  # seconds

# How many times to back off and retry a 403/429 before giving up.
MAX_RETRIES = 2

# Spacing between consecutive SEARCH calls only (Core API calls are not
# paced; they have a 5,000/hour budget). Kept modest because the client now
# makes very few search calls per run.
SEARCH_PACE = 1.0  # seconds

# ---------------------------------------------------------------------- #
# Level -> behaviour mapping. This is the heart of change 2.
# ---------------------------------------------------------------------- #

# Beginner labels and the hyphenated variants repos use inconsistently.
GFI_LABELS = ["good first issue", "good-first-issue"]
HELP_LABELS = ["help wanted", "help-wanted"]

# What each level fetches. "unlabeled" means run an extra pass with no
# label qualifier at all (used for professionals, who shouldn't be limited
# to issues someone pre-tagged as easy).
LEVEL_CONFIG: dict[str, dict[str, Any]] = {
    "beginner": {
        "labels": GFI_LABELS,
        "unlabeled": False,
        "min_repo_stars": 25,
        "issue_size": "small",   # steer toward small, well-scoped issues
        "max_comments": 40,      # avoid heavily-contested threads
    },
    "amateur": {
        "labels": GFI_LABELS + HELP_LABELS,
        "unlabeled": False,
        "min_repo_stars": 50,
        "issue_size": "any",
        "max_comments": 60,
    },
    "professional": {
        "labels": HELP_LABELS,
        "unlabeled": True,       # also fetch open issues with no beginner tag
        "min_repo_stars": 300,   # prefer substantial, reputable repos
        "issue_size": "large",   # steer toward meatier work
        "max_comments": 120,
    },
}

DEFAULT_LEVEL = "beginner"


def level_config(level: Optional[str]) -> dict[str, Any]:
    """Return the fetch policy for a level, defaulting safely."""
    return LEVEL_CONFIG.get((level or DEFAULT_LEVEL).lower(), LEVEL_CONFIG[DEFAULT_LEVEL])


class GitHubClientError(RuntimeError):
    """Raised on unrecoverable API problems (e.g. a far-off quota reset)."""


class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        use_cache: bool = True,
        cache_ttl: int = 3600,
        cache_dir: str = ".issuescout_cache",
    ) -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.use_cache = use_cache
        self.cache_ttl = cache_ttl
        self.cache_dir = Path(cache_dir)
        if use_cache:
            self.cache_dir.mkdir(exist_ok=True)

        self.session = requests.Session()
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers.update(headers)

        # Non-fatal problems from the last collect run, for the UI to show.
        self.warnings: list[str] = []
        self._last_search_at = 0.0
        # Circuit breaker: once the search secondary limit is hit, stop
        # making further SEARCH calls this run so we finish fast instead of
        # hammering a throttled API. Core API calls continue normally.
        self._search_blocked = False
        # When True, skip per-call search pacing: a capped thread pool bounds
        # the burst instead. Used only by the parallel collection path.
        self._parallel = False

    # ------------------------------------------------------------------ #
    # Low-level GET with caching, pacing, and fail-fast rate limiting.
    # ------------------------------------------------------------------ #

    def _cache_key(self, url: str, params: dict) -> Path:
        raw = url + json.dumps(params, sort_keys=True)
        digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def _cache_read(self, path: Path) -> Optional[Any]:
        if not (self.use_cache and path.exists()):
            return None
        if time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _cache_write(self, path: Path, data: Any) -> None:
        if not self.use_cache:
            return
        try:
            path.write_text(json.dumps(data))
        except OSError:
            pass

    def _pace_search(self) -> None:
        if self._parallel:
            return
        elapsed = time.time() - self._last_search_at
        if elapsed < SEARCH_PACE:
            time.sleep(SEARCH_PACE - elapsed)
        self._last_search_at = time.time()

    def _request(self, url: str, params: Optional[dict] = None,
                 max_retries: Optional[int] = None) -> Any:
        """GET with cache, rate-limit waits/retries, clear 422 + 5xx handling.

        This is the single rate-limit-aware path. repo_health.py and
        career_map.py call it directly, so its name and signature must stay
        stable. Search endpoints are auto-detected from the URL for pacing.
        """
        params = params or {}
        max_retries = MAX_RETRIES if max_retries is None else max_retries
        is_search = "/search/" in url
        cache_path = self._cache_key(url, params)
        cached = self._cache_read(cache_path)
        if cached is not None:
            return cached

        # Circuit breaker: if we already hit the search limit this run, don't
        # make more search calls. Fail fast so the run finishes in seconds.
        # (Cached search results above are still served; Core calls proceed.)
        if is_search and self._search_blocked:
            raise GitHubClientError(
                "GitHub secondary rate limit hit earlier this run; skipping "
                "further searches. Wait about a minute and retry."
            )

        attempts = 0
        while True:
            if is_search:
                self._pace_search()

            resp = self.session.get(url, params=params, timeout=25)
            status = resp.status_code

            if status in (403, 429):
                remaining = resp.headers.get("X-RateLimit-Remaining")
                retry_after = resp.headers.get("Retry-After")
                reset = resp.headers.get("X-RateLimit-Reset")

                # Work out how long to wait, by which kind of limit this is.
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)                 # secondary limit
                    kind = "secondary"
                elif remaining == "0" and reset:
                    wait = max(0, int(reset) - int(time.time()))  # primary
                    kind = "primary"
                else:
                    wait = 2 ** attempts                    # unknown burst
                    kind = "secondary"

                attempts += 1
                if attempts <= max_retries and wait <= MAX_RATE_LIMIT_SLEEP:
                    time.sleep(wait + 1)
                    continue

                if kind == "primary":
                    mins = round(wait / 60)
                    raise GitHubClientError(
                        f"GitHub rate limit hit. Quota resets in ~{mins} min. "
                        "Set GITHUB_TOKEN for higher limits, or wait."
                    )
                # Trip the breaker so the rest of the run skips searches.
                if is_search:
                    self._search_blocked = True
                raise GitHubClientError(
                    "GitHub secondary rate limit hit (too many search "
                    "requests too quickly). Wait about a minute and retry, "
                    "or narrow your languages / topics / companies."
                )

            if status == 422:
                # Usually a search query for an org/user that doesn't exist.
                try:
                    msg = resp.json().get("message", "")
                except ValueError:
                    msg = ""
                raise GitHubClientError(
                    f"GitHub rejected the query (422): {msg} "
                    f"(often an org or user that doesn't exist). params={params}"
                )

            if status >= 500:
                attempts += 1
                if attempts <= max_retries:
                    time.sleep(2 ** attempts)
                    continue

            resp.raise_for_status()
            data = resp.json()
            self._cache_write(cache_path, data)
            return data

    # Backward-compatible alias for internal callers. Pacing is auto-
    # detected from the URL inside _request.
    def _get(self, url: str, params: Optional[dict] = None,
             is_search: bool = False) -> Any:
        return self._request(url, params)

    # ------------------------------------------------------------------ #
    # Issue normalization (shape the scorer depends on; do not drop keys).
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_issue(raw: dict) -> dict:
        repo_url = raw.get("repository_url", "")
        repo_full_name = "/".join(repo_url.split("/")[-2:]) if repo_url else ""
        body = raw.get("body") or ""
        labels = [l["name"] for l in raw.get("labels", []) if isinstance(l, dict)]
        return {
            "id": raw.get("id"),
            "number": raw.get("number"),
            "title": raw.get("title", ""),
            "html_url": raw.get("html_url", ""),
            "repo_full_name": repo_full_name,
            "labels": labels,
            "comments": raw.get("comments", 0),
            "assignee": (raw.get("assignee") or {}).get("login") if raw.get("assignee") else None,
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
            "body_length": len(body),
            "checklist_items": body.count("- [ ]") + body.count("- [x]"),
            "linked_pr": "pull_request" in raw,
            "from_target_org": False,
            # filled in later by repo_health: language, topics, stars, etc.
        }

    def _search_issues_strict(self, q: str, max_results: int) -> list[dict]:
        """Run one issue-search query, paginate, return normalized issues.
        Raises GitHubClientError on rate limits / bad queries."""
        out: list[dict] = []
        per_page = min(100, max_results)
        page = 1
        while len(out) < max_results and page * per_page <= SEARCH_HARD_CAP:
            data = self._get(
                f"{API_ROOT}/search/issues",
                {"q": q, "per_page": per_page, "page": page},
                is_search=True,
            )
            items = data.get("items", [])
            if not items:
                break
            for raw in items:
                # Exclude PRs that slip through and anything already claimed.
                if "pull_request" in raw or raw.get("assignee"):
                    continue
                out.append(self._normalize_issue(raw))
            if len(items) < per_page:
                break
            page += 1
        return out[:max_results]

    def _search_issues(self, q: str, max_results: int) -> list[dict]:
        """Graceful wrapper: a failed query returns what it has and records
        one warning instead of crashing the run."""
        try:
            return self._search_issues_strict(q, max_results)
        except GitHubClientError as exc:
            if "422" in str(exc):
                msg = f"Skipped a query GitHub rejected: {exc}"
            else:
                msg = f"Search slowed by rate limits: {exc}"
            if msg not in self.warnings:
                self.warnings.append(msg)
            return []

    @staticmethod
    def _label_clause(label: str) -> str:
        return f'label:"{label}"'

    @staticmethod
    def _combined_label_clause(labels: list[str]) -> str:
        # GitHub issue search treats comma-separated label values as OR:
        # label:"good first issue","good-first-issue". One query instead of
        # one per variant, which is the main way we stay under the search
        # budget.
        return "label:" + ",".join(f'"{l}"' for l in labels)

    def _search_issues_labeled(self, base_q: str, labels: list[str],
                               max_results: int) -> list[dict]:
        """One OR-combined label search; falls back to per-label queries if
        GitHub rejects the combined syntax (422). Rate-limit errors degrade
        gracefully (warning + empty result), never crash the run."""
        if not labels:
            return []
        q = f"{base_q} {self._combined_label_clause(labels)}".strip()
        try:
            return self._search_issues_strict(q, max_results)
        except GitHubClientError as exc:
            if "422" not in str(exc):
                msg = f"Search slowed by rate limits: {exc}"
                if msg not in self.warnings:
                    self.warnings.append(msg)
                return []
        # Combined syntax rejected: split into per-label queries (each one
        # already degrades gracefully).
        pool: dict[int, dict] = {}
        for label in labels:
            q = f"{base_q} {self._label_clause(label)}".strip()
            for issue in self._search_issues(q, max_results):
                pool.setdefault(issue["id"], issue)
        return list(pool.values())

    # ------------------------------------------------------------------ #
    # Discovery path 1: issues by language, label net chosen by level.
    # ------------------------------------------------------------------ #

    def search_issues_by_level(
        self,
        languages: Iterable[str],
        level: Optional[str] = None,
        max_per_query: int = 25,
    ) -> list[dict]:
        cfg = level_config(level)
        langs = list(languages) or [None]
        pool: dict[int, dict] = {}

        # Each label variant is its own query, because multiple label
        # qualifiers in one query are ANDed by GitHub and would match
        # nothing.
        for lang in langs:
            lang_clause = f"language:{lang}" if lang else ""
            base_q = f"is:issue is:open no:assignee {lang_clause}".strip()
            # ONE combined OR-label query per language instead of one query
            # per label variant: the main search-budget saving.
            for issue in self._search_issues_labeled(base_q, cfg["labels"],
                                                     max_per_query):
                pool.setdefault(issue["id"], issue)
            # Professionals also get an unlabeled pass: real open work that
            # was never tagged "beginner". Cap comments so we skip
            # bikeshedding threads.
            if cfg["unlabeled"]:
                q = (
                    f"is:issue is:open no:assignee {lang_clause} "
                    f'comments:<{cfg["max_comments"]}'
                ).strip()
                for issue in self._search_issues(q, max_per_query):
                    pool.setdefault(issue["id"], issue)
        return list(pool.values())

    # ------------------------------------------------------------------ #
    # Discovery path 2: repos by topic (star floor scales with level).
    # ------------------------------------------------------------------ #

    def search_repos_by_topics(
        self,
        topics: Iterable[str],
        languages: Iterable[str],
        level: Optional[str] = None,
        max_per_query: int = 5,
    ) -> list[dict]:
        cfg = level_config(level)
        min_stars = cfg["min_repo_stars"]
        langs = list(languages) or [None]
        seen: dict[str, dict] = {}
        for topic in topics:
            topic_slug = topic.strip().lower().replace(" ", "-")
            for lang in langs:
                clauses = [f"topic:{topic_slug}"]
                if lang:
                    clauses.append(f"language:{lang}")
                if min_stars:
                    clauses.append(f"stars:>={min_stars}")
                q = " ".join(clauses)
                try:
                    data = self._get(
                        f"{API_ROOT}/search/repositories",
                        {"q": q, "sort": "stars", "order": "desc",
                         "per_page": max_per_query},
                        is_search=True,
                    )
                except GitHubClientError as exc:
                    msg = f"Search slowed by rate limits: {exc}"
                    if msg not in self.warnings:
                        self.warnings.append(msg)
                    continue
                for repo in data.get("items", []):
                    seen.setdefault(repo["full_name"], repo)
        return list(seen.values())

    def fetch_repo_issues_by_level(
        self,
        repo_full_name: str,
        level: Optional[str] = None,
        max_results: int = 10,
    ) -> list[dict]:
        """Fetch open, unassigned issues for one repo using the CORE API
        (GET /repos/{repo}/issues, 5,000/hour), filtering by the level's
        labels client-side. This avoids the Search API entirely for the
        per-repo path, which was the main rate-limit drain.
        """
        cfg = level_config(level)
        wanted = {l.lower() for l in cfg["labels"]}
        try:
            raw = self._request(
                f"{API_ROOT}/repos/{repo_full_name}/issues",
                {"state": "open", "per_page": 30,
                 "sort": "updated", "direction": "desc"},
            )
        except GitHubClientError as exc:
            msg = f"Could not list issues for {repo_full_name}: {exc}"
            if msg not in self.warnings:
                self.warnings.append(msg)
            return []

        if not isinstance(raw, list):
            return []

        out: list[dict] = []
        for item in raw:
            # The repo issues endpoint includes PRs and assigned issues.
            if "pull_request" in item or item.get("assignee"):
                continue
            labels = {l["name"].lower() for l in item.get("labels", [])
                      if isinstance(l, dict)}
            if (labels & wanted) or cfg["unlabeled"]:
                out.append(self._normalize_issue(item))
            if len(out) >= max_results:
                break
        return out

    # ------------------------------------------------------------------ #
    # Discovery path 3: target-company orgs. THIS is the headline feature
    # and the one that was failing. Fetched first now, with a fallback.
    # ------------------------------------------------------------------ #

    def fetch_org_issues(
        self,
        orgs: Iterable[str],
        level: Optional[str] = None,
        max_per_org: int = 25,
        single_query: bool = False,
    ) -> list[dict]:
        cfg = level_config(level)
        pool: dict[int, dict] = {}
        for org in orgs:
            org = org.strip()
            if not org:
                continue
            got_for_org = 0
            try:
                base_q = f"is:issue is:open no:assignee org:{org}"
                if single_query:
                    # ONE search per org (web fast path): the broad open,
                    # unassigned query. It reliably returns candidates for any
                    # org in a single call; the deterministic scorer then ranks
                    # by level (approachability favors beginner-friendly work),
                    # so we don't need a separate labeled pass here.
                    q = f'{base_q} comments:<{cfg["max_comments"]}'
                    for issue in self._search_issues(q, max_per_org):
                        issue["from_target_org"] = True
                        pool.setdefault(issue["id"], issue)
                    continue
                # ONE combined OR-label query per org (was one per variant).
                for issue in self._search_issues_labeled(base_q,
                                                         cfg["labels"],
                                                         max_per_org):
                    issue["from_target_org"] = True
                    if pool.setdefault(issue["id"], issue) is issue:
                        got_for_org += 1
                # Fallback: if labels found nothing for this org, OR the
                # level wants unlabeled work, pull open unassigned issues
                # directly. A target company that doesn't use the beginner
                # labels still shows up.
                if got_for_org == 0 or cfg["unlabeled"]:
                    q = f'{base_q} comments:<{cfg["max_comments"]}'
                    for issue in self._search_issues(q, max_per_org):
                        issue["from_target_org"] = True
                        pool.setdefault(issue["id"], issue)
            except (GitHubClientError, requests.RequestException) as exc:
                # Do NOT swallow this. Record it so the UI can say why the
                # target companies are missing.
                self.warnings.append(f"Could not fetch issues for org '{org}': {exc}")
        return list(pool.values())

    # ------------------------------------------------------------------ #
    # Company name -> org login fallback.
    # ------------------------------------------------------------------ #

    def search_org(self, company_name: str) -> Optional[str]:
        try:
            data = self._get(
                f"{API_ROOT}/search/users",
                {"q": f"{company_name} type:org", "per_page": 1},
                is_search=True,
            )
        except (GitHubClientError, requests.RequestException):
            return None
        items = data.get("items", [])
        return items[0]["login"] if items else None

    # ------------------------------------------------------------------ #
    # Repo metadata getters (used by repo_health.py).
    # ------------------------------------------------------------------ #

    def get_repo(self, full_name: str) -> dict:
        return self._get(f"{API_ROOT}/repos/{full_name}")

    def get_repo_commits(self, full_name: str, per_page: int = 5) -> list[dict]:
        return self._get(f"{API_ROOT}/repos/{full_name}/commits",
                         {"per_page": per_page})

    def get_repo_issue_comments(self, full_name: str, per_page: int = 30) -> list[dict]:
        return self._get(f"{API_ROOT}/repos/{full_name}/issues/comments",
                         {"per_page": per_page, "sort": "created",
                          "direction": "desc"})


# ---------------------------------------------------------------------- #
# Orchestrator. Order matters: target orgs are gathered FIRST.
# ---------------------------------------------------------------------- #

def collect_candidate_issues(
    languages: Iterable[str],
    topics: Iterable[str],
    target_orgs: Iterable[str] = (),
    level: Optional[str] = None,
    client: Optional[GitHubClient] = None,
    max_per_query: int = 25,
    report: Optional[dict] = None,
    parallel: bool = False,
    include_topics: bool = True,
    org_single_query: bool = False,
) -> list[dict]:
    """
    Gather a deduplicated pool of candidate issues from all three sources,
    scaled to `level`. Target-company issues are fetched first (so they
    don't lose the rate-limit race) and tagged from_target_org=True.

    If a `report` dict is passed, it is filled with per-source counts
    (target_issues / language_issues / topic_issues) and an `errors` list,
    so the caller (pipeline / app) can show the user what happened. Non-
    fatal fetch problems also land on `client.warnings`.

    When `parallel=True`, the independent searches (per org, per language,
    the topic-repo search, then per-repo issue fetches) run in a small,
    capped thread pool with per-call pacing disabled. Wall-clock collapses
    from "sum of paced calls" to roughly two short bursts. The circuit
    breaker still trips on a secondary limit and the run degrades to partial
    results rather than failing. Used by the web server for a fast first
    results moment; CLI/Streamlit keep the serial path.
    """
    client = client or GitHubClient()
    client.warnings = []
    languages = list(languages)
    topics = list(topics)
    target_orgs = list(target_orgs)
    pool: dict[int, dict] = {}
    counts = {"target_issues": 0, "language_issues": 0, "topic_issues": 0}

    if parallel:
        _collect_parallel(client, languages, topics, target_orgs, level,
                          max_per_query, pool, counts, include_topics,
                          org_single_query)
        if not any(i.get("from_target_org") for i in pool.values()) and target_orgs:
            client.warnings.append(
                "No issues found in your target companies right now. They may "
                "have no matching open issues at your level, or the org handles "
                "need checking."
            )
        if report is not None:
            report.update(counts)
            report["errors"] = list(client.warnings)
        return list(pool.values())

    # 1. TARGET ORGS FIRST. This is the fix: the headline feature gets the
    #    freshest slice of the per-minute search budget.
    for issue in client.fetch_org_issues(target_orgs, level=level,
                                         max_per_org=max_per_query,
                                         single_query=org_single_query):
        issue["from_target_org"] = True
        if issue["id"] not in pool:
            counts["target_issues"] += 1
        pool[issue["id"]] = issue

    # 2. Language search at the chosen level.
    for issue in client.search_issues_by_level(languages, level=level,
                                               max_per_query=max_per_query):
        if pool.setdefault(issue["id"], issue) is issue:
            counts["language_issues"] += 1

    # 3. Topic-driven repos, then their issues. (Optional: the web fast path
    #    skips this; it does several searches for results that largely overlap
    #    the target + language passes.)
    if include_topics:
        repos = client.search_repos_by_topics(topics, languages, level=level,
                                              max_per_query=5)
        for repo in repos[:6]:
            for issue in client.fetch_repo_issues_by_level(repo["full_name"],
                                                           level=level,
                                                           max_results=10):
                if pool.setdefault(issue["id"], issue) is issue:
                    counts["topic_issues"] += 1

    if not any(i.get("from_target_org") for i in pool.values()) and target_orgs:
        client.warnings.append(
            "No issues found in your target companies right now. They may "
            "have no matching open issues at your level, or the org handles "
            "need checking."
        )

    if report is not None:
        report.update(counts)
        report["errors"] = list(client.warnings)

    return list(pool.values())


def _collect_parallel(client, languages, topics, target_orgs, level,
                      max_per_query, pool, counts, include_topics=True,
                      org_single_query=False):
    """Fan out the independent searches across a capped pool. Pool mutation
    stays on this (main) thread; workers only make client calls."""
    client._parallel = True
    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            org_futs = {ex.submit(client.fetch_org_issues, [o], level=level,
                                  max_per_org=max_per_query,
                                  single_query=org_single_query): o
                        for o in target_orgs}
            lang_futs = [ex.submit(client.search_issues_by_level, [l],
                                   level=level, max_per_query=max_per_query)
                         for l in languages]
            repo_fut = (ex.submit(client.search_repos_by_topics, topics,
                                  languages, level=level, max_per_query=5)
                        if (topics and include_topics) else None)

            for fut in org_futs:
                try:
                    issues = list(fut.result())
                except Exception:
                    continue
                for issue in issues:
                    issue["from_target_org"] = True
                    if issue["id"] not in pool:
                        counts["target_issues"] += 1
                    pool[issue["id"]] = issue

            for fut in lang_futs:
                try:
                    issues = list(fut.result())
                except Exception:
                    continue
                for issue in issues:
                    if pool.setdefault(issue["id"], issue) is issue:
                        counts["language_issues"] += 1

            repos = []
            if repo_fut is not None:
                try:
                    repos = repo_fut.result() or []
                except Exception:
                    repos = []

            repo_futs = [ex.submit(client.fetch_repo_issues_by_level,
                                   r["full_name"], level=level, max_results=10)
                         for r in repos[:4]]
            for fut in repo_futs:
                try:
                    issues = list(fut.result())
                except Exception:
                    continue
                for issue in issues:
                    if pool.setdefault(issue["id"], issue) is issue:
                        counts["topic_issues"] += 1
    finally:
        client._parallel = False