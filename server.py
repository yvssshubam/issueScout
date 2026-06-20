"""
server.py — web host for the IssueScout onboarding flow.

Serves the static onboarding wizard in web/ and exposes a single JSON
endpoint, POST /api/match, that translates the onboarding answers into the
profile shape run_pipeline expects, runs the REAL deterministic pipeline
(live GitHub issues + repo-health + scoring + LLM/templated explanation),
and returns ranked matches the frontend renders into the design's results
screens.

No third-party web framework: built on the stdlib http.server so it runs
with nothing more than the project's existing dependencies.

Run:
    python server.py            # then open http://localhost:8000
    python server.py --port 9000
"""

from __future__ import annotations

import argparse
import collections
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
ENV_FILE = ROOT / "env.issuescout"


# --------------------------------------------------------------------- #
# Environment: load env.issuescout (GITHUB_TOKEN, LLM backend, ...) into
# os.environ before the pipeline reads them. Real shell vars win.
# --------------------------------------------------------------------- #

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


load_env(ENV_FILE)

# Imported after env is loaded so any import-time config picks it up.
from pipeline import run_pipeline          # noqa: E402
from llm import get_explanation            # noqa: E402
from github_client import GitHubClient     # noqa: E402
from repo_health import maintainer_responsiveness  # noqa: E402


def _sample_responsiveness(issues: list) -> None:
    """Maintainer responsiveness costs a per-repo call, so we skip it during
    the big candidate loop and sample it here only for the issues we show.
    Deduped by repo and fetched concurrently (Core API, no search pacing);
    best-effort — failures leave the graceful 'recently active' fallback."""
    if not issues:
        return
    try:
        client = GitHubClient()
    except Exception:
        return
    repos = []
    seen = set()
    for issue in issues:
        r = issue.get("repo_full_name", "")
        if r and r not in seen:
            seen.add(r)
            repos.append(r)

    def fetch(repo):
        owner = repo.split("/")[0]
        try:
            comments = client.get_repo_issue_comments(repo, per_page=30)
            return repo, maintainer_responsiveness(comments, owner)
        except Exception:
            return repo, (False, None)

    result = {}
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for repo, rv in ex.map(fetch, repos):
                result[repo] = rv
    except Exception:
        for repo in repos:
            result[repo] = (False, None)

    for issue in issues:
        is_resp, days = result.get(issue.get("repo_full_name", ""), (False, None))
        h = issue.setdefault("repo_health", {})
        h["is_responsive"] = is_resp
        h["days_since_maintainer"] = days


# --------------------------------------------------------------------- #
# Onboarding answers  ->  pipeline profile.
# --------------------------------------------------------------------- #

# The fork is the whole design: career stage drives the weight preset.
STAGE_PRESET = {
    "early": {"college_year": "2nd", "experience": "none", "level": "beginner",
              "weekly_hours": 5},
    "late": {"college_year": "final", "experience": "experienced",
             "level": "professional", "weekly_hours": 12},
}

# Task-size / substance choice -> preferred contribution types.
TASK_TO_TYPES = {
    "docs": ["docs"],
    "bug": ["bug fix"],
    "feature": ["small feature"],
    "hard": ["small feature", "bug fix"],
}

CAREER_GOALS = {"backend", "frontend", "fullstack", "ml", "data", "devops",
                "mobile", "security", "systems", "devtools"}


