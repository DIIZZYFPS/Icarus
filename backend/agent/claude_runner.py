"""
claude_runner.py — run Claude Code headless for a Councilor `claude_code` task.

Two modes (ADR icarus_desktop_avatar.md §5.5):
  ask   read-only (permission mode `plan`) in the project itself: investigate, explain, propose.
  edit  in a fresh git worktree on branch claude/<task_id>; Claude may edit files and run
        the allowlisted inspection/test commands; changes are committed on that branch and
        left for review. Nothing is pushed or merged.

Progress (tool calls) is reported through `on_progress` as short human labels.
Runs in the Councilor's host process: stdlib + asyncio only.
"""

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

MODES = ("ask", "edit")
DEFAULT_ALLOWED = (
    "Read", "Glob", "Grep", "Edit", "Write", "MultiEdit", "TodoWrite",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)",
    "Bash(python -m unittest:*)", "Bash(python3 -m unittest:*)", "Bash(pytest:*)", "Bash(python -m pytest:*)",
    "Bash(npm test:*)", "Bash(npm run test:*)", "Bash(cargo test:*)", "Bash(go test:*)", "Bash(make test:*)",
)
SYSTEM_NOTE = (
    "You are running headless on behalf of Icarus, DIIZZY's local assistant; nobody can answer "
    "questions mid-run, so make reasonable decisions and state them. Never push, merge, or touch "
    "remotes. Finish with a short plain-text summary (two to five sentences) of what you found or "
    "changed and anything DIIZZY should check; it will be read aloud."
)


def parse_projects(raw: str | None = None) -> dict[str, Path]:
    """DESKTOP_CLAUDE_PROJECTS='icarus=~/Projects/Icarus,site=~/code/site' -> {name: path}."""
    raw = os.getenv("DESKTOP_CLAUDE_PROJECTS", "") if raw is None else raw
    projects = {}
    for part in raw.split(","):
        name, _, path = part.partition("=")
        if name.strip() and path.strip():
            projects[name.strip().lower()] = Path(os.path.expanduser(path.strip()))
    return projects


def describe_tool_use(name: str, args: dict) -> str:
    """'Edit' + {'file_path': '/x/y/engine.py'} -> 'Editing engine.py'."""
    target = args.get("file_path") or args.get("path") or args.get("notebook_path") or ""
    base = Path(str(target)).name if target else ""
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"Editing {base}" if base else "Editing files"
    if name == "Read":
        return f"Reading {base}" if base else "Reading files"
    if name in ("Grep", "Glob"):
        return "Searching the code"
    if name == "Bash":
        cmd = str(args.get("command", "")).strip().split("\n")[0]
        return f"Running {cmd[:40]}" if cmd else "Running a command"
    if name == "TodoWrite":
        return "Planning"
    return name


@dataclass
class ClaudeResult:
    ok: bool
    summary: str
    error: str | None = None
    branch: str | None = None
    worktree: str | None = None
    diffstat: str = ""
    cost_usd: float | None = None
    turns: int | None = None
    elapsed_s: float = 0.0
    artifacts: dict = field(default_factory=dict)


async def _git(cwd: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


def worktree_root() -> Path:
    base = os.getenv("CLAUDE_WORKTREE_ROOT") or os.path.join(
        os.getenv("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "icarus", "claude-worktrees")
    return Path(base)


async def prepare_worktree(project: Path, project_name: str, task_id: str) -> tuple[Path, str]:
    branch = f"claude/{task_id}"
    path = worktree_root() / project_name / task_id
    path.parent.mkdir(parents=True, exist_ok=True)
    code, out = await _git(project, "worktree", "add", "-b", branch, str(path), "HEAD")
    if code != 0:
        raise RuntimeError(f"couldn't create a worktree in {project}: {out[:200]}")
    return path, branch


async def commit_worktree(path: Path, task: str) -> str:
    """Commit whatever Claude changed on its branch; return a diffstat ('' if nothing changed)."""
    await _git(path, "add", "-A")
    code, status = await _git(path, "status", "--porcelain")
    if not status.strip():
        return ""
    message = f"claude: {task.strip().splitlines()[0][:72]}\n\nMade by Claude Code for Icarus (desktop task). Review before merging."
    code, out = await _git(path, "-c", "user.name=Icarus (Claude Code)", "-c", "user.email=icarus@localhost",
                           "commit", "-q", "-m", message)
    if code != 0:
        raise RuntimeError(f"commit failed: {out[:200]}")
    _, stat = await _git(path, "diff", "--stat", "HEAD~1", "HEAD")
    return stat.strip().splitlines()[-1] if stat.strip() else "changes committed"


async def run_claude(
    *,
    task_id: str,
    project_name: str,
    project: Path,
    task: str,
    mode: str,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    timeout_s: float | None = None,
) -> ClaudeResult:
    start = time.monotonic()
    timeout_s = timeout_s or float(os.getenv("CLAUDE_TASK_TIMEOUT_MINUTES", "30")) * 60
    claude = os.getenv("CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not project.is_dir():
        return ClaudeResult(False, "", error=f"project folder {project} doesn't exist")

    cwd, branch = project, None
    if mode == "edit":
        try:
            cwd, branch = await prepare_worktree(project, project_name, task_id)
        except RuntimeError as e:
            return ClaudeResult(False, "", error=str(e))

    allowed = os.getenv("CLAUDE_ALLOWED_TOOLS", "").split() or list(DEFAULT_ALLOWED)
    cmd = [claude, "-p", task, "--output-format", "stream-json", "--verbose",
           "--append-system-prompt", SYSTEM_NOTE,
           "--permission-mode", "acceptEdits" if mode == "edit" else "plan"]
    if mode == "edit":
        cmd += ["--allowedTools", *allowed]

    logger.info(f"[claude {task_id}] {mode} in {cwd}")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024)
    final: dict | None = None
    last_progress = 0.0

    async def read():
        nonlocal final, last_progress
        async for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") == "result":
                final = event
            elif event.get("type") == "assistant" and on_progress:
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use" and time.monotonic() - last_progress > 1.0:
                        last_progress = time.monotonic()
                        try:
                            await on_progress(describe_tool_use(block.get("name", ""), block.get("input") or {}))
                        except Exception:
                            pass

    try:
        await asyncio.wait_for(read(), timeout_s)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ClaudeResult(False, "", error=f"timed out after {int(timeout_s // 60)} min", branch=branch,
                            worktree=str(cwd) if branch else None, elapsed_s=time.monotonic() - start)

    stderr = (await proc.stderr.read()).decode(errors="replace").strip()
    elapsed = time.monotonic() - start
    if not final:
        return ClaudeResult(False, "", error=f"claude exited {proc.returncode} without a result: {stderr[-300:]}",
                            branch=branch, worktree=str(cwd) if branch else None, elapsed_s=elapsed)

    summary = str(final.get("result") or "").strip()
    ok = final.get("subtype") == "success" and not final.get("is_error")
    result = ClaudeResult(ok, summary, error=None if ok else f"claude reported {final.get('subtype')}",
                          branch=branch, worktree=str(cwd) if branch else None,
                          cost_usd=final.get("total_cost_usd"), turns=final.get("num_turns"), elapsed_s=elapsed)
    if branch:
        try:
            result.diffstat = await commit_worktree(cwd, task)
        except RuntimeError as e:
            result.error = (result.error + "; " if result.error else "") + str(e)
        result.artifacts = {"branch": branch, "worktree": str(cwd), "diffstat": result.diffstat or "no changes"}
    return result
