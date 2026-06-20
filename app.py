from __future__ import annotations
import os
import streamlit as st
from pipeline import run_pipeline
from weights import get_weights, stage_for, describe_weights, size_preference
from llm import get_explanation, parse_interests, model_available
import storage
st.set_page_config(page_title="IssueScout", page_icon="🧭", layout="wide")

CAREER_GOALS = ["backend", "frontend", "fullstack", "ml", "data", "devops",
                "mobile", "security", "systems", "devtools"]
COLLEGE_YEARS = ["1st", "2nd", "3rd", "final", "grad"]
EXPERIENCE = ["none", "some", "experienced"]
CONTRIB_TYPES = ["docs", "tests", "bug fix", "small feature"]
LEVELS = ["learning", "comfortable", "strong"]
SKILL_LEVELS = ["beginner", "amateur", "professional"]
COMMON_LANGS = ["python", "javascript", "typescript", "java", "go", "rust",
                "c++", "c#", "ruby", "php", "swift", "kotlin"]
COMMON_TOPICS = ["web", "api", "ml", "data", "devtools", "security",
                 "frontend", "backend", "mobile", "cli", "database", "cloud"]


def collect_profile() -> dict:
    st.sidebar.header("Your profile")

    # Known tech stack with per-language level.
    st.sidebar.subheader("Known tech stack")
    chosen_langs = st.sidebar.multiselect(
        "Languages / frameworks you know", COMMON_LANGS,
        default=["python"])
    extra = st.sidebar.text_input(
        "Other languages (comma-separated)", "")
    for e in [x.strip().lower() for x in extra.split(",") if x.strip()]:
        if e not in chosen_langs:
            chosen_langs.append(e)

    stack = {}
    for lang in chosen_langs:
        lvl = st.sidebar.select_slider(
            f"  {lang} level", LEVELS, value="comfortable",
            key=f"lvl_{lang}")
        stack[lang] = lvl

    topics = st.sidebar.multiselect(
        "Interest topics", COMMON_TOPICS, default=["web", "api"])

    interests_text = st.sidebar.text_area(
        "Describe your interests (free text, optional)",
        placeholder="e.g. I like building developer tools and working on "
                    "APIs, curious about machine learning")

    col1, col2 = st.sidebar.columns(2)
    college_year = col1.selectbox("College year", COLLEGE_YEARS, index=1)
    experience = col2.selectbox("OSS experience", EXPERIENCE, index=0)

    level = st.sidebar.selectbox(
        "Your level (drives ranking)", SKILL_LEVELS, index=0,
        help="beginner favors easy, well-labeled issues; professional "
             "penalizes trivial fixes and weights career relevance.")

    companies_raw = st.sidebar.text_input(
        "Target companies (comma-separated)", "microsoft, vercel")
    companies = [c.strip() for c in companies_raw.split(",") if c.strip()]

    career_goal = st.sidebar.selectbox("Career goal / target role",
                                       CAREER_GOALS, index=0)

    weekly_hours = st.sidebar.slider("Weekly time available (hours)",
                                     1, 40, 5)

    preferred_types = st.sidebar.multiselect(
        "Preferred contribution types", CONTRIB_TYPES,
        default=["docs", "tests"])

    return {
        "stack": stack,
        "topics": topics,
        "interests_text": interests_text,
        "college_year": college_year,
        "experience": experience,
        "companies": companies,
        "career_goal": career_goal,
        "weekly_hours": weekly_hours,
        "preferred_types": preferred_types,
        "level": level,
    }

