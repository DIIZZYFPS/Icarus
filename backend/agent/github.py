import os
import httpx
import logging
import base64
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

class GitHubClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.base_url = "https://api.github.com"
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Icarus-Daemon-v0.1.0"
        }
        if self.token:
            self.headers["Authorization"] = f"token {self.token}"
        else:
            logger.warning("GITHUB_TOKEN not found in environment. GitHub tools will operate in read-only mode for public repos.")

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.request(method, url, headers=self.headers, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"GitHub API Error: {e.response.status_code} - {e.response.text}")
                raise Exception(f"GitHub API Error: {e.response.status_code} - {e.response.text}")
            except Exception as e:
                logger.error(f"GitHub Request Error: {str(e)}")
                raise Exception(f"GitHub Request Error: {str(e)}")

    async def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}")

    async def list_repos(self, user: Optional[str] = None) -> List[Dict[str, Any]]:
        path = f"/users/{user}/repos" if user else "/user/repos"
        return await self._request("GET", path)

    async def get_contents(self, owner: str, repo: str, path: str, ref: str = "main") -> Dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})

    async def list_issues(self, owner: str, repo: str, state: str = "open") -> List[Dict[str, Any]]:
        return await self._request("GET", f"/repos/{owner}/{repo}/issues", params={"state": state})

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/issues/{issue_number}")

    async def get_issue_comments(self, owner: str, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        return await self._request("GET", f"/repos/{owner}/{repo}/issues/{issue_number}/comments")

    async def create_issue(self, owner: str, repo: str, title: str, body: str) -> Dict[str, Any]:
        data = {"title": title, "body": body}
        return await self._request("POST", f"/repos/{owner}/{repo}/issues", json=data)

    async def write_file(self, owner: str, repo: str, path: str, content: str, message: str, branch: str = "main", sha: Optional[str] = None) -> Dict[str, Any]:
        data = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": branch
        }
        if sha:
            data["sha"] = sha
        return await self._request("PUT", f"/repos/{owner}/{repo}/contents/{path}", json=data)

    async def create_pr(self, owner: str, repo: str, title: str, head: str, base: str = "main", body: Optional[str] = None) -> Dict[str, Any]:
        data = {"title": title, "head": head, "base": base}
        if body:
            data["body"] = body
        return await self._request("POST", f"/repos/{owner}/{repo}/pulls", json=data)

    async def create_branch(self, owner: str, repo: str, branch: str, from_branch: str = "main") -> Dict[str, Any]:
        ref_data = await self._request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{from_branch}")
        sha = ref_data["object"]["sha"]
        return await self._request("POST", f"/repos/{owner}/{repo}/git/refs", json={
            "ref": f"refs/heads/{branch}",
            "sha": sha
        })

# Singleton instance
_client = GitHubClient()

def get_github_client():
    return _client
