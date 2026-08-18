"""
worker_job_scout.py — Job posting match-scoring worker for Icarus.

Consumes job-posting tasks from the `tasks:job_scout` Redis stream. Given a
posting (a direct URL, pasted JD text, or a stub {company, title, link}
handed off by another producer — see the payload-shape table below), it
compares the posting's requirements against DIIZZY's resume and GitHub
project history, and produces a match score, missing qualifications, and
concrete tailoring suggestions ("this requirement isn't on your resume, but
project X demonstrates it").

Results are tracked as tracked_items rows with item_type="job_opportunity" —
deliberately separate from item_type="job_application" (worker_email_triage.py),
since these are postings DIIZZY hasn't applied to yet. See
tracked_items_repo.promote_to_application() for the "I actually applied" path.

Payload shapes (distinguished by key presence, no explicit discriminator):
  {"url": "<job posting URL>"}                     — manual add (Discord)
  {"jd_text": "<pasted description>"}              — manual add (Discord)
  {"stub": {"company", "title", "link", "source"}} — digest/repo producers

Unlike worker_email_triage.py, a failed match has no safe default score to
substitute (a fabricated 0.0 would misrepresent a good match as bad) — so
process_task lets failures propagate to WorkerBase's retry/backoff/DLQ
machinery instead of swallowing them.
"""

import os
import re
import json
import logging
from backend.agent.worker_base import WorkerBase
from backend.agent.posting_fetch import fetch_posting_text

logger = logging.getLogger(__name__)

RESUME_PATH = "/workspace/projects/resume.md"

# Same shape/comment convention as worker_email_triage.py's REVIEW_CONFIDENCE
# — a starting point to tune once real scouting volume is visible. Leans
# toward notifying: under-notifying (missing a good opportunity) is the
# worse failure mode here, unlike triage where over-notifying is the one to
# avoid.
NOTIFY_MATCH_SCORE = 0.75

MATCH_PROMPT = """You are evaluating a job posting against a candidate's qualifications. Respond with a JSON object ONLY — no extra text, no markdown fences.

The JSON must contain exactly these fields:
- "company": the hiring company's name — best guess from the posting text if not already known.
- "role": the job title/position — best guess from the posting text if not already known.
- "match_score": a number from 0.0 to 1.0 for how well the candidate's qualifications
  (resume + project evidence below) match this posting's stated requirements.
- "verdict": one of "strong_match", "partial_match", "weak_match" (roughly: >=0.75 strong,
  >=0.45 partial, below that weak — but use judgment, don't just threshold blindly).
- "missing_qualifications": a list of requirements from the posting that the resume and
  project evidence do not support.
- "matched_qualifications": a list of objects {{"requirement": "...", "evidence": "...",
  "evidence_source": "resume" or "project:<name>"}} — cite the SPECIFIC resume bullet or
  project that supports each matched requirement. Prefer resume evidence when a
  requirement is already covered there; use project evidence for anything the resume
  doesn't state.
- "tailoring_suggestions": concrete suggestions for improving the application — especially
  requirements a listed PROJECT demonstrates but the resume doesn't currently mention
  (e.g. "requirement X isn't on your resume, but project Y demonstrates it — consider
  adding a bullet").
- "confidence": 0.0 to 1.0 — how confident you are in this assessment. Lower it when the
  posting text is thin/incomplete (see note below) or the requirements are vague.
- "summary": one or two sentence overall assessment.

Weight project evidence tagged source="resume" as equally reliable as the resume itself
(it's the same text, pulled from a different place). Weight source="description" project
evidence as weaker — it's repo metadata, not a real description of what was built — and
say so explicitly if a tailoring suggestion leans on it.
{thin_input_note}
JOB POSTING:
{posting_text}

RESUME:
{resume_text}

PROJECT EVIDENCE (beyond the resume):
{corpus_block}

Respond with the JSON object only."""


