import asyncio
from pathlib import Path
from app.errors import ToolExecutionError
from app.security.permissions import validate_command

class ShellTools:
    def __init__(self, workspace: Path, allowed: set[str], denied: set[str], timeout: int): self.workspace, self.allowed, self.denied, self.timeout = workspace, allowed, denied, timeout
    async def run_command(self, command: str, timeout_seconds: int | None = None) -> dict:
        args = validate_command(command, self.allowed, self.denied)
        timeout = min(timeout_seconds or self.timeout, self.timeout)
        try:
            process = await asyncio.create_subprocess_exec(*args, cwd=self.workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            raise ToolExecutionError(f"unable to start command: {exc}") from exc
        try: stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill(); await process.communicate(); raise ToolExecutionError(f"command timed out after {timeout} seconds")
        return {"command": args, "exit_code": process.returncode, "stdout": stdout.decode(errors="replace")[-20000:], "stderr": stderr.decode(errors="replace")[-20000:]}
    async def run_tests(self, command: str = "pytest") -> dict:
        return await self.run_command(command)
