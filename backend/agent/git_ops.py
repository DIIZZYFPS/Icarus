import os
import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)

def github_clone_repo(repo_url: str, target_dir: str, branch: str = "main", token: Optional[str] = None) -> str:
    """
    Clones a GitHub repository into the workspace.
    
    If token is provided, uses it for authentication.
    repo_url should be the HTTPS URL (e.g., https://github.com/owner/repo.git)
    """
    try:
        # If token is provided, inject it into the URL for authentication
        if token:
            repo_url = repo_url.replace("https://", f"https://{token}@")
        
        # Ensure target directory exists
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)

        # Build git clone command
        # --depth 1 for shallow clone to save space/time
        # -b for branch selection
        cmd = ["git", "clone", "--depth", "1", "-b", branch, repo_url, target_dir]
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return f"Successfully cloned {repo_url} (branch: {branch}) to {target_dir}.\n{result.stdout}"
    except subprocess.CalledProcessError as e:
        error_msg = f"Failed to clone repository: {e.stderr}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        return f"Unexpected error: {str(e)}"