def render_card(issue: dict, seen_ids: set) -> None:
    title = issue.get("title", "(untitled)")
    repo = issue.get("repo_full_name", "")
    url = issue.get("html_url", "#")
    score = issue.get("score", 0.0)
    is_target = issue.get("career", {}).get("is_target_company_org")
    already_seen = issue.get("id") in seen_ids

    with st.container(border=True):
        top = st.columns([0.7, 0.3])
        with top[0]:
            badge = " 🎯 **Target company**" if is_target else ""
            seen_tag = "  ·  _seen before_" if already_seen else ""
            st.markdown(f"### [{title}]({url}){badge}")
            st.caption(f"{repo}{seen_tag}")
        with top[1]:
            st.metric("Score", f"{score:.0f}")

        st.write(get_explanation(issue))

        with st.expander("Why this score? (raw breakdown)"):
            bd = issue.get("breakdown", {})
            for factor, pts in sorted(bd.items(), key=lambda x: -x[1]):
                st.write(f"**{factor.replace('_', ' ')}**: +{pts}")
            if issue.get("bonus"):
                for k, v in issue["bonus"].items():
                    st.write(f"**bonus ({k.replace('_', ' ')})**: +{v}")
            h = issue.get("repo_health", {})
            if h.get("ok"):
                st.caption(
                    f"repo: {h.get('stars', 0)} stars · "
                    f"language {h.get('language') or 'n/a'} · "
                    f"size {issue.get('size_bucket', 'n/a')}")

def main():
    st.title("🧭 IssueScout")
    st.caption("Find beginner-friendly open-source issues that fit your "
               "skills, stage, and target companies. Ranking is computed by "
               "code; the AI only explains and parses.")

    # Status row.
    cols = st.columns(3)
    token_ok = bool(os.environ.get("GITHUB_TOKEN"))
    cols[0].markdown(("✅" if token_ok else "⚠️") +
                     f" GitHub token: {'set' if token_ok else 'missing'}")
    backend = os.environ.get("LLM_BACKEND", "ollama")
    cols[1].markdown(f"🤖 LLM backend: `{backend}` "
                     f"({'on' if model_available() else 'templated fallback'})")
    cols[2].markdown("📦 Results cached locally")

    profile = collect_profile()

    c1, c2 = st.columns([0.25, 0.75])
    find = c1.button("🔍 Find issues", type="primary", use_container_width=True)
    max_issues = c2.slider("How many results", 5, 25, 10)

    if st.sidebar.button("Clear 'seen' history"):
        storage.clear_seen()
        st.sidebar.success("Cleared.")

    if not find:
        st.info("Fill in your profile on the left, then click **Find issues**.")
        stage = stage_for(profile["college_year"], profile["experience"])
        st.caption(f"Current stage preset: **{stage}** · "
                   f"{describe_weights(get_weights(profile['college_year'], profile['experience']))} · "
                   f"prefers {size_preference(profile['college_year'], profile['experience'])} issues")
        return

    if not token_ok:
        st.error("Set GITHUB_TOKEN in your environment for usable rate "
                 "limits, then restart the app.")
        return

    # If the user wrote free-text interests, parse them into extra filters.
    if profile.get("interests_text", "").strip():
        parsed = parse_interests(profile["interests_text"], profile)
        for l in parsed.get("languages", []):
            profile["stack"].setdefault(l, "learning")
        for t in parsed.get("topics", []):
            if t not in profile["topics"]:
                profile["topics"].append(t)
        for c in parsed.get("contribution_types", []):
            if c not in profile["preferred_types"]:
                profile["preferred_types"].append(c)

    with st.spinner("Searching GitHub and scoring issues..."):
        try:
            ranked = run_pipeline(profile, max_issues=max_issues,
                                  verbose=False)
        except Exception as e:  # noqa: BLE001 - surface errors in UI
            st.error(f"Something went wrong: {e}")
            return

    if not ranked:
        st.warning("No matching issues found. Try broadening your languages "
                   "or topics, or different target companies.")
        return

    seen_ids = storage.get_seen()
    n_target = sum(1 for i in ranked
                   if i.get("career", {}).get("is_target_company_org"))
    st.success(f"Found {len(ranked)} issues · {n_target} from target "
               f"companies.")

    report = profile.get("_collection_report", {})
    if profile.get("companies") and report.get("target_issues", 0) == 0:
        if report.get("errors"):
            st.warning(
                "Couldn't fetch issues from your target companies this time "
                "(GitHub rate limit). Wait a minute and try again. "
                f"Details: {report['errors']}")
        else:
            st.info(
                "Your target companies had no open, unassigned beginner "
                "issues right now. Try different companies, or check back "
                "later.")

    for issue in ranked:
        render_card(issue, seen_ids)

    storage.mark_seen([i["id"] for i in ranked if "id" in i])


if __name__ == "__main__":
    main()