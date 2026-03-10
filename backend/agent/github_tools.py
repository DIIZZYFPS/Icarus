import logging
import base64
from typing import Optional, List, Dict, Any
from .github import get_github_client

logger = logging.getLogger(__name__)

PROTECTED_BRANCHES = {"main", "master"}

async def github_get_repo_info(owner: str, repo: str) -> str:
    """Fetches metadata about a specific GitHub repository.
    Returns details like description, stars, fork count, etc."""
    client = get_github_client()
    try:
        data = await client.get_repo(owner, repo)
        return f"Repo: {data['full_name']}\nDescription: {data['description']}\nStars: {data['stargazers_count']}\nForks: {data['forks_count']}"
    except Exception as e:
        return f"Error fetching repo info: {str(e)}"

async def github_list_repos(user: Optional[str] = None) -> str:
    """Lists repositories for a user (or the authenticated user if user is None)."""
    client = get_github_client()
    try:
        repos = await client.list_repos(user)
        repo_list = "\n".join([f"- {r['full_name']}" for r in repos])
        return f"Repositories:\n{repo_list}"
    except Exception as e:
        return f"Error listing repositories: {str(e)}"

async def github_read_file(owner: str, repo: str, path: str, branch: str = "main") -> str:
    """Reads the content of a file from a GitHub repository.
    Handles base64 decoding automatically."""
    client = get_github_client()
    try:
        data = await client.get_contents(owner, repo, path, branch)
        if isinstance(data, list):
            return f"Error: '{path}' is a directory, not a file."
        if data.get("encoding") == "base64":
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content
        return data.get("content", "Error: No content found.")
    except Exception as e:
        return f"Error reading file from GitHub: {str(e)}"

async def github_list_issues(owner: str, repo: str, state: str = "open") -> str:
    """Lists issues in a GitHub repository."""
    client = get_github_client()
    try:
        issues = await client.list_issues(owner, repo, state)
        if not issues:
            return f"No {state} issues found in {owner}/{repo}."
        issue_list = "\n".join([f"#{i['number']}: {i['title']} ({i['user']['login']})" for i in issues])
        return f"Issues in {owner}/{repo}:\n{issue_list}"
    except Exception as e:
        return f"Error listing issues: {str(e)}"

async def github_read_issue(owner: str, repo: str, issue_number: int, include_comments: bool = True) -> str:
    """Fetches details of a specific GitHub issue, including its body and optionally comments.
    Useful for understanding the context of a bug or feature request."""
    client = get_github_client()
    try:
        issue = await client.get_issue(owner, repo, issue_number)

        issue_user = issue.get("user") or {}
        issue_author = issue_user.get("login", "unknown")
        issue_body = issue.get("body") or "No description provided."

        response = [
            f"Issue #{issue['number']}: {issue['title']}",
            f"State: {issue['state']}",
            f"Author: {issue_author}",
            f"URL: {issue['html_url']}",
            "\n--- Body ---",
            issue_body,
            "--- End Body ---\n"
        ]

        if include_comments:
            comments = await client.get_issue_comments(owner, repo, issue_number)
            if comments:
                response.append(f"Comments ({len(comments)}):")
                for c in comments:
                    comment_user = c.get("user") or {}
                    comment_author = comment_user.get("login", "unknown")
                    comment_body = c.get("body") or ""
                    truncated_body = comment_body[:200]
                    ellipsis = "..." if len(comment_body) > 200 else ""
                    response.append(f"- [{comment_author}]: {truncated_body}{ellipsis}")
            else:
                response.append("No comments found.")
                
        return "\n".join(response)
    except Exception as e:
        return f"Error reading issue: {str(e)}"

async def github_create_issue(owner: str, repo: str, title: str, body: str) -> str:
    """Creates a new issue in a GitHub repository."""
    client = get_github_client()
    try:
        issue = await client.create_issue(owner, repo, title, body)
        return f"Successfully created issue #{issue['number']}: {issue['html_url']}"
    except Exception as e:
        return f"Error creating issue: {str(e)}"

async def github_write_file(owner: str, repo: str, path: str, content: str, message: str, branch: str = "main") -> str:
    """Creates or updates a file in a GitHub repository.
    If the file exists, it will try to fetch its SHA first to perform an update.
    Direct writes to protected branches (main, master) are not permitted — create a branch first."""
    if branch in PROTECTED_BRANCHES:
        return f"Error: Direct writes to '{branch}' are not permitted. Use github_create_branch to create a working branch, then open a PR."
    client = get_github_client()
    try:
        sha = None
        try:
            existing = await client.get_contents(owner, repo, path, branch)
            if not isinstance(existing, list):
                sha = existing.get("sha")
        except Exception as e:
            if "404" not in str(e):
                return f"Error checking existing file: {str(e)}"

        result = await client.write_file(owner, repo, path, content, message, branch, sha)
        return f"Successfully wrote file '{path}' to {owner}/{repo} (branch: {branch}). URL: {result['content']['html_url']}"
    except Exception as e:
        return f"Error writing file to GitHub: {str(e)}"

async def github_create_pr(owner: str, repo: str, title: str, head: str, base: str = "main", body: Optional[str] = None) -> str:
    """Creates a Pull Request on GitHub."""
    client = get_github_client()
    try:
        pr = await client.create_pr(owner, repo, title, head, base, body)
        return f"Successfully created PR #{pr['number']}: {pr['html_url']}"
    except Exception as e:
        return f"Error creating PR: {str(e)}"

async def github_create_branch(owner: str, repo: str, branch: str, from_branch: str = "main") -> str:
    """Creates a new branch in a GitHub repository, branching off from from_branch (default: main).
    Always call this before github_write_file when preparing changes for a PR."""
    client = get_github_client()
    try:
        await client.create_branch(owner, repo, branch, from_branch)
        return f"Created branch '{branch}' from '{from_branch}' in {owner}/{repo}."
    except Exception as e:
        return f"Error creating branch: {str(e)}"

GITHUB_TOOLS = [
    github_get_repo_info,
    github_list_repos,
    github_read_file,
    github_list_issues,
    github_read_issue,
    github_create_issue,
    github_create_branch,
    github_write_file,
    github_create_pr
]
