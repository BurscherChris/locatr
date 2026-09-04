"""Regression tests for agent loop detection and tool result quality.

TEST 1:  LLM -> list_files -> tool result -> LLM -> write_file           (no loop)
TEST 2:  LLM -> list_files -> tool result -> LLM -> git_status           (no loop)
TEST 3:  LLM -> list_files -> tool result -> LLM -> final response       (no loop)
TEST 4:  LLM repeats identical list_files call 4x                        (loop guard)
TEST 5:  list_files("/") then list_files("/src")                          (no false positive)
TEST 6:  Tool result has same tool_call_id as assistant tool call
TEST 7:  Conversation history preserves assistant(tool_call) + tool(tool_call_id) order
TEST 8:  Multiple tool calls in a single assistant response
TEST 9:  Neuron response with finish_reason = tool_calls
TEST 10: Neuron response with finish_reason = stop
TEST 11: Tool execution failure returns correct tool result
TEST 12: Completion gate failure does NOT cause infinite idential tool loop
"""

import json
from pathlib import Path

import pytest
from app.agent.governance import GovernanceState, GovernanceMode
from app.agent.loop import AgentLoop, _normalise_arguments, _safe_preview, ToolCallRecord, LoopState
from app.agent.runner import build_registry
from app.tools.base import ToolResult


_GOV = GovernanceState(GovernanceMode.PR_REQUIRED)


# ======================================================================
# Helper: scripted neuron that follows a prescribed response sequence
# ======================================================================


class ScriptedNeuron:
    """Returns pre-programmed responses. Each call advances to the next response."""

    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.call_count = 0

    async def complete(self, messages, tools):
        if self.call_count >= len(self.responses):
            msg = "ScriptedNeuron exhausted (no more responses programmed)"
            return {"role": "assistant", "content": msg}
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp


# ======================================================================
# Helper: build a standard tool registry pointing at tmp_path
# ======================================================================


def _registry(tmp_path: Path):
    return build_registry(
        type("S", (), {"allowed_commands": [], "denied_commands": [], "command_timeout_seconds": 5,
                        "github_token": "", "github_api_url": "", "http_timeout_seconds": 5})(),
        tmp_path, _GOV, include_remote=False,
    )


# ======================================================================
# TEST 1: list_files -> tool result -> write_file (no loop)
# ======================================================================


@pytest.mark.asyncio
async def test_list_files_then_write_file(tmp_path):
    """LLM calls list_files, gets result, then writes a file. No loop."""
    (tmp_path / "README.md").write_text("project")
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "list_files", "arguments": '{"path": "."}'}}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_2", "type": "function",
                         "function": {"name": "write_file", "arguments": '{"path": "output.txt", "content": "done"}'}}]},
        {"role": "assistant", "content": "completed"},
    ])
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    assert (tmp_path / "output.txt").read_text() == "done"
    tools = [e["tool"] for e in result["tool_history"]]
    assert tools == ["list_files", "write_file"]


# ======================================================================
# TEST 2: list_files -> tool result -> git_status (no loop)
# ======================================================================


@pytest.mark.asyncio
async def test_list_files_then_git_status(tmp_path):
    """LLM calls list_files, gets result, then git_status. No loop."""
    (tmp_path / "README.md").write_text("project")
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "list_files", "arguments": '{"path": "."}'}}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "type": "function",
                         "function": {"name": "git_status", "arguments": "{}"}}]},
        {"role": "assistant", "content": "completed"},
    ])
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    tools = [e["tool"] for e in result["tool_history"]]
    assert tools == ["list_files", "git_status"]


# ======================================================================
# TEST 3: list_files -> tool result -> final response (no loop)
# ======================================================================


@pytest.mark.asyncio
async def test_list_files_then_final(tmp_path):
    """LLM calls list_files once, gets result, then stops."""
    (tmp_path / "README.md").write_text("project")
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "list_files", "arguments": '{"path": "."}'}}]},
        {"role": "assistant", "content": "Task is done."},
    ])
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    assert len(result["tool_history"]) == 1
    assert result["tool_history"][0]["tool"] == "list_files"


# ======================================================================
# TEST 4: identical list_files 4x -> loop guard fires
# ======================================================================


