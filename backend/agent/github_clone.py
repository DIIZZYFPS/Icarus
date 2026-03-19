import os
import subprocess
import shutil
import logging
from typing import Optional

logger = logging.getLogger(__name__)

async def github_clone_repo(owner: str, repo: str, target_dir: str, branch: str = "main") -> str:
    """Clones a GitHub repository into the specified directory.
    Uses GITHUB_TOKEN if available for private repository access."""
    token = os.getenv("GITHUB_TOKEN")
    
    if os.path.exists(target_dir):
        return f"Error: Target directory '{target_dir}' already exists."

    # Construct URL
    if token:
        repo_url = f"https://{token}@github.com/{owner}/{repo}.git"
    else:
        repo_url = f"https://github.com/{owner}/{repo}.git"

    try:
        # Clone with branch
        cmd = ["git", "clone", "--branch", branch, "--depth", "1", repo_url, target_dir]
        
        # Use shell=False for security
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        return f"Successfully cloned {owner}/{repo} (branch: {branch}) into {target_dir}."
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip()
        return f"Error cloning repository: {error_msg}"
    except Exception as e:
        return f"Unexpected error during clone: {str(e)}"
