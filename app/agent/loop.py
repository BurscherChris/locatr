import inspect
import json
import logging
from app.agent.prompts import SYSTEM_PROMPT
from app.errors import AgentIterationLimitError
from app.neuron.client import NeuronClient
from app.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


class AgentLoop:
    def __init__(self, client: NeuronClient, tools: ToolRegistry, max_iterations: int):
        self.client = client
        self.tools = tools
        self.max_iterations = max_iterations

    async def run(self, task: str, context: str, on_tool=None) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Task: {task}\n\nWorkspace context:\n{context}"},
        ]
        history = []
        for iteration in range(1, self.max_iterations + 1):
            assistant = await self.client.complete(messages, self.tools.definitions())
            messages.append(assistant)
            tool_calls = assistant.get("tool_calls") or []
            content = assistant.get("content", "")

            if tool_calls:
                log.info("Agent iteration %s: %s tool call(s) requested: %s",
                         iteration, len(tool_calls),
                         [c.get("function", {}).get("name", "?") for c in tool_calls])
            else:
                log.info("Agent iteration %s: no tool calls — model returned text (len=%s)",
                         iteration, len(content))

            if not tool_calls:
                return {
                    "status": "completed",
                    "iterations": iteration,
                    "message": content,
                    "tool_history": history,
                    "final_text": content,
                }

            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name", "")
                arguments_raw = function.get("arguments", "{}")
                log.info("Agent iteration %s executing tool=%s args_preview=%s",
                         iteration, name, arguments_raw[:200])
                if on_tool:
                    notification = on_tool(name)
                    if inspect.isawaitable(notification):
                        await notification
                result = await self.tools.execute(name, arguments_raw)
                log.info("Agent iteration %s tool=%s ok=%s error=%s",
                         iteration, name, result.ok, result.error if not result.ok else "")
                history.append({"tool": name, "ok": result.ok})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "unknown"),
                    "content": result.message(),
                })

        raise AgentIterationLimitError(f"agent reached {self.max_iterations} iterations")