@pytest.mark.asyncio
async def test_identical_list_files_loop_guard(tmp_path):
    """LLM calls list_files with same path 4x consecutively -> loop detected."""
    from app.errors import AgentIterationLimitError
    (tmp_path / "README.md").write_text("project")
    same_call = {"id": "c_ls", "type": "function",
                 "function": {"name": "list_files", "arguments": '{"path": "."}'}}
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None, "tool_calls": [same_call]},
        {"role": "assistant", "content": None, "tool_calls": [same_call]},
        {"role": "assistant", "content": None, "tool_calls": [same_call]},
        {"role": "assistant", "content": None, "tool_calls": [same_call]},
        {"role": "assistant", "content": "should never reach here"},
    ])
    with pytest.raises(AgentIterationLimitError, match="loop detected"):
        await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")


# ======================================================================
# TEST 5: list_files("/") then list_files("/src") -> no false positive
# ======================================================================


@pytest.mark.asyncio
async def test_different_paths_no_false_positive(tmp_path):
    """Different paths should not trigger loop detection."""
    (tmp_path / "README.md").write_text("project")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main")
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "list_files", "arguments": '{"path": "."}'}}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "type": "function",
                         "function": {"name": "list_files", "arguments": '{"path": "src"}'}}]},
        {"role": "assistant", "content": "done"},
    ])
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    tools = [e["tool"] for e in result["tool_history"]]
    assert tools == ["list_files", "list_files"]


# ======================================================================
# TEST 6: tool result has same tool_call_id as assistant tool call
# ======================================================================


@pytest.mark.asyncio
async def test_tool_call_id_matches(tmp_path):
    """Verify the tool result message preserves the tool_call_id."""
    (tmp_path / "README.md").write_text("project")
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_abc123", "type": "function",
                         "function": {"name": "list_files", "arguments": '{"path": "."}'}}]},
        {"role": "assistant", "content": "done"},
    ])
    # Monkey-patch AgentLoop.run to capture the appended tool message
    captured = {}

    original_run = AgentLoop.run

    async def patched_run(self, task, context, on_tool=None, completion_check=None):
        result = await original_run(self, task, context, on_tool, completion_check)
        return result

    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    # The tool history entry should contain the call id
    assert result["tool_history"][0].get("tool_call_id") == "call_abc123"


# ======================================================================
# TEST 7: conversation history preserves message order
# ======================================================================


@pytest.mark.asyncio
async def test_conversation_history_order(tmp_path):
    """Verify the messages list maintains correct assistant(tool_call) then tool(result) order."""
    (tmp_path / "README.md").write_text("project")
    messages_snapshot = []

    class CapturingNeuron:
        def __init__(self):
            self.call = 0

        async def complete(self, messages, tools):
            self.call += 1
            messages_snapshot.append((self.call, list(messages)))
            if self.call == 1:
                return {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "list_files", "arguments": '{"path": "."}'}}]}
            return {"role": "assistant", "content": "done"}

    loop = AgentLoop(CapturingNeuron(), _registry(tmp_path), 10)
    result = await loop.run("task", "context")
    assert result["status"] == "completed"

    # Check the messages passed to neuron on 2nd call
    _, msgs = messages_snapshot[1]
    roles = [m["role"] for m in msgs]
    # Should be: system, user, assistant (with tool_calls), tool (with result)
    assert roles == ["system", "user", "assistant", "tool"], f"unexpected roles: {roles}"

    # Verify tool message has the matching tool_call_id
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "c1"
    # Verify tool result is valid JSON
    content = json.loads(tool_msgs[0]["content"])
    assert content["ok"] is True
    assert "files" in content["data"]


# ======================================================================
# TEST 8: multiple tool calls in one assistant response
# ======================================================================


@pytest.mark.asyncio
async def test_multiple_tool_calls_single_response(tmp_path):
    """Two tool calls in one assistant response should both execute and return."""
    (tmp_path / "README.md").write_text("project")
    (tmp_path / "src").mkdir()
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None,
         "tool_calls": [
             {"id": "c1", "type": "function",
              "function": {"name": "list_files", "arguments": '{"path": "."}'}},
             {"id": "c2", "type": "function",
              "function": {"name": "list_files", "arguments": '{"path": "src"}'}},
         ]},
        {"role": "assistant", "content": "done"},
    ])
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    assert len(result["tool_history"]) == 2
    assert result["tool_history"][0]["tool"] == "list_files"
    assert result["tool_history"][1]["tool"] == "list_files"


