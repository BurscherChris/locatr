import asyncio
import os
from pathlib import Path
from app.errors import GitError


async def run_git(workspace: Path, *args: str, timeout: int = 120, token: str = "") -> str:
    env = os.environ.copy()
    # Prevent git from using any cached credentials or credential helpers
    # that might conflict with the askpass-based authentication.
    env.pop("GIT_CREDENTIAL_HELPER", None)
    env["GIT_CREDENTIAL_HELPER"] = ""
    if token:
        env["GIT_ASKPASS"] = "/app/app/git/askpass.sh"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["AGENT_GIT_TOKEN"] = token
    else:
        env["GIT_ASKPASS"] = ""
        env["GIT_TERMINAL_PROMPT"] = "0"
    process = await asyncio.create_subprocess_exec("git", *args, cwd=workspace, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try: stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill(); await process.communicate(); raise GitError("git command timed out")
    if process.returncode:
        raise GitError(stderr.decode(errors="replace").strip() or "git command failed")
    return stdout.decode(errors="replace").strip()
