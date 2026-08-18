"""
project_corpus.py — Builds the "projects for matching" corpus used by
worker_job_scout.py's MATCH_PROMPT: DIIZZY's GitHub project history, as
evidence beyond what's literally printed on the resume (workspace/projects/resume.md).

Fallback chain per repo, since READMEs vary wildly in quality — never let the
LLM guess what an unfamiliar/undocumented repo does from its name alone:
  1. README.md, if substantive (>= README_MIN_CHARS of real prose) -> source="readme"
  2. repo description + language/topics metadata                  -> source="description"
  3. the resume's own bullet text, for known flagship projects     -> source="resume"
  4. skip entirely

Cached in Redis with a TTL (repo READMEs don't change often) rather than
rebuilt on every match — see get_cached_project_corpus().
"""

import re
import json
import logging

from .github import get_github_client
from .github_tools import github_read_file
from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

# List DIIZZY's repos by username rather than the token-authenticated
# "/user/repos" endpoint — works whether or not GITHUB_TOKEN is configured
# (GitHubClient degrades to read-only-public-repos without one per its own
# docstring) and doesn't silently depend on which account the token happens
# to belong to.
GITHUB_USERNAME = "DIIZZYFPS"

# Case-insensitive *substring* match against the repo name, not equality —
# resume.md links flagship projects to repos whose actual name doesn't match
# the display name verbatim (e.g. "AgentBay" is the display name for repo
# "AgentBay---Tetsy"). Extend this map as new flagship projects are added to
# the resume.
FLAGSHIP_PROJECTS = {
    "saive": "sAIve",
    "agentbay": "AgentBay",
    "wayme": "Wayme",
    "fdms": "FDMS",
}

README_MIN_CHARS = 150
PROJECT_CORPUS_TTL_SECONDS = 7 * 24 * 3600  # repo READMEs don't change often
PROJECT_CORPUS_CACHE_KEY = "icarus:job_scout:project_corpus"

# Fixed constant, not user-supplied input — unlike tools.py's
# WORKSPACE_WRITE_ROOT jail (which validates LLM-supplied relative paths),
# this module only ever reads this one known file, so no escape-checking is
# needed here.
RESUME_PATH = "/workspace/projects/resume.md"


async def _resume_bullets_for(display_name: str) -> str | None:
    """Pull the bullet block for a flagship project straight out of the
    resume itself — a weak/absent README shouldn't shadow prose DIIZZY
    already wrote. Best-effort: a resume.md parsing quirk here degrades to
    None, not a broken corpus build."""
    try:
        with open(RESUME_PATH, "r") as f:
            text = f.read()
    except Exception as e:
        logger.warning(f"[project_corpus] Could not read resume for bullet fallback: {e}")
        return None

    # Match "### <display_name> ... | github.com/..." heading through to the
    # next "### " heading (or end of file).
    pattern = re.compile(
        rf"^###\s+{re.escape(display_name)}\b.*?(?=^###\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None
    block = match.group(0).strip()
    return block if len(block) >= README_MIN_CHARS else None


def _match_flagship(repo_name: str) -> str | None:
    """Case-insensitive substring match against FLAGSHIP_PROJECTS' keys."""
    lowered = repo_name.lower()
    for key, display_name in FLAGSHIP_PROJECTS.items():
        if key in lowered:
            return display_name
    return None


async def _build_entry(repo: dict) -> dict | None:
    """Apply the fallback chain for one repo dict (as returned by
    GitHubClient.list_repos()) and return a corpus entry, or None to skip."""
    full_name = repo.get("full_name") or ""
    if not full_name:
        return None

    owner, _, repo_name = full_name.partition("/")
    name = repo.get("name") or repo_name

    # GitHub's special "profile README" repo (repo name == username) renders
    # as bio text on the profile page — never useful as project evidence.
    if repo_name.lower() == owner.lower():
        return None

    display_name = _match_flagship(repo_name)

    # Skip forks — UNLESS the resume itself names this as a flagship project.
    # Verified against real data: AgentBay (github.com/DIIZZYFPS/AgentBay---Tetsy,
    # the resume's own "not on the resume" example project) is flagged fork=True
    # by GitHub — it started from a hackathon starter template — but it's real,
    # credited work. Being named on the resume is the actual signal of "this is
    # DIIZZY's work," not GitHub's fork flag; a blind fork skip silently drops
    # exactly the kind of project this corpus exists to surface.
    if repo.get("fork") and not display_name:
        return None

    # (a) README.md, if substantive
    for readme_path in ("README.md", "readme.md"):
        content = await github_read_file(owner, repo_name, readme_path)
        if not content.startswith("Error"):
            stripped = content.strip()
            if len(stripped) >= README_MIN_CHARS:
                return {
                    "name": name, "repo_full_name": full_name,
                    # Capped tighter than you'd think (worker_job_scout.py
                    # only budgets 3000 chars for the *whole* corpus block) —
                    # several shorter entries beat one or two truncated-
                    # mid-sentence giants dominating that budget.
                    "source": "readme", "text": stripped[:1000],
                }
            break  # got a file, just too short — don't also try the other casing

    # (b) description + language/topics
    description = (repo.get("description") or "").strip()
    language = repo.get("language")
    topics = repo.get("topics") or []
    if description:
        parts = [description]
        if language:
            parts.append(f"Primary language: {language}.")
        if topics:
            parts.append(f"Topics: {', '.join(topics)}.")
        return {
            "name": name, "repo_full_name": full_name,
            "source": "description", "text": " ".join(parts),
        }

    # (c) resume bullet text, for known flagship projects (display_name
    # computed above, alongside the fork-skip exemption)
    if display_name:
        bullets = await _resume_bullets_for(display_name)
        if bullets:
            return {
                "name": display_name, "repo_full_name": full_name,
                "source": "resume", "text": bullets,
            }

    # (d) nothing substantive to say about this repo — never let the LLM
    # guess what it does from the name alone.
    return None


async def build_project_corpus() -> list[dict]:
    """Fetch DIIZZY's repos and build a corpus entry for each one worth
    including. Hits the GitHub API directly, uncached — callers should go
    through get_cached_project_corpus() instead."""
    client = get_github_client()
    try:
        repos = await client.list_repos(user=GITHUB_USERNAME)
    except Exception as e:
        logger.error(f"[project_corpus] Failed to list repos: {e}")
        return []

    entries = []
    for repo in repos:
        try:
            entry = await _build_entry(repo)
        except Exception as e:
            logger.warning(f"[project_corpus] Skipping {repo.get('full_name')}: {e}")
            entry = None
        if entry:
            entries.append(entry)
    return entries


async def get_cached_project_corpus(force_refresh: bool = False) -> list[dict]:
    """Redis-cached corpus, refreshed lazily on read once the TTL expires —
    repo READMEs don't change often enough to justify a dedicated periodic
    loop for this alone. (Revisit if Phase 3's GitHub-repo-polling loop ends
    up needing a periodic task anyway — cheap to fold this in at that point.)"""
    redis = get_redis_client()
    if not force_refresh:
        cached = await redis.get(PROJECT_CORPUS_CACHE_KEY)
        if cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                logger.warning("[project_corpus] Cached corpus was not valid JSON — rebuilding.")

    corpus = await build_project_corpus()
    try:
        await redis.set(PROJECT_CORPUS_CACHE_KEY, json.dumps(corpus), ex=PROJECT_CORPUS_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"[project_corpus] Failed to cache corpus: {e}")
    return corpus
