import json
import pytest
from app.agent.loop import AgentLoop
from app.agent.runner import build_registry

class ScriptedNeuron:
    def __init__(self): self.calls = 0
    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1: return {"role":"assistant","tool_calls":[{"id":"1","function":{"name":"write_file","arguments":json.dumps({"path":"changed.txt","content":"done\n"})}}]}
        if self.calls == 2: return {"role":"assistant","tool_calls":[{"id":"2","function":{"name":"run_command","arguments":json.dumps({"command":"python3 -c 'print(\"ok\")'"})}}]}
        return {"role":"assistant","content":"verified"}

@pytest.mark.asyncio
async def test_agent_loop_executes_real_tools(settings, tmp_path):
    result = await AgentLoop(ScriptedNeuron(), build_registry(settings, tmp_path, include_remote=False), 5).run("change file", "test")
    assert result["status"] == "completed"
    assert (tmp_path / "changed.txt").read_text() == "done\n"
    assert [entry["tool"] for entry in result["tool_history"]] == ["write_file", "run_command"]