# ======================================================================
# TEST 9: finish_reason = tool_calls
# ======================================================================


@pytest.mark.asyncio
async def test_finish_reason_tool_calls(tmp_path):
    """finish_reason = tool_calls should be treated the same as having tool_calls."""
    (tmp_path / "README.md").write_text("project")
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None,
         "finish_reason": "tool_calls",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "list_files", "arguments": '{"path": "."}'}}]},
        {"role": "assistant", "content": "finished",
         "finish_reason": "stop"},
    ])
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    assert len(result["tool_history"]) == 1
    assert result["tool_history"][0]["tool"] == "list_files"


# ======================================================================
# TEST 10: finish_reason = stop (no tool calls)
# ======================================================================


@pytest.mark.asyncio
async def test_finish_reason_stop(tmp_path):
    """finish_reason = stop with no tool calls should end the loop."""
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": "Task complete.", "finish_reason": "stop"},
    ])
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    assert result["final_text"] == "Task complete."


# ======================================================================
# TEST 11: tool execution failure is returned as tool result
# ======================================================================


@pytest.mark.asyncio
async def test_tool_failure_as_result(tmp_path):
    """A tool that raises should return a proper tool result with ok=False."""
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c_bad", "type": "function",
                         "function": {"name": "list_files", "arguments": '{"path": "/nonexistent"}'}}]},
        {"role": "assistant", "content": "I see the error, will proceed anyway."},
    ])
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "context")
    assert result["status"] == "completed"
    assert result["tool_history"][0]["ok"] is False


# ======================================================================
# TEST 12: completion gate failure does NOT cause idential tool loop
# ======================================================================


@pytest.mark.asyncio
async def test_completion_gate_no_infinite_loop(tmp_path):
    """When the completion gate fails, the model should not re-issue the same tool call."""
    call_count = 0

    class TrackingNeuron:
        async def complete(self, messages, tools):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "list_files", "arguments": '{"path": "."}'}}]}
            # After seeing gate failure, model decides to do something different
            if call_count == 2:
                return {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "c2", "type": "function",
                                        "function": {"name": "git_status", "arguments": "{}"}}]}
            if call_count == 3:
                return {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "c3", "type": "function",
                                        "function": {"name": "git_diff", "arguments": "{}"}}]}
            return {"role": "assistant", "content": "done after gate fix"}

    async def failing_gate(messages):
        """Gate fails first few times, then passes."""
        tool_count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")
        if tool_count >= 3:
            return True, ""
        return False, "changes not pushed yet"

    (tmp_path / "README.md").write_text("project")
    result = await AgentLoop(TrackingNeuron(), _registry(tmp_path), 10).run(
        "task", "context", completion_check=failing_gate,
    )
    # Should eventually complete (different tool calls, not a loop)
    assert result["status"] == "completed"
    # Should have called multiple different tools
    tools_used = [e["tool"] for e in result["tool_history"]]
    assert len(tools_used) >= 2
    # Should NOT have 4+ identical list_files calls
    list_file_calls = [t for t in tools_used if t == "list_files"]
    assert len(list_file_calls) < 4, f"Too many list_file calls: {tools_used}"


# ======================================================================
# Unit tests for _normalise_arguments
# ======================================================================


class TestNormaliseArguments:
    def test_identical_after_normalisation(self):
        assert _normalise_arguments('{"path":"."}') == _normalise_arguments('{"path": "."}')

    def test_different_keys_stay_different(self):
        assert _normalise_arguments('{"path": "."}') != _normalise_arguments('{"path": "src"}')

    def test_malformed_json_preserved(self):
        assert _normalise_arguments("not json") == "not json"

    def test_empty_object(self):
        assert _normalise_arguments("{}") == "{}"


# ======================================================================
# Unit tests for ToolResult.message() format
# ======================================================================


class TestToolResultMessage:
    def test_valid_json(self):
        r = ToolResult(ok=True, data={"files": ["a", "b"]})
        parsed = json.loads(r.message())
        assert parsed["ok"] is True
        assert parsed["data"]["files"] == ["a", "b"]

    def test_error_result(self):
        r = ToolResult(ok=False, error="something went wrong")
        parsed = json.loads(r.message())
        assert parsed["ok"] is False
        assert "something" in parsed["error"]

    def test_no_data(self):
        r = ToolResult(ok=True, data=None)
        parsed = json.loads(r.message())
        assert parsed["ok"] is True
        assert parsed["data"] is None


# ======================================================================
# Unit tests for detect_loop
# ======================================================================


class TestDetectLoop:
    def test_no_loop_for_few_calls(self):
        s = LoopState()
        assert s.detect_loop() is None

    def test_loop_detected(self):
        s = LoopState()
        for _ in range(4):
            s.record("list_files", '{"path": "."}', True, '{"ok": true}')
        msg = s.detect_loop()
        assert msg is not None
        assert "list_files" in msg

    def test_different_args_no_loop(self):
        s = LoopState()
        s.record("list_files", '{"path": "."}', True, "")
        s.record("list_files", '{"path": "src"}', True, "")
        s.record("list_files", '{"path": "."}', True, "")
        s.record("list_files", '{"path": "src"}', True, "")
        assert s.detect_loop() is None


# ======================================================================
# Unit test for _safe_preview
# ======================================================================


class TestSafePreview:
    def test_short_string(self):
        assert _safe_preview("hello") == "hello"

    def test_long_string_truncated(self):
        long = "x" * 200
        preview = _safe_preview(long, max_len=10)
        assert len(preview) == 11  # 10 chars + ellipsis
        assert preview.endswith("…")

    def test_exact_fit(self):
        assert _safe_preview("12345", max_len=5) == "12345"

# ======================================================================
# TEST 13: Repeat warning injected after 2 identical read-only calls
# ======================================================================


@pytest.mark.asyncio
async def test_repeat_warning_injected_after_two_repeats(tmp_path):
    """After 2 identical list_files calls the tool result must carry a note."""
    (tmp_path / "a.txt").write_text("x")
    call = {"id": "c", "type": "function",
            "function": {"name": "list_files", "arguments": '{"path": "."}'}}
    neuron = ScriptedNeuron([
        {"role": "assistant", "content": None, "tool_calls": [dict(call, id="c1")]},
        {"role": "assistant", "content": None, "tool_calls": [dict(call, id="c2")]},
        {"role": "assistant", "content": None, "tool_calls": [dict(call, id="c3")]},
        {"role": "assistant", "content": "done"},
    ])
    captured_messages = []

    original_complete = neuron.complete

    async def capturing_complete(messages, tools):
        captured_messages.append([dict(m) for m in messages])
        return await original_complete(messages, tools)

    neuron.complete = capturing_complete
    result = await AgentLoop(neuron, _registry(tmp_path), 10).run("task", "ctx")
    assert result["status"] == "completed"

    # The tool result for the 3rd call (c3) should contain the repeat note
    third_call_batch = captured_messages[3]  # messages sent on the 4th neuron call
    tool_results = [m for m in third_call_batch if m.get("role") == "tool" and m.get("tool_call_id") == "c3"]
    assert len(tool_results) == 1
    assert "note" in tool_results[0]["content"]
    assert "3 times" in tool_results[0]["content"]


# ======================================================================
# TEST 14: search_code accepts a single file path (regression for LOC loop)
# ======================================================================


@pytest.mark.asyncio
async def test_search_code_on_single_file(tmp_path):
    """search_code must work when given a file path, not just a directory."""
    from app.tools.filesystem import FilesystemTools
    (tmp_path / "hello.tsx").write_text("import test from 'x';\nconst y = 1;\n")
    fs = FilesystemTools(tmp_path)
    result = await fs.search_code("test", "hello.tsx")
    assert result["matches"]
    assert result["matches"][0]["path"] == "hello.tsx"
    assert result["matches"][0]["line"] == 1
    assert result["searched"] == "hello.tsx"


@pytest.mark.asyncio
async def test_search_code_nonexistent_path_error(tmp_path):
    from app.tools.filesystem import FilesystemTools
    from app.errors import ToolExecutionError
    fs = FilesystemTools(tmp_path)
    with pytest.raises(ToolExecutionError, match="does not exist"):
        await fs.search_code("x", "nope/missing.ts")


# ======================================================================
# TEST 15: list_files on a file path gives a helpful error
# ======================================================================


