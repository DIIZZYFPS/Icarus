"""
posting_fetch.py — Fetches and lightly cleans a job posting page's text.

Sibling to github.py's GitHubClient, same two-layer error-handling shape: the
client method raises on failure (network layer, log-then-reraise), while the
module-level tool-facing function catches and returns an error *string*
instead — worker_job_scout.py treats a fetch failure as "fall back to scoring
off stub metadata alone," not as a crash.

No HTML-boilerplate-stripping library (trafilatura/readability-lxml) was added
for this — a regex-based strip is "good enough": this is a one-off fetch per
scored posting, not a bulk-scraping pipeline, and the match-scoring LLM already
tolerates messy input elsewhere in this codebase (worker_email_triage.py feeds
raw email bodies straight to the model with no cleanup at all).
"""

import re
import logging
import httpx

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 6000

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


class PostingFetchClient:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (compatible; IcarusJobScout/0.1)",
            "Accept": "text/html,application/xhtml+xml",
        }

    async def fetch_html(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            try:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.text
            except httpx.HTTPStatusError as e:
                logger.error(f"Posting fetch HTTP error: {e.response.status_code} - {url}")
                raise Exception(f"Posting fetch HTTP error: {e.response.status_code}")
            except Exception as e:
                logger.error(f"Posting fetch error: {str(e)} - {url}")
                raise Exception(f"Posting fetch error: {str(e)}")


# Singleton instance — same convention as get_github_client().
_client = PostingFetchClient()


def _strip_html(html: str) -> str:
    """Best-effort HTML → plain text. Not a real readability pass — strips
    script/style blocks and tags, collapses whitespace, and leaves the rest
    for the LLM to sift through. Good enough as model input, not for display."""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


async def fetch_posting_text(url: str) -> str:
    """Fetch a job posting URL and return cleaned, truncated text, or an
    error string on failure (never raises — see module docstring)."""
    try:
        html = await _client.fetch_html(url)
    except Exception as e:
        return f"Error fetching posting: {str(e)}"

    text = _strip_html(html)
    if not text:
        return "Error fetching posting: page had no extractable text."
    return text[:MAX_TEXT_CHARS]
