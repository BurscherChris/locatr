"""Agent execution loop with diagnostics, loop detection, and completion gate integration.

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
from typing import Any

from app.agent.prompts import SYSTEM_PROMPT
from app.errors import AgentIterationLimitError, ToolExecutionError
from app.neuron.client import NeuronClient
from app.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


# ── Normalisation helpers ──────────────────────────────────────────────


def _normalise_arguments(raw: str) -> str:
    """Parse JSON arguments and re-serialise sorted for stable comparison.

    This strips whitespace differences and key ordering so that
    '{"path":"."}' and '{"path": "."}' compare as equal.
    """
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return json.dumps(parsed, sort_keys=True)
        return raw
    except (json.JSONDecodeError, ValueError):
        return raw


def _safe_preview(text: str, max_len: int = 120) -> str:
    """Truncate *text* for log messages, keeping first *max_len* chars."""
    return text[:max_len] + ("…" if len(text) > max_len else "")


# ── Result tracking across iterations ──────────────────────────────────


@dataclass
class ToolCallRecord:
    """A single recorded tool call for loop/progress detection."""
    name: str
    normalised_args: str
    ok: bool
    previous_result: str


@dataclass
class LoopState:
    """Tracks iteration state for diagnostics and loop detection."""
    iteration: int = 0
    messages_count: int = 0
    last_role: str = ""
    total_tool_calls: int = 0
    recent: list[ToolCallRecord] = field(default_factory=list)
    meaningful_changes: int = 0

    # ── helpers ────────────────────────────────────────────────────────

    def record(self, name: str, raw_args: str, ok: bool, result_message: str) -> None:
        normalised = _normalise_arguments(raw_args)
        self.recent.append(ToolCallRecord(
            name=name,
            normalised_args=normalised,
            ok=ok,
            previous_result=result_message,
        ))
        if len(self.recent) > 20:
            self.recent.pop(0)
        if ok and name in (
            "write_file", "git_commit", "git_push",
            "git_create_branch", "create_pull_request",
        ):
            self.meaningful_changes += 1

    def repeat_count(self, name: str, raw_args: str) -> int:
        """How many times the exact (name, normalised args) has been called consecutively before now."""
        normalised = _normalise_arguments(raw_args)
        count = 0
        for r in reversed(self.recent):
            if r.name == name and r.normalised_args == normalised:
                count += 1
            else:
                break
        return count

    def previous_result_for(self, name: str, raw_args: str) -> str | None:
        """Return the result of the most recent identical call, or None."""
        normalised = _normalise_arguments(raw_args)
        for r in reversed(self.recent):
            if r.name == name and r.normalised_args == normalised:
                return r.previous_result
        return None

    def _last_fingerprint(self) -> tuple[str, str]:
        if not self.recent:
            return ("", "")
        r = self.recent[-1]
        return (r.name, r.normalised_args)

    def detect_loop(self) -> str | None:
        """Return a description if a loop is detected, else None.

        A loop means the same tool + same normalised arguments appeared
        at least 4 times consecutively with no state change.
        """
        if len(self.recent) < 4:
            return None
        fingerprint = self._last_fingerprint()
        count = 0
        for r in reversed(self.recent):
            if (r.name, r.normalised_args) == fingerprint:
                count += 1
            else:
                break
        if count >= 4:
            return f"repeated identical action {count} times: {fingerprint[0]}"
        return None

    def progress_warning(self) -> bool:
        return self.iteration > 20 and self.meaningful_changes == 0


# ── Main loop ──────────────────────────────────────────────────────────


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

            # ── Debug: incoming context ─────────────────────────────────
            last_msg = messages[-1] if messages else {}
            pending_tool_calls = any(
                msg.get("tool_calls") for msg in messages
                if isinstance(msg, dict) and msg.get("role") == "assistant"
            )
            log.debug("ITERATION %s pre-neuron: last_role=%s last_content=%s pending_tool_calls=%s messages=%s",
                      iteration,
                      last_msg.get("role", "?"),
                      _safe_preview(str(last_msg.get("content", "")), 80),
                      pending_tool_calls,
                      len(messages))

            assistant = await self.client.complete(messages, self.tools.definitions())

            # finish_reason is an internal diagnostic field — pop it before storing
            # the message so the history sent back to Neuron stays OpenAI-compatible.
            finish_reason = assistant.pop("_finish_reason", None) or assistant.get("finish_reason", "unknown")

            messages.append(assistant)
            state.last_role = "assistant"

            tool_calls = assistant.get("tool_calls") or []
            content = assistant.get("content") or ""

            # ── Debug: neuron response ──────────────────────────────────
            if tool_calls:
                names = [c.get("function", {}).get("name", "?") for c in tool_calls]
                ids = [c.get("id", "?") for c in tool_calls]
                log.debug("ITERATION %s post-neuron: finish_reason=%s tool_calls=%s ids=%s names=%s",
                          iteration, finish_reason, len(tool_calls), ids, names)
            else:
                log.debug("ITERATION %s post-neuron: finish_reason=%s content_len=%s",
                          iteration, finish_reason, len(content))

            # ── Info logging ────────────────────────────────────────────
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
                    call_id = call.get("id", "unknown")

                    log.info("ITERATION %s executing tool=%s id=%s args_preview=%s",
                             iteration, name, call_id, _safe_preview(arguments_raw, 200))

                    if on_tool:
                        notification = on_tool(name)
                        if inspect.isawaitable(notification):
                            await notification

                    # How many times has this exact call already been made consecutively?
                    prior_repeats = state.repeat_count(name, arguments_raw)

                    result = await self.tools.execute(name, arguments_raw)
                    state.total_tool_calls += 1

                    result_message = result.message()
                    state.record(name, arguments_raw, result.ok, result_message)

                    log.info("ITERATION %s tool=%s id=%s ok=%s repeats=%s result_preview=%s",
                             iteration, name, call_id, result.ok, prior_repeats,
                             _safe_preview(result_message, 120))

                    # When the model repeats the exact same read-only call, inject an
                    # explicit note so it knows the result is unchanged and should move on.
                    # This gives the model a chance to correct course before the hard
                    # loop guard at 4 repetitions fires.
                    if prior_repeats >= 2 and result.ok:
                        result_message = json.dumps({
                            "ok": True,
                            "data": result.data,
                            "note": (
                                f"You have called {name} with these exact arguments "
                                f"{prior_repeats + 1} times. The result is identical to before. "
                                "Do not repeat this call — use the information you already have "
                                "to move forward with the task (e.g. read_file for a specific file, "
                                "write_file to make changes, run_tests to validate)."
                            ),
                        })
                        log.warning("ITERATION %s: injected repeat-warning into tool result for %s (repeats=%s)",
                                    iteration, name, prior_repeats + 1)

                    history.append({"tool": name, "ok": result.ok, "tool_call_id": call_id})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_message,
                    })

                # Loop detection (after ALL tool calls in this iteration)
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
                        log.info("ITERATION %s: completion gate did not pass: %s",
                                 iteration, _safe_preview(gate_message, 200))
                        messages.append({
                            "role": "tool",
                            "tool_call_id": "completion_gate",
                            "content": json.dumps({"ok": False, "error": gate_message}),
                        })
                        continue

                return {
                    "status": "completed",
                    "iterations": iteration,
                    "message": content,
                    "tool_history": history,
                    "final_text": content,
                }

        raise AgentIterationLimitError(f"agent reached {self.max_iterations} iterations")