def build_profile(ans: dict) -> dict:
    stage = "late" if ans.get("stage") == "late" else "early"
    preset = STAGE_PRESET[stage]

    # Caps: every extra language / topic / target company adds rate-limited
    # GitHub Search calls. Beyond a handful, the per-minute search budget is
    # exhausted and the run stalls on retries and returns little. Bounding the
    # inputs keeps a request fast and reliable; the first few of each dominate
    # relevance anyway.
    MAX_LANGS, MAX_TOPICS, MAX_TARGETS = 4, 5, 5

    langs = [str(l).strip().lower() for l in (ans.get("langs") or []) if str(l).strip()][:MAX_LANGS]
    stack = {l: ("comfortable" if stage == "late" else "learning") for l in langs}

    # Interest topics: chosen suggestion chips + free text (free text gets
    # refined by llm.parse_interests inside the pipeline path below).
    topics = [str(t).strip().lower() for t in (ans.get("topics") or []) if str(t).strip()][:MAX_TOPICS]

    # Bias the search toward the chosen contribution path. For non-code paths
    # (design, docs, translation, accessibility, community) this is often the
    # main relevance signal, since no programming language is provided.
    contrib = str(ans.get("contrib") or "").strip().lower()
    ctopic = {"docs": "documentation", "design": "design",
              "i18n": "translation", "a11y": "accessibility",
              "community": "community", "devops": "devops",
              "data": "data"}.get(contrib)
    if ctopic and ctopic not in topics:
        topics = ([ctopic] + topics)[:MAX_TOPICS]

    task = ans.get("taskSize")
    preferred_types = TASK_TO_TYPES.get(task, ["docs", "tests"] if stage == "early"
                                        else ["small feature", "bug fix"])

    companies = [str(c).strip() for c in (ans.get("targets") or []) if str(c).strip()][:MAX_TARGETS]

    # Infer a career goal from chosen topics; harmless default otherwise.
    goal = "fullstack"
    for t in topics:
        key = t.replace(" ", "").replace("&", "")
        if key in CAREER_GOALS:
            goal = key
            break
        if "front" in t:
            goal = "frontend"; break
        if "back" in t or "api" in t:
            goal = "backend"; break
        if "ml" in t or "machine" in t or "data" in t:
            goal = "data"; break
        if "devops" in t or "infra" in t or "cloud" in t:
            goal = "devops"; break

    return {
        "stack": stack,
        "topics": topics,
        "interests_text": (ans.get("interests") or "").strip(),
        "college_year": preset["college_year"],
        "experience": preset["experience"],
        "level": preset["level"],
        "companies": companies,
        "career_goal": goal,
        "weekly_hours": preset["weekly_hours"],
        "preferred_types": preferred_types,
    }


# --------------------------------------------------------------------- #
# Pipeline issue dict  ->  the shape the results screen renders.
# --------------------------------------------------------------------- #

def _resp_dots(days):
    if days is None:
        return 2
    if days <= 2:
        return 4
    if days <= 5:
        return 3
    if days <= 14:
        return 2
    return 1


def _clamp_dots(x):
    return max(1, min(4, int(round(x * 4))))


def shape_issue(issue: dict, stage: str) -> dict:
    health = issue.get("repo_health", {}) or {}
    scores = health.get("scores", {}) or {}
    sub = issue.get("subscores", {}) or {}
    repo = issue.get("repo_full_name", "")
    is_target = bool(issue.get("career", {}).get("is_target_company_org"))
    org = repo.split("/")[0] if "/" in repo else repo

    moss, ochre = "#2D5C4C", "#92662C"
    accent = ochre if stage == "late" else moss

    # ---- first-class signal: responsiveness (real, when sampled) ----
    days_maint = health.get("days_since_maintainer")
    is_responsive = health.get("is_responsive")
    if days_maint is not None:
        days = int(round(days_maint))
        resp_dots = _resp_dots(days)
        if stage == "late":
            resp_blurb = f"Replies in ~{days} day{'s' if days != 1 else ''} — a high bar, but high signal."
        else:
            resp_blurb = f"Maintainers reply to newcomers in ~{days} day{'s' if days != 1 else ''}."
        resp_days = days
    else:
        # Not sampled / unknown: fall back to liveness, no invented day count.
        resp_dots = _clamp_dots(scores.get("liveness", 0.5))
        resp_blurb = ("Recently active — pushes land regularly."
                      if scores.get("liveness", 0) >= 0.7
                      else "A steady, maintained project.")
        resp_days = None

    # ---- first-class signal: documentation (derived from real health) ----
    docs_raw = (scores.get("liveness", 0.5) + scores.get("issue_balance", 0.5)) / 2
    docs_dots = _clamp_dots(docs_raw)
    docs_blurb = ("Setup docs are clear and well-organised."
                  if docs_raw >= 0.7 else "Contributor docs are workable.")

    # ---- fit breakdown bars (real subscores) ----
    def bar(label, key, color):
        return {"label": label, "pct": int(round(_clamp01(sub.get(key, 0.0)) * 100)),
                "color": color}

    if stage == "late":
        bars = [bar("career fit", "career_relevance", ochre),
                bar("skill", "skill_fit", moss),
                bar("repo health", "repo_health", moss)]
    else:
        bars = [bar("skill", "skill_fit", moss),
                bar("approachable", "approachability", moss),
                bar("repo health", "repo_health", moss)]

    stars = health.get("stars", 0) or 0
    if stars >= 1000:
        health_blurb = f"A very active, well-known project ({stars:,}★)."
    elif stars > 0:
        health_blurb = f"A maintained project ({stars:,}★)."
    else:
        health_blurb = "A maintained project."

    size = issue.get("size_bucket", "medium")
    diff_label = {"small": "good first issue", "medium": "intermediate",
                  "large": "substantial"}.get(size, size)

    try:
        # Templated (deterministic, instant) for the web list: calling the
        # LLM once per issue serially is the main latency on machines where a
        # backend is configured. The deterministic factors still drive it.
        reason = get_explanation(issue, use_model=False)
    except Exception:
        reason = "Ranked by overall fit."

    return {
        "repo": repo,
        "org": org,
        "number": issue.get("number"),
        "title": issue.get("title", "(untitled)"),
        "url": issue.get("html_url", "#"),
        "guide": f"https://github.com/{repo}/contribute" if repo else "#",
        "score": int(round(issue.get("score", 0))),
        "diffLabel": diff_label,
        "size": size,
        "isTarget": is_target,
        "reason": reason,
        "accent": accent,
        "responsiveness": {"dots": resp_dots, "days": resp_days,
                           "blurb": resp_blurb, "responsive": bool(is_responsive)},
        "documentation": {"dots": docs_dots, "blurb": docs_blurb},
        "bars": bars,
        "healthBlurb": health_blurb,
    }


