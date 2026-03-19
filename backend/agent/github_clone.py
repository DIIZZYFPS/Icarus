import asyncio
import os
import shutil

async def github_clone_repo(owner: str, repo: str, target_dir: str, branch: str = "main") -> str:
    """Clones a GitHub repository into the specified directory.
    Uses GITHUB_TOKEN if available for private repository access."""
    token = os.getenv("GITHUB_TOKEN")

    if os.path.exists(target_dir):
        return f"Error: Target directory '{target_dir}' already exists."

    # Ensure parent directory exists before cloning
    parent = os.path.dirname(os.path.abspath(target_dir))
    os.makedirs(parent, exist_ok=True)

    repo_url = f"https://github.com/{owner}/{repo}.git"

    # Pass token via git config env vars to keep it out of command-line args
    env = os.environ.copy()
    if token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {token}"

    cmd = ["git", "clone", "--branch", branch, "--depth", "1", repo_url, target_dir]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            # Clean up any partial clone so retries are possible
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            error_msg = stderr_bytes.decode(errors="replace").strip()
            return f"Error cloning repository: {error_msg}"
        return f"Successfully cloned {owner}/{repo} (branch: {branch}) into {target_dir}."
    except asyncio.TimeoutError:
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
        return "Error: Clone operation timed out."
    except Exception as e:
        return f"Unexpected error during clone: {str(e)}"
