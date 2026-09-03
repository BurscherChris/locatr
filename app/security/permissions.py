import shlex
from pathlib import Path
from app.errors import ToolExecutionError


def safe_path(workspace: Path, requested: str) -> Path:
    root = workspace.resolve()
    candidate = (root / requested).resolve() if not Path(requested).is_absolute() else Path(requested).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolExecutionError("requested path is outside the workspace")
    relative = candidate.relative_to(root) if candidate != root else Path(".")
    if any(part in {".git", ".ssh"} or part == ".env" for part in relative.parts):
        raise ToolExecutionError("access to protected workspace files is not permitted")
    return candidate


def validate_command(command: str, allowed: set[str], denied: set[str]) -> list[str]:
    if any(token in command for token in ("\n", "\r", "$(`", "`", ";", "&&", "||", ">", "<")):
        raise ToolExecutionError("shell operators and redirection are not permitted")
    try: args = shlex.split(command)
    except ValueError as exc: raise ToolExecutionError(f"invalid command: {exc}") from exc
    if not args: raise ToolExecutionError("command is required")
    executable = Path(args[0]).name
    if executable in denied or executable not in allowed:
        raise ToolExecutionError(f"command '{executable}' is not permitted")
    if any(arg.startswith("/") or ".." in Path(arg).parts for arg in args[1:] if arg.startswith(("/", ".")) or "/" in arg):
        raise ToolExecutionError("command path escapes are not permitted")
    return args