@pytest.mark.asyncio
async def test_list_files_on_file_gives_hint(tmp_path):
    from app.tools.filesystem import FilesystemTools
    from app.errors import ToolExecutionError
    (tmp_path / "f.ts").write_text("x")
    fs = FilesystemTools(tmp_path)
    with pytest.raises(ToolExecutionError, match="read_file"):
        await fs.list_files("f.ts")


@pytest.mark.asyncio
async def test_list_files_returns_directories_separately(tmp_path):
    from app.tools.filesystem import FilesystemTools
    (tmp_path / "a.ts").write_text("x")
    (tmp_path / "sub").mkdir()
    fs = FilesystemTools(tmp_path)
    result = await fs.list_files(".")
    assert "a.ts" in result["files"]
    assert "sub/" in result["directories"]


# ======================================================================
# TEST 16: Repository tree in initial context
# ======================================================================


def test_build_repository_tree_includes_files_and_skips_noise(tmp_path):
    from app.agent.runner import build_repository_tree
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("x")
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "button.tsx").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "react").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("SECRET=x")
    tree = build_repository_tree(tmp_path)
    assert "app/page.tsx" in tree
    assert "components/button.tsx" in tree
    assert "node_modules" not in tree
    assert ".git" not in tree
    assert ".env" not in tree


def test_build_context_contains_file_tree(tmp_path):
    from app.agent.runner import build_context
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("x")
    ctx = build_context("task", "T-1", "https://x/y.git", tmp_path, "main", None)
    assert "## Repository File Tree" in ctx
    assert "src/main.ts" in ctx


# ======================================================================
# TEST 17: Exploration nudge fires after 8 consecutive read-only calls
# ======================================================================


@pytest.mark.asyncio
async def test_exploration_nudge_injected(tmp_path):
    """After 8 consecutive read-only calls with no writes a runtime notice is added."""
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x")
    reads = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": f"c{i}", "type": "function",
                         "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{i}.txt"})}}]}
        for i in range(8)
    ]
    neuron = ScriptedNeuron(reads + [{"role": "assistant", "content": "done"}])
    captured = []
    orig = neuron.complete

    async def cap(messages, tools):
        captured.append([dict(m) for m in messages])
        return await orig(messages, tools)

    neuron.complete = cap
    await AgentLoop(neuron, _registry(tmp_path), 20).run("task", "ctx")
    # The 9th neuron call (index 8) should see the runtime notice appended after 8 reads
    final_batch = captured[-1]
    notices = [m for m in final_batch if m.get("role") == "user" and "[Runtime notice]" in (m.get("content") or "")]
    assert len(notices) == 1
    assert "8 consecutive read-only" in notices[0]["content"]


@pytest.mark.asyncio
async def test_no_nudge_when_writes_happen(tmp_path):
    """If the model writes a file the read-only streak resets and no nudge fires."""
    for i in range(5):
        (tmp_path / f"a{i}.txt").write_text("x")
    calls = []
    for i in range(5):
        calls.append({"role": "assistant", "content": None,
                      "tool_calls": [{"id": f"r{i}", "type": "function",
                                      "function": {"name": "read_file", "arguments": json.dumps({"path": f"a{i}.txt"})}}]})
    calls.append({"role": "assistant", "content": None,
                  "tool_calls": [{"id": "w", "type": "function",
                                  "function": {"name": "write_file", "arguments": '{"path": "out.txt", "content": "y"}'}}]})
    for i in range(5):
        calls.append({"role": "assistant", "content": None,
                      "tool_calls": [{"id": f"r2{i}", "type": "function",
                                      "function": {"name": "read_file", "arguments": json.dumps({"path": f"a{i}.txt"})}}]})
    calls.append({"role": "assistant", "content": "done"})
    neuron = ScriptedNeuron(calls)
    captured = []
    orig = neuron.complete

    async def cap(messages, tools):
        captured.append([dict(m) for m in messages])
        return await orig(messages, tools)

    neuron.complete = cap
    await AgentLoop(neuron, _registry(tmp_path), 30).run("task", "ctx")
    all_msgs = [m for batch in captured for m in batch]
    notices = [m for m in all_msgs if m.get("role") == "user" and "[Runtime notice]" in (m.get("content") or "")]
    assert not notices