class JobScoutWorker(WorkerBase):
    def __init__(self):
        super().__init__(
            stream_name="tasks:job_scout",
            group_name="job_scout_group",
            max_retries=3,
        )

    def _normalize_key(self, text: str) -> str:
        """Collapse a free-text identifier into a stable-ish entity key —
        same helper as worker_email_triage.py's, duplicated rather than
        shared per this codebase's small-per-module-helper convention."""
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"

    def _read_resume(self) -> str:
        try:
            with open(RESUME_PATH, "r") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"[job_scout] Could not read resume: {e}")
            return "(resume unavailable)"

    def _format_corpus(self, corpus: list[dict]) -> str:
        if not corpus:
            return "(no project evidence available)"
        blocks = [f"### {e['name']} (source={e['source']})\n{e['text']}" for e in corpus]
        return "\n\n".join(blocks)

    async def _resolve_posting_text(self, data: dict) -> tuple[str, dict]:
        """Distinguish the three payload shapes by key presence. Returns
        (posting_text, stub) — stub is {} for the two direct-input shapes,
        since their company/role come back from the LLM's own match output
        instead of needing a second extraction pass."""
        jd_text = data.get("jd_text")
        if jd_text:
            return str(jd_text)[:8000], {}

        url = data.get("url")
        if url:
            text = await fetch_posting_text(url)
            return text, {"link": url}

        stub = data.get("stub") or {}
        link = stub.get("link")
        if link:
            text = await fetch_posting_text(link)
            if not text.startswith("Error fetching posting"):
                return text, stub
            logger.warning(f"[job_scout] Posting fetch failed for {link}: {text}")

        # No usable posting text — fall back to scoring off stub metadata
        # alone, explicitly flagged as thin input so the model doesn't
        # fake confidence it doesn't have.
        thin_lines = [
            "[No full posting text available — scoring from listing metadata only]",
            f"Company: {stub.get('company', 'unknown')}",
            f"Title: {stub.get('title', 'unknown')}",
        ]
        if stub.get("location"):
            thin_lines.append(f"Location: {stub['location']}")
        if stub.get("salary"):
            thin_lines.append(f"Salary: {stub['salary']}")
        return "\n".join(thin_lines) + "\n", stub

    async def _score_match(self, posting_text: str, resume_text: str, corpus: list[dict]) -> dict | None:
        """Call the local LLM to compare the posting against resume+corpus.
        Returns parsed JSON, or None on any failure (parse or otherwise) —
        local_llm.py returns errors as ordinary-looking strings rather than
        raising, so a bare json.loads() would throw on a failed call; this
        degrades the same defensive way worker_email_triage._classify_email
        already does."""
        from backend.agent.llm_router import generate

        thin_input_note = (
            "\nNote: the posting text below is thin listing metadata, not the full job "
            "description — score and flag confidence accordingly.\n"
            if posting_text.startswith("[No full posting text available")
            else ""
        )
        # Verified live against this deployment's actual local LLM: the
        # original 6000/4000/8000 caps built a ~5000-token prompt that blew
        # past LOCAL_LLM_TIMEOUT (120s, shared config — not something to bump
        # for this one worker). Trimmed to fit comfortably instead of
        # touching shared infra. (llm_router.py separately turns thinking
        # OFF for task_type="job_match" now, for the same reason — see its
        # comment; reasoning alone was eating the whole budget on this box.)
        prompt = MATCH_PROMPT.format(
            thin_input_note=thin_input_note,
            posting_text=posting_text[:2500],
            resume_text=resume_text[:4000],  # full resume.md is ~3.1k chars — this cap is a no-op headroom, not active trimming
            corpus_block=self._format_corpus(corpus)[:3000],
        )

        response = ""
        try:
            response = await generate(
                task_type="job_match",
                messages=[{"role": "user", "text": prompt}],
                max_tokens=800,
            )
            cleaned = response.strip()
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"[job_scout] Failed to parse match JSON: {e}\nRaw: {response[:300]}")
            return None
        except Exception as e:
            logger.error(f"[job_scout] Match scoring failed: {e}")
            return None

    def _validate_match(self, raw: dict) -> dict:
        """Ensure the match result has all expected fields with correct types."""
        valid_verdicts = {"strong_match", "partial_match", "weak_match"}

        def _clamp01(value, default=0.0):
            try:
                return max(0.0, min(float(value), 1.0))
            except (TypeError, ValueError):
                return default

        def _str_list(value):
            return [str(v) for v in value][:20] if isinstance(value, list) else []

        match_score = _clamp01(raw.get("match_score"), 0.0)
        verdict = raw.get("verdict")
        if verdict not in valid_verdicts:
            verdict = (
                "strong_match" if match_score >= 0.75 else
                "partial_match" if match_score >= 0.45 else
                "weak_match"
            )

        matched = raw.get("matched_qualifications")
        matched = matched[:20] if isinstance(matched, list) else []

        return {
            "company": str(raw.get("company") or "").strip()[:200] or "Unknown",
            "role": str(raw.get("role") or "").strip()[:200] or "Unknown",
            "match_score": match_score,
            "verdict": verdict,
            "missing_qualifications": _str_list(raw.get("missing_qualifications")),
            "matched_qualifications": matched,
            "tailoring_suggestions": _str_list(raw.get("tailoring_suggestions")),
            "confidence": _clamp01(raw.get("confidence"), 0.0),
            "summary": str(raw.get("summary", ""))[:400],
        }

    async def _track_opportunity(self, match: dict, stub: dict) -> int | None:
        from backend.agent.tracked_items_repo import upsert_item

        entity_key = self._normalize_key(f"{match['company']}-{match['role']}")
        payload = dict(match)
        if stub.get("link"):
            payload["link"] = stub["link"]
        if stub.get("source"):
            payload["posting_source"] = stub["source"]
        # Carried straight through from the producer stub, not re-derived by
        # the LLM — location/salary are listing facts, not something worth
        # spending a match-scoring call re-extracting. Absent for manual
        # url/jd_text dispatches, which have no stub.
        for key in ("location", "salary"):
            if stub.get(key):
                payload[key] = stub[key]

        return await upsert_item(
            platform="discord",
            user_id=os.environ.get("DISCORD_OPERATOR_ID", "0"),
            item_type="job_opportunity",
            entity_key=entity_key,
            state=match["verdict"],
            summary=match.get("summary"),
            payload=payload,
            source="job_scout",
        )

    async def _notify_discord(self, match: dict, stub: dict):
        """Push notification to the Councilor response queue for heartbeat
        delivery — same pattern as worker_email_triage._notify_discord."""
        from backend.database.redis_connection import get_redis_client

        lines = [
            f"Match: {match['match_score']:.0%} ({match['verdict'].replace('_', ' ')})",
            f"Company: {match['company']}",
            f"Role: {match['role']}",
        ]
        if stub.get("link"):
            lines.append(f"Link: {stub['link']}")
        if match.get("missing_qualifications"):
            lines.append("Missing qualifications:")
            lines.extend(f"  - {q}" for q in match["missing_qualifications"][:5])
        if match.get("tailoring_suggestions"):
            lines.append("Tailoring suggestions:")
            lines.extend(f"  - {s}" for s in match["tailoring_suggestions"][:3])

        try:
            redis = get_redis_client()
            response_payload = json.dumps({
                "type": "job_scout_notification",
                "platform": "discord",
                "user_id": os.environ.get("DISCORD_OPERATOR_ID", "0"),
                "message": "\n".join(lines),
            })
            await redis.lpush("icarus:councilor:responses", response_payload)
            logger.info(f"[job_scout] Queued notification for {match['company']} / {match['role']}")
        except Exception as e:
            logger.error(f"[job_scout] Failed to queue notification: {e}")

    async def process_task(self, task_id: str, data: dict):
        """Process a single job-scouting task. Exceptions propagate to
        WorkerBase's retry/backoff/DLQ machinery — a failed match has no
        safe default score to substitute, so a transient failure (network,
        LLM hiccup) should be retried rather than silently swallowed, unlike
        triage which always has a safe default classification to fall back to."""
        from backend.agent.project_corpus import get_cached_project_corpus
        from backend.agent.activity_repo import publish_activity

        posting_text, stub = await self._resolve_posting_text(data)
        resume_text = self._read_resume()
        corpus = await get_cached_project_corpus()

        raw_match = await self._score_match(posting_text, resume_text, corpus)
        if raw_match is None:
            raise RuntimeError(f"Match scoring failed for task {task_id} — see logs above")

        match = self._validate_match(raw_match)
        logger.info(
            f"[job_scout] {match['company']} / {match['role']}: "
            f"score={match['match_score']:.2f} verdict={match['verdict']}"
        )

        item_id = await self._track_opportunity(match, stub)

        await publish_activity(
            actor="job_scout", event_type="scored",
            action="scored job posting",
            detail=f"{match['company']} / {match['role']} → {match['verdict']} ({match['match_score']:.0%})",
            thread_id=f"job_scout-{task_id}",
            severity="info",
        )

        # Store at icarus:email_score:{task_id} — the literal key the
        # existing generic get_worker_result() tool already polls (see
        # orchestrator.py). Reusing it means the manual-add producer needs
        # zero new tool code, just documenting this stream in the system
        # prompt (see engine.py).
        result = {"task_id": task_id, "status": "completed", "item_id": item_id, **match}
        await self.redis.set(f"icarus:email_score:{task_id}", json.dumps(result), ex=86400)

        if match["match_score"] >= NOTIFY_MATCH_SCORE:
            await self._notify_discord(match, stub)


if __name__ == "__main__":
    import sys
    import asyncio
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    worker = JobScoutWorker()
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        worker.stop()
        sys.exit(0)
