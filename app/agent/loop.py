import inspect
import json
from app.agent.prompts import SYSTEM_PROMPT
from app.errors import AgentIterationLimitError
from app.neuron.client import NeuronClient
from app.tools.registry import ToolRegistry

class AgentLoop:
    def __init__(self, client: NeuronClient, tools: ToolRegistry, max_iterations: int): self.client, self.tools, self.max_iterations = client, tools, max_iterations
    async def run(self, task: str, context: str, on_tool=None) -> dict:
        messages = [{"role":"system","content":SYSTEM_PROMPT}, {"role":"user","content":f"Task: {task}\n\nWorkspace context:\n{context}"}]
        history = []
        for iteration in range(1, self.max_iterations + 1):
            assistant = await self.client.complete(messages, self.tools.definitions())
            messages.append(assistant)
            calls = assistant.get("tool_calls") or []
            if not calls: return {"status":"completed","iterations":iteration,"message":assistant.get("content", ""),"tool_history":history}
            for call in calls:
                function = call.get("function", {})
                if on_tool:
                    notification = on_tool(function.get("name", ""))
                    if inspect.isawaitable(notification): await notification
                result = await self.tools.execute(function.get("name", ""), function.get("arguments", "{}"))
                history.append({"tool":function.get("name", ""),"ok":result.ok})
                messages.append({"role":"tool","tool_call_id":call.get("id", "unknown"),"content":result.message()})
        raise AgentIterationLimitError(f"agent reached {self.max_iterations} iterations")
