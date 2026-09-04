import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from app.errors import ToolExecutionError

@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    def message(self) -> str: return json.dumps({"ok": self.ok, "data": self.data} if self.ok else {"ok": False, "error": self.error})

class Tool:
    def __init__(self, name: str, description: str, parameters: dict, execute: Callable[..., Awaitable[Any]]): self.name, self.description, self.parameters, self.execute = name, description, parameters, execute
    def definition(self) -> dict: return {"type":"function", "function":{"name":self.name,"description":self.description,"parameters":self.parameters}}
    async def run(self, arguments: dict) -> ToolResult:
        if not isinstance(arguments, dict): return ToolResult(False, error="arguments must be an object")
        required = self.parameters.get("required", [])
        if missing := [key for key in required if key not in arguments]: return ToolResult(False, error=f"missing required arguments: {', '.join(missing)}")
        try: return ToolResult(True, await self.execute(**arguments))
        except (ToolExecutionError, ValueError, TypeError) as exc: return ToolResult(False, error=str(exc))
        except Exception: return ToolResult(False, error="tool execution failed")