def _clamp01(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def do_match(ans: dict) -> dict:
    stage = "late" if ans.get("stage") == "late" else "early"
    profile = build_profile(ans)

    if not os.environ.get("GITHUB_TOKEN"):
        return {"ok": False, "error": "no_token",
                "message": "GITHUB_TOKEN is not set. Add it to env.issuescout."}

    # Free-text interests -> structured filters (mirrors app.py behaviour).
    if profile.get("interests_text"):
        try:
            from llm import parse_interests
            parsed = parse_interests(profile["interests_text"], profile)
            for l in parsed.get("languages", []):
                profile["stack"].setdefault(l, "learning")
            for t in parsed.get("topics", []):
                if t not in profile["topics"]:
                    profile["topics"].append(t)
            for c in parsed.get("contribution_types", []):
                if c not in profile["preferred_types"]:
                    profile["preferred_types"].append(c)
        except Exception:
            pass

    # Tight breadth keeps the onboarding "first results moment" responsive.
    # Each candidate repo costs a closed-issue Search call (for issue_balance);
    # the responsiveness Search is the other big cost, so we turn it OFF for
    # the whole candidate loop and sample it afterwards only for the issues we
    # actually show (_sample_responsiveness). Net: far fewer rate-limited
    # Search calls, so results come back in a few seconds instead of stalling.
    import time as _time
    _t0 = _time.time()
    # Code paths supply languages, so the target + language searches cover it
    # and we skip the slower topic phase. Non-code paths (design/docs/etc.)
    # supply no language, so the topic phase is their candidate source.
    has_langs = bool(profile.get("stack"))
    ranked = run_pipeline(profile, max_issues=8, verbose=False,
                          check_responsiveness=False, check_issue_balance=False,
                          max_per_query=4, candidate_cap=8, parallel=True,
                          include_topics=(not has_langs), org_single_query=True)
    _t1 = _time.time()
    _sample_responsiveness(ranked)
    _t2 = _time.time()
    print(f"· match timing: search+score {_t1-_t0:.1f}s  "
          f"responsiveness {_t2-_t1:.1f}s  total {_t2-_t0:.1f}s "
          f"(issues={len(ranked)})", file=sys.stderr, flush=True)

    report = profile.get("_collection_report", {}) or {}
    issues = [shape_issue(i, stage) for i in ranked]
    n_target = sum(1 for i in issues if i["isTarget"])
    errs = report.get("errors", []) or []
    note = ""
    if not issues:
        rate = any("rate limit" in str(e).lower() or "secondary" in str(e).lower()
                   for e in errs)
        note = ("GitHub's search rate limit was hit this run. Wait about a "
                "minute and try again, or use fewer languages / topics / "
                "target companies.") if rate else (
                "No open, unassigned issues matched at your level right now. "
                "Try broadening your languages or topics, or different targets.")
    return {
        "ok": True,
        "stage": stage,
        "count": len(issues),
        "targetCount": n_target,
        "elapsedMs": int((_time.time() - _t0) * 1000),
        "note": note,
        "issues": issues,
        "report": {"target": report.get("target_issues", 0),
                   "language": report.get("language_issues", 0),
                   "topic": report.get("topic_issues", 0),
                   "errors": errs},
        "profile": {"langs": list(profile["stack"].keys()),
                    "companies": profile["companies"],
                    "level": profile["level"]},
    }


# --------------------------------------------------------------------- #
# HTTP handler.
# --------------------------------------------------------------------- #

# --------------------------------------------------------------------- #
# Request guards: a public deploy must not let /api/match be hammered into
# burning the GitHub / LLM quota, and must reject oversized or malformed
# bodies before the pipeline runs. Both are cheap, in-process, and need no
# extra dependency.
# --------------------------------------------------------------------- #
MAX_BODY_BYTES = 16 * 1024          # reject request bodies larger than 16 KB
RATE_MAX = 20                       # max /api/match calls ...
RATE_WINDOW = 60.0                  # ... per this many seconds, per client IP

_rate_lock = threading.Lock()
_rate_hits: "collections.defaultdict[str, collections.deque]" = \
    collections.defaultdict(collections.deque)


def _rate_ok(ip: str) -> tuple[bool, int]:
    """Sliding-window limiter. Returns (allowed, retry_after_seconds)."""
    now = time.time()
    with _rate_lock:
        dq = _rate_hits[ip]
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_MAX:
            return False, int(RATE_WINDOW - (now - dq[0])) + 1
        dq.append(now)
        if len(_rate_hits) > 4096:  # bound memory: drop idle IPs
            for k in [k for k, v in list(_rate_hits.items()) if not v]:
                _rate_hits.pop(k, None)
        return True, 0


def _sanitize_ans(ans: dict) -> dict:
    """Whitelist keys and bound sizes before anything reaches the pipeline.
    The pipeline applies its own relevance caps (4/5/5) on top of this; these
    are defensive ceilings against abusive input, not product limits."""
    def clean_list(v, maxlen=25, slen=80):
        if not isinstance(v, list):
            return []
        out = []
        for x in v[:maxlen]:
            if isinstance(x, (str, int, float)):
                s = str(x)[:slen].strip()
                if s:
                    out.append(s)
        return out
    stage = ans.get("stage")
    return {
        "stage": stage if stage in ("early", "late") else "early",
        "langs": clean_list(ans.get("langs")),
        "topics": clean_list(ans.get("topics")),
        "interests": str(ans.get("interests") or "")[:500],
        "taskSize": str(ans.get("taskSize") or "")[:40],
        "targets": clean_list(ans.get("targets")),
        "contrib": str(ans.get("contrib") or "")[:40],
        "skills": clean_list(ans.get("skills")),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "IssueScout/1.0"

    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("· " + (fmt % args) + "\n")

    # ---- static files ----
    def _send(self, status, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path):
        rel = path.lstrip("/")
        if rel == "" or rel == "/":
            rel = "index.html"
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR)) or not target.is_file():
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return
        ctype, _ = mimetypes.guess_type(str(target))
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                   "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"ok": True, "token": bool(os.environ.get("GITHUB_TOKEN"))})
            return
        self._serve_static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/match":
            self._send(404, {"ok": False, "message": "unknown endpoint"})
            return
        # Per-IP rate limit so a public deploy can't be hammered.
        ip = self.client_address[0] if self.client_address else "?"
        ok, retry = _rate_ok(ip)
        if not ok:
            self._send(429, {"ok": False,
                             "message": f"Too many requests. Try again in ~{retry}s."})
            return
        # Reject oversized bodies before reading them.
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "message": "Request too large."})
            return
        try:
            raw = self.rfile.read(length) if length else b"{}"
            ans = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"ok": False, "message": "invalid JSON"})
            return
        if not isinstance(ans, dict):
            self._send(400, {"ok": False, "message": "invalid request"})
            return
        ans = _sanitize_ans(ans)
        try:
            result = do_match(ans)
            self._send(200, result)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"ok": False, "error": "pipeline",
                             "message": f"Something went wrong: {e}"})


def main():
    ap = argparse.ArgumentParser(description="IssueScout onboarding web server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    token = "set" if os.environ.get("GITHUB_TOKEN") else "MISSING"
    print(f"IssueScout onboarding running at http://{args.host}:{args.port}")
    print(f"  GitHub token: {token}  ·  LLM backend: {os.environ.get('LLM_BACKEND', 'ollama')}")
    print("  Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
        httpd.shutdown()


if __name__ == "__main__":
    main()