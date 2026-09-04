"""Agent execution loop with diagnostics, loop detection, and completiogn gate integration.

One iteration:
1. Send conversation to Neuron
2. If model returns tool calls → execute them → append results → next iteration
3. If model returns text (no tool calls) → evaluate completion gate
   - Gate passes → return completed
   - Gate fails → append gate failure to conversation → next iteration
4. Loop detection: repeated identical tool calls with no state change
5. Progress detection: warn if no meaningful progress after many iterations
"""

import inspect
import json
import logging
from dataclasses import dataclass, field

from app.agent.prompts import SYSTEM_PROMPT
from app.errors import AgentIterationLimitError, ToolExecutionError
from app.neuron.client import NeuronClient
from app.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


@dataclass
class LoopState:
    """Tracks iteration state for diagnostics and loop detection."""
    iteration: int = 0
    messages_count: int = 0
    last_role: str = ""
    total_tool_calls: int = 0
    recent_tool_calls: list[tuple[str, str]] = field(default_factory=list)  # (tool_name, args_preview)
    meaningful_changes: int = 0

    def record_tool_call(self, name: str, args: str, ok: bool) -> None:
        preview = args[:80]
        self.recent_tool_calls.append((name, preview))
        if len(self.recent_tool_calls) > 10:
            self.recent_tool_calls.pop(0)
        if ok and name in ("write_file", "git_commit", "git_push", "git_create_branch", "create_pull_request"):
            self.meaningful_changes += 1

    def detect_loop(self) -> str | None:
        """Return a description if a loop is detected, else None."""
        recent = self.recent_tool_calls
        if len(recent) >= 4:
            last = recent[-1]
            # Check for 3+ identical consecutive tool calls
            count = sum(1 for r in reversed(recent) if r == last)
            if count >= 4:
                return f"repeated identical action {count} times: {last[0]}"
        return None

    def progress_warning(self) -> bool:
        return self.iteration > 20 and self.meaningful_changes == 0


class AgentLoop:
    def __init__(self, client: NeuronClient, tools: ToolRegistry, max_iterations: int):
        self.client = client
        self.tools = tools
        self.max_iterations = max_iterations

    async def run(self, task: str, context: str, on_tool=None, completion_check=None) -> dict:
        """Run the agent loop.

        Args:
            task: The Linear issue task description.
            context: Built workspace/instruction context.
            on_tool: Optional callback when a tool is invoked.
            completion_check: Optional async function(conversation) -> (passed: bool, message: str).
                             Called when the model returns text. If it returns False,
                             the message is appended and the loop continues.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Task: {task}\n\nWorkspace context:\n{context}"},
        ]
        history = []
        state = LoopState()

        for iteration in range(1, self.max_iterations + 1):
            state.iteration = iteration
            state.messages_count = len(messages)

            assistant = await self.client.complete(messages, self.tools.definitions())
            messages.append(assistant)
            state.last_role = "assistant"

            tool_calls = assistant.get("tool_calls") or []
            content = assistant.get("content", "")

            # ── Logging ─────────────────────────────────────────────────
            if tool_calls:
                names = [c.get("function", {}).get("name", "?") for c in tool_calls]
                log.info("ITERATION %s: %s tool calls: %s | messages=%s | meaningful_changes=%s",
                         iteration, len(tool_calls), names, len(messages), state.meaningful_changes)
            else:
                log.info("ITERATION %s: text response (len=%s) | messages=%s | meaningful_changes=%s",
                         iteration, len(content), len(messages), state.meaningful_changes)

            if state.progress_warning():
                log.warning("ITERATION %s: no meaningful progress after %s iterations", iteration, iteration)

            # ── Tool calls ──────────────────────────────────────────────
            if tool_calls:
                for call in tool_calls:
                    function = call.get("function", {})
                    name = function.get("name", "")
                    arguments_raw = function.get("arguments", "{}")

                    log.info("ITERATION %s executing tool=%s args_preview=%s",
                             iteration, name, arguments_raw[:200])

                    if on_tool:
                        notification = on_tool(name)
                        if inspect.isawaitable(notification):
                            await notification

                    result = await self.tools.execute(name, arguments_raw)
                    state.total_tool_calls += 1
                    state.record_tool_call(name, arguments_raw, result.ok)

                    log.info("ITERATION %s tool=%s ok=%s error=%s",
                             iteration, name, result.ok, result.error if not result.ok else "")

                    history.append({"tool": name, "ok": result.ok})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", "unknown"),
                        "content": result.message(),
                    })

                    # Loop detection
                    loop_msg = state.detect_loop()
                    if loop_msg:
                        log.error("ITERATION %s: loop detected: %s", iteration, loop_msg)
                        raise AgentIterationLimitError(f"agent loop detected: {loop_msg}")

            # ── Text response (no tool calls) ──────────────────────────
            else:
                if completion_check:
                    try:
                        passed, gate_message = await completion_check(messages)
                    except Exception as exc:
                        log.warning("ITERATION %s: completion check error: %s", iteration, exc)
                        passed, gate_message = False, f"Completion check failed: {exc}"

                    if not passed:
                        log.info("ITERATION %s: completion gate did not pass: %s", iteration, gate_message[:200])
                        messages.append({
                            "role": "tool",
                            "tool_call_id": "completion_gate",
                            "content": json.dumps({"ok": False, "error": gate_message}),
                        })
                        continue  # let the model see the gate failure and respond

                return {
                    "status": "completed",
                    "iterations": iteration,
                    "message": content,
                    "tool_history": history,
                    "final_text": content,
                }

        raise AgentIterationLimitError(f"agent reached {self.max_iterations} iterations")