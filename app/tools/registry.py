import json
from app.tools.base import Tool, ToolResult

class ToolRegistry:
    def __init__(self): self._tools: dict[str, Tool] = {}
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools: raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool
    def definitions(self) -> list[dict]: return [tool.definition() for tool in self._tools.values()]
    async def execute(self, name: str, raw_arguments: str | dict) -> ToolResult:
        if name not in self._tools: return ToolResult(False, error=f"unknown tool: {name}")
        try: arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError: return ToolResult(False, error="malformed tool arguments")
        return await self._tools[name].run(arguments)
