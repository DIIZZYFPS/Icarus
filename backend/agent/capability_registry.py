"""
capability_registry.py — Declared risk tiers for self-modification.

Answers "can this actor do this action unsupervised" as a lookup against
declared paths, not a judgment call the model (or a human skimming a PR) has
to make fresh every time. Consulted by councilor.py once an escalation's
diff is known, to annotate the PR with a risk classification.

This does NOT enable auto-merge. Every escalation still produces a PR for
review, exactly as before — classification is informational today, so
review attention goes where it matters instead of being spent evenly across
a one-line docstring fix and a change to auth handling. Wiring an actual
auto-merge path for the "standard" tier is a separate, larger trust decision
that should be made deliberately, not fall out of adding this file.

Same shape of question ("can this actor do this unsupervised") the original
project plan scoped for DAEX device-capability federation — this table is a
reasonable place for that to plug in later too, without inventing a second
policy system for what is structurally the same lookup.
"""

import fnmatch

# Paths where a change is never "low risk", regardless of how small the diff
# looks or how confident the model sounds — secrets, the sandbox/permission
# logic itself, the tool registry, auth/token handling, the schema layer.
# Patterns are matched with fnmatch against the changed-file path as reported
# by `git status --porcelain` (repo-root-relative).
SENSITIVE_PATH_PATTERNS = [
    ".env", ".env.*",
    "docker-compose.yml",
    "backend/agent/tools.py",                # tool registry — what the agent can even do
    "backend/agent/github.py",               # GitHub auth
    "backend/agent/gmail_tools.py",          # Gmail auth
    "backend/agent/calendar_tools.py",       # Calendar auth
    "backend/agent/worker_base.py",          # retry/DLQ safety logic
    "backend/database/*",                    # schema/connection layer
    "backend/agent/capability_registry.py",  # this file
    "councilor.py",                          # the sandbox/permission logic itself
    ".worktrees/*",                          # sandbox scaffolding — should never be a target
]


def classify_change(changed_files: list[str]) -> tuple[str, list[str]]:
    """Classify a set of changed files by risk tier.

    Returns (tier, matched_paths):
      - tier: "sensitive" if any changed file matches a sensitive pattern,
        else "standard".
      - matched_paths: which changed files triggered it — empty for
        "standard" — for surfacing in the PR body.
    """
    matched = [
        f for f in changed_files
        if any(fnmatch.fnmatch(f, pattern) for pattern in SENSITIVE_PATH_PATTERNS)
    ]
    tier = "sensitive" if matched else "standard"
    return tier, matched
