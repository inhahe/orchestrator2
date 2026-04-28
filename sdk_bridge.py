"""Claude Agent SDK integration — connect, run turns, dispatch messages.

This is the core bridge between the FastAPI backend and ``claude_agent_sdk``.
It replaces the Orchestrator class's ``_connect()``, ``_message_dispatcher()``,
``run_turn()``, ``worker_loop()``, and ``_handle_async_message()`` methods,
adapted for the web: instead of printing ANSI, it broadcasts structured JSON
messages through a ``broadcaster`` callback.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Awaitable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
try:
    from claude_agent_sdk import (
        PermissionResultAllow,
        PermissionResultDeny,
    )
except ImportError:
    PermissionResultAllow = None  # type: ignore
    PermissionResultDeny = None  # type: ignore

try:
    from claude_agent_sdk import ThinkingBlock  # type: ignore
except ImportError:
    ThinkingBlock = None  # type: ignore[assignment]

from config import (
    CONTINUE_PROMPT,
    DISPATCHER_DEAD,
    DONE_SENTINEL,
    WAITING_SENTINEL,
    Config,
    default_compact_at,
    model_context_window,
)
from state import (
    State,
    apply_rate_limit_info,
    fmt_duration,
    humanize_size,
    ring_bell,
    state_to_panels_dict,
    state_to_status_dict,
)
from session import (
    find_most_recent_session_for_cwd,
    find_session_dir,
    read_session_title,
    render_session_history,
    trim_session,
    project_dir_for_cwd,
)
from tool_manager import (
    complete_bg_task,
    complete_tool,
    format_tool_result_msg,
    format_tool_use_msg,
    register_bg_task,
    register_thinking,
    register_tool_use,
    summarize_tool_result,
)

log = logging.getLogger(__name__)

# Broadcaster type: async function that sends a dict to all WS clients.
Broadcaster = Callable[[dict[str, Any]], Awaitable[None]]


class SDKBridge:
    """Manages the Claude Agent SDK lifecycle and message flow."""

    def __init__(
        self,
        config: Config,
        state: State,
        broadcaster: Broadcaster,
    ) -> None:
        self.config = config
        self.state = state
        self.broadcast = broadcaster

        self.client: ClaudeSDKClient | None = None
        self.event_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.turn_msg_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.turn_active = asyncio.Event()
        self.interrupt_event = asyncio.Event()
        self.stop_event = asyncio.Event()

        self._dispatcher_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._initial_resume_id: str | None = None
        self._pending_permission: asyncio.Future | None = None
        self._is_auto_turn = False  # True when the turn was triggered by auto-continue

    # ------------------------------------------------------------------
    # SDK options
    # ------------------------------------------------------------------

    def _make_options(self, resume_id: str | None = None) -> ClaudeAgentOptions:
        """Build ``ClaudeAgentOptions`` from config + state."""
        kwargs: dict[str, Any] = {
            "permission_mode": self.config.permission_mode,
            "cwd": self.config.cwd,
            "setting_sources": ["user", "project", "local"],
            "max_buffer_size": 10 * 1024 * 1024,
        }

        # Session resume / continue logic.
        rid = resume_id or self._initial_resume_id
        if rid:
            kwargs["resume"] = rid
        elif not self.config.no_continue:
            kwargs["continue_conversation"] = True

        # Effort override.
        if self.state.effort:
            kwargs["effort"] = self.state.effort

        # Model override.
        if self.state.model:
            kwargs["model"] = self.state.model

        # Thinking toggle.
        if not self.state.thinking_enabled:
            kwargs["thinking"] = {"type": "disabled"}

        # Tool allow/deny lists.
        if self.config.allowed_tools:
            kwargs["allowed_tools"] = list(self.config.allowed_tools)
        if self.config.disallowed_tools:
            kwargs["disallowed_tools"] = list(self.config.disallowed_tools)

        # MCP config.
        if self.config.mcp_config:
            kwargs["mcp_config"] = self.config.mcp_config

        # System prompt extension.
        if self.config.append_system_prompt:
            kwargs["append_system_prompt"] = self.config.append_system_prompt

        # Permission callback (when not bypassing).
        if (
            self.config.permission_mode != "bypassPermissions"
            and PermissionResultAllow is not None
        ):
            kwargs["can_use_tool"] = self._handle_tool_permission

        return ClaudeAgentOptions(**kwargs)

    # ------------------------------------------------------------------
    # Connect / disconnect / reconnect
    # ------------------------------------------------------------------

    async def connect(self, resume_id: str | None = None) -> None:
        """Create SDK client, connect, and start the message dispatcher."""
        options = self._make_options(resume_id)
        self._initial_resume_id = None  # one-time use
        self.client = ClaudeSDKClient(options=options)

        self.state.connecting = True
        self.state.connect_started_at = time.monotonic()
        await self.broadcast({"type": "status_update",
                              "status": state_to_status_dict(self.state, self.config)})

        try:
            await self.client.connect()
        finally:
            self.state.connecting = False
            self.state.connect_started_at = None

        elapsed = time.monotonic() - (self.state.connect_started_at or time.monotonic())
        await self.broadcast({
            "type": "system_msg",
            "subtype": "connected",
            "data": {"elapsed": fmt_duration(abs(elapsed))},
        })

        # Start the message dispatcher.
        self._dispatcher_task = asyncio.create_task(
            self._message_dispatcher(), name="sdk-dispatcher",
        )

    async def disconnect(self) -> None:
        """Shut down client and dispatcher."""
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def reconnect(self) -> None:
        """Disconnect and reconnect with current session ID."""
        sid = self.state.session_id
        await self.broadcast({
            "type": "system_msg",
            "subtype": "reconnecting",
            "data": {
                "effort": self.state.effort or "auto",
                "model": self.state.model or "auto",
                "session_id": sid,
            },
        })
        await self.disconnect()
        await self.connect(resume_id=sid)

    # ------------------------------------------------------------------
    # Message dispatcher
    # ------------------------------------------------------------------

    async def _message_dispatcher(self) -> None:
        """Read SDK messages and route to turn queue or async handler."""
        try:
            async for msg in self.client.receive_messages():
                if self.stop_event.is_set():
                    break
                if self.turn_active.is_set():
                    await self.turn_msg_queue.put(msg)
                else:
                    await self._handle_async_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("dispatcher crashed: %s", exc)
            if self.turn_active.is_set():
                await self.turn_msg_queue.put(DISPATCHER_DEAD)

    # ------------------------------------------------------------------
    # Async (between-turn) message handling
    # ------------------------------------------------------------------

    async def _handle_async_message(self, msg: Any) -> None:
        """Handle an SDK message that arrives between turns."""
        state = self.state

        if isinstance(msg, SystemMessage):
            await self._handle_system_message(msg, during_turn=False)

        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text = block.text.strip()
                    if text:
                        await self.broadcast({
                            "type": "assistant_text",
                            "content": text,
                            "delta": False,
                            "is_async": True,
                        })

        elif isinstance(msg, ResultMessage):
            if msg.session_id:
                state.session_id = msg.session_id
            if hasattr(msg, "usage") and msg.usage:
                state.last_usage = msg.usage if isinstance(msg.usage, dict) else {}
            if hasattr(msg, "total_cost_usd"):
                state.total_cost_usd += msg.total_cost_usd or 0
            subtype = getattr(msg, "subtype", None) or "unknown"
            state.last_result_subtype = subtype

    # ------------------------------------------------------------------
    # System message handling (shared by turn + async paths)
    # ------------------------------------------------------------------

    async def _handle_system_message(self, msg: SystemMessage, *, during_turn: bool) -> None:
        """Process a SystemMessage from the SDK."""
        state = self.state
        subtype = getattr(msg, "subtype", None) or ""
        data: dict[str, Any] = {}

        if subtype == "init":
            first_init = not state.init_seen
            state.init_seen = True
            sid = getattr(msg, "session_id", None)
            old_sid = state.session_id
            if sid:
                state.session_id = sid
                state.session_title = read_session_title(sid)
                # Warn if the SDK silently started a fresh session
                # instead of resuming the one we asked for.
                expected = state.expected_resume_sid
                if first_init and expected and expected != sid:
                    state.expected_resume_sid = None
                    log.warning(
                        "expected to resume %s but SDK started %s",
                        expected[:12], sid[:12],
                    )
                    await self.broadcast({
                        "type": "system_msg",
                        "subtype": "warning",
                        "data": {"message": (
                            f"Expected to resume {expected[:12]} but SDK "
                            f"started {sid[:12]} — prior context may not "
                            f"be loaded."
                        )},
                    })
            # The SDK sends init every turn.  Only broadcast the first
            # one (or session-id changes) — repeats are just noise.
            if not first_init and sid == old_sid:
                return  # suppress repeat init
            data = {"session_id": sid}

        elif subtype == "api_retry":
            # Track for stall detection.
            state.api_retry_times.append(time.monotonic())
            error_info = getattr(msg, "error", None)
            data = {"error": str(error_info) if error_info else None}
            # Check stall threshold.
            if self.config.api_stall_limit > 0:
                window = self.config.api_stall_window
                cutoff = time.monotonic() - window
                recent = sum(1 for t in state.api_retry_times if t >= cutoff)
                if recent >= self.config.api_stall_limit:
                    state.needs_user_attention = "api-error"
                    ring_bell(state, "api-stall")

        elif subtype == "task_notification":
            # Background task completion.
            task_id = getattr(msg, "task_id", None) or ""
            status = getattr(msg, "status", None) or "completed"
            summary = getattr(msg, "summary", None)
            entry = complete_bg_task(state, task_id, status, summary=summary)
            if entry:
                data = {
                    "task_id": task_id,
                    "seq": entry.get("seq"),
                    "name": entry.get("name"),
                    "status": status,
                    "summary": summary,
                }
                await self.broadcast({"type": "bg_complete", **data})
                ring_bell(state, "bg-done")
                # If no more bg tasks, queue a wakeup.
                if not state.background_tasks:
                    self.event_queue.put_nowait(("wakeup", "bg-all-done"))

        elif subtype == "task_started":
            task_id = getattr(msg, "task_id", None) or ""
            task_type = getattr(msg, "task_type", None) or "bash"
            name = getattr(msg, "name", None) or ""
            tool_use_id = getattr(msg, "tool_use_id", None)
            seq = register_bg_task(
                state, task_id, task_type, name, tool_use_id=tool_use_id,
            )
            data = {
                "task_id": task_id,
                "seq": seq,
                "name": name,
                "task_type": task_type,
            }
            await self.broadcast({"type": "bg_started", **data})

        elif subtype == "compact_boundary":
            meta = getattr(msg, "compact_metadata", None) or {}
            trigger = meta.get("trigger", "sdk")
            pre_tokens = meta.get("pre_tokens", 0)
            state.compact_during_last_turn = True
            state.last_compact_trigger = trigger
            # Reset context tokens — the post-compact size is much smaller.
            state.context_tokens = 0
            # Broadcast a visible message matching the TUI's format.
            data = {
                "message": f"[compacted -- {trigger} -- was ~{pre_tokens} tok]",
            }

        elif subtype == "session_state_changed":
            session_state = getattr(msg, "session_state", None)
            if session_state == "requires_action":
                ring_bell(state, "requires-action")
                data = {"state": "requires_action"}

        # Rate limit info (may be on any system message).
        rate_info = getattr(msg, "rate_limit_info", None)
        if rate_info is not None:
            apply_rate_limit_info(state, rate_info)

        # High-frequency / noisy subtypes — suppress from the frontend.
        if subtype in ("task_progress", "hook_started", "hook_ended"):
            return

        await self.broadcast({
            "type": "system_msg",
            "subtype": subtype,
            "data": data,
        })

    # ------------------------------------------------------------------
    # Tool permission callback
    # ------------------------------------------------------------------

    async def _handle_tool_permission(
        self, tool_name: str, tool_input: dict, context: Any,
    ) -> Any:
        """SDK ``can_use_tool`` callback — asks the user via WebSocket."""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_permission = fut
        await self.broadcast({
            "type": "permission_request",
            "tool_name": tool_name,
            "tool_input": tool_input,
        })
        allowed = await fut
        self._pending_permission = None
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message="User denied")

    def resolve_permission(self, allow: bool) -> None:
        """Called by the WS handler when the user responds to a permission prompt."""
        if self._pending_permission and not self._pending_permission.done():
            self._pending_permission.set_result(allow)

    # ------------------------------------------------------------------
    # run_turn() — process one complete turn
    # ------------------------------------------------------------------

    async def run_turn(self, prompt_text: str) -> tuple[str, bool]:
        """Send a prompt and process the full turn.

        Returns ``(assistant_text, was_interrupted)``.
        """
        state = self.state
        config = self.config

        # Drain stale messages from the queue.
        while not self.turn_msg_queue.empty():
            try:
                self.turn_msg_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self.interrupt_event.clear()
        state.busy = True
        state.turn_started_at = time.monotonic()
        state.compact_during_last_turn = False
        state.current_turn_tool_seqs = []

        # Route SDK messages to turn_msg_queue.
        self.turn_active.set()

        await self.broadcast({"type": "turn_start", "prompt": prompt_text})

        # Send the prompt.
        await self.client.query(prompt_text)

        assistant_text = ""
        interrupted = False
        current_streaming_text = ""

        try:
            while True:
                msg = await self.turn_msg_queue.get()

                if msg is DISPATCHER_DEAD:
                    raise RuntimeError("SDK dispatcher died mid-turn")

                # ---- SystemMessage ----
                if isinstance(msg, SystemMessage):
                    await self._handle_system_message(msg, during_turn=True)

                # ---- AssistantMessage ----
                elif isinstance(msg, AssistantMessage):
                    model_id = getattr(msg, "model", None)
                    if model_id and not state.active_model:
                        state.active_model = model_id

                    for block in msg.content:
                        # Text
                        if isinstance(block, TextBlock):
                            text = block.text or ""
                            current_streaming_text += text
                            await self.broadcast({
                                "type": "assistant_text",
                                "content": text,
                                "delta": True,
                            })

                        # Tool use
                        elif isinstance(block, ToolUseBlock):
                            # Flush pending text.
                            if current_streaming_text.strip():
                                assistant_text += current_streaming_text
                                current_streaming_text = ""

                            name = block.name
                            inp = block.input if isinstance(block.input, dict) else {}
                            is_bg = name == "Bash" and inp.get("run_in_background")

                            seq = register_tool_use(
                                state, block.id, name, inp,
                            )

                            # Capture TodoWrite plan.
                            if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                                state.current_todos = inp["todos"]

                            ws_msg = format_tool_use_msg(
                                name, inp, block.id, seq,
                                is_background=bool(is_bg),
                            )
                            await self.broadcast(ws_msg)

                        # Thinking
                        elif ThinkingBlock and isinstance(block, ThinkingBlock):
                            thinking_text = getattr(block, "thinking", "") or ""
                            if thinking_text.strip():
                                seq = register_thinking(state, thinking_text)
                                await self.broadcast({
                                    "type": "thinking",
                                    "seq": seq,
                                    "content": thinking_text,
                                })

                # ---- UserMessage (tool results injected by CLI) ----
                elif isinstance(msg, UserMessage):
                    content = msg.content if hasattr(msg, "content") else None
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                result_text = summarize_tool_result(block)
                                is_err = bool(block.is_error)
                                tool_use_id = block.tool_use_id

                                seq, tool_name, active_info = complete_tool(
                                    state, tool_use_id, result_text, is_err,
                                )
                                ws_msg = format_tool_result_msg(
                                    tool_use_id, seq, tool_name or "?",
                                    result_text, is_err,
                                )
                                await self.broadcast(ws_msg)
                    elif isinstance(content, str):
                        # CLI notice (e.g. task notification XML).
                        await self.broadcast({
                            "type": "system_msg",
                            "subtype": "notice",
                            "data": {"content": content[:500]},
                        })

                # ---- ResultMessage (end of turn) ----
                elif isinstance(msg, ResultMessage):
                    if current_streaming_text.strip():
                        assistant_text += current_streaming_text
                        current_streaming_text = ""

                    if msg.session_id:
                        state.session_id = msg.session_id

                    # Book metrics (skip if this was a compact turn).
                    if not state.compact_during_last_turn:
                        state.turns += 1
                        if hasattr(msg, "total_cost_usd") and msg.total_cost_usd:
                            state.total_cost_usd += msg.total_cost_usd
                        if hasattr(msg, "usage"):
                            usage = msg.usage if isinstance(msg.usage, dict) else {}
                            state.last_usage = usage
                            input_tokens = usage.get("input_tokens", 0)
                            if input_tokens:
                                state.context_tokens = input_tokens

                    subtype = getattr(msg, "subtype", None) or "unknown"
                    state.last_result_subtype = subtype

                    # Reload title.
                    if state.session_id:
                        state.session_title = read_session_title(state.session_id)

                    # Rate limit info may be on ResultMessage too.
                    rate_info = getattr(msg, "rate_limit_info", None)
                    if rate_info is not None:
                        apply_rate_limit_info(state, rate_info)

                    duration = time.monotonic() - (state.turn_started_at or time.monotonic())
                    await self.broadcast({
                        "type": "turn_end",
                        "subtype": subtype,
                        "duration": fmt_duration(duration),
                        "turns": state.turns,
                    })
                    break

                # Check for interrupt.
                if self.interrupt_event.is_set():
                    interrupted = True
                    if self.client:
                        try:
                            await self.client.interrupt()
                        except Exception:
                            pass
                    break

        finally:
            self.turn_active.clear()
            state.busy = False
            state.turn_started_at = None
            # Clear foreground active tools (bg tasks survive).
            state.active_tools.clear()

        # Flush bell.
        if state.pending_bell:
            await self.broadcast({"type": "bell", "event": state.pending_bell})
            state.pending_bell = None

        return assistant_text, interrupted

    # ------------------------------------------------------------------
    # worker_loop() — main turn driver
    # ------------------------------------------------------------------

    async def worker_loop(self) -> None:
        """Main loop: connect, run turns, handle auto-continue."""
        state = self.state
        config = self.config

        # --- Connect ---
        try:
            await self.connect()
        except Exception as exc:
            log.error("SDK connect failed: %s", exc)
            await self.broadcast({
                "type": "system_msg",
                "subtype": "error",
                "data": {"message": f"SDK connection failed: {exc}. Send a message to retry."},
            })
            # Wait for user input, then retry connect.
            while not self.stop_event.is_set():
                kind, payload = await self.event_queue.get()
                if kind in ("quit", "force-quit"):
                    return
                if kind == "message":
                    try:
                        await self.connect()
                        # Connection succeeded — push the message back and fall through.
                        self.event_queue.put_nowait((kind, payload))
                        break
                    except Exception as exc2:
                        log.error("SDK reconnect failed: %s", exc2)
                        await self.broadcast({
                            "type": "system_msg",
                            "subtype": "error",
                            "data": {"message": f"SDK reconnect failed: {exc2}"},
                        })

        # --- Initial prompt ---
        next_prompt: str | None = None
        if config.initial_prompt:
            next_prompt = config.initial_prompt
        else:
            # Wait for first user input.
            kind, payload = await self.event_queue.get()
            if kind == "message":
                next_prompt = payload
            elif kind in ("quit", "force-quit"):
                return
            # Other kinds: drop and wait again.
            while next_prompt is None and not self.stop_event.is_set():
                kind, payload = await self.event_queue.get()
                if kind == "message":
                    next_prompt = payload
                    break
                if kind in ("quit", "force-quit"):
                    return

        # --- Turn loop ---
        while next_prompt is not None and not self.stop_event.is_set():
            assistant_text = ""
            interrupted = False
            self._is_auto_turn = next_prompt == state.continue_prompt

            try:
                assistant_text, interrupted = await self.run_turn(next_prompt)
            except Exception as exc:
                log.exception("run_turn failed: %s", exc)
                await self.broadcast({
                    "type": "system_msg",
                    "subtype": "error",
                    "data": {"message": f"Turn failed: {exc}"},
                })
                if config.auto_reconnect:
                    try:
                        await self.reconnect()
                    except Exception:
                        pass
                # Fall through to await next input.

            # --- Between-turn processing ---
            next_prompt = await self._between_turns(
                assistant_text, interrupted,
            )

        await self.broadcast({
            "type": "system_msg",
            "subtype": "worker_stopped",
            "data": {},
        })

    async def _between_turns(
        self,
        assistant_text: str,
        interrupted: bool,
    ) -> str | None:
        """Post-turn decision logic.  Returns the next prompt or None to stop."""
        state = self.state
        config = self.config

        # Drain queued commands.
        has_compact = False
        reconnect_needed = False
        quit_requested = False

        while not self.event_queue.empty():
            try:
                kind, payload = self.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if kind == "message":
                if payload not in state.queued_prompts:
                    state.queued_prompts.append(payload)
            elif kind == "compact":
                has_compact = True
            elif kind == "effort":
                state.effort = None if payload == "auto" else payload
                reconnect_needed = True
                await self.broadcast({
                    "type": "system_msg", "subtype": "info",
                    "data": {"message": f"effort → {payload}"},
                })
            elif kind == "model":
                state.model = payload
                reconnect_needed = True
                await self.broadcast({
                    "type": "system_msg", "subtype": "info",
                    "data": {"message": f"model → {payload}"},
                })
            elif kind == "thinking":
                if payload == "on":
                    state.thinking_enabled = True
                elif payload == "off":
                    state.thinking_enabled = False
                else:
                    state.thinking_enabled = not state.thinking_enabled
                reconnect_needed = True
                await self.broadcast({
                    "type": "system_msg", "subtype": "info",
                    "data": {"message": f"thinking → {'on' if state.thinking_enabled else 'off'}"},
                })
            elif kind == "clear-context":
                await self._clear_context()
                return await self._await_next_prompt()
            elif kind == "connect":
                reconnect_needed = True
            elif kind in ("quit", "force-quit"):
                quit_requested = True
            elif kind == "btw":
                # Side question — for now, queue as regular message.
                # Full /btw implementation would run in a separate context.
                state.queued_prompts.append(payload)

        if quit_requested:
            return None

        if reconnect_needed:
            await self.reconnect()

        # --- Interrupted ---
        if interrupted:
            ring_bell(state, "interrupt")
            state.needs_user_attention = None
            return await self._await_next_prompt()

        # --- Pending /compact ---
        if has_compact:
            await self.broadcast({
                "type": "system_msg", "subtype": "info",
                "data": {"message": "Compacting session..."},
            })
            return "/compact"

        # --- Queued user prompts ---
        if state.queued_prompts:
            prompt = state.queued_prompts.popleft()
            # Echo to chat now that the prompt is being processed.
            await self.broadcast({
                "type": "user_message",
                "content": prompt,
            })
            return prompt

        # --- API stall ---
        if state.needs_user_attention == "api-error":
            return await self._await_next_prompt()

        # --- Context trim (rolling window) ---
        max_ctx = getattr(state, "_max_context_tokens", config.max_context_tokens)
        if max_ctx > 0 and state.context_tokens > max_ctx and state.session_id:
            proj = project_dir_for_cwd(config.cwd)
            new_sid = trim_session(state.session_id, proj, max_ctx)
            if new_sid:
                state.session_id = new_sid
                await self.reconnect()
                await self.broadcast({
                    "type": "system_msg", "subtype": "info",
                    "data": {"message": f"Context trimmed → session {new_sid[:8]}"},
                })

        # --- Auto-compact check ---
        compact_at = getattr(state, "_compact_at", config.compact_at)
        auto_compact = getattr(state, "_auto_compact", config.auto_compact)
        if (
            auto_compact
            and state.context_tokens >= compact_at
            and (
                state.last_compact_turn is None
                or state.turns - state.last_compact_turn > config.compact_cooldown_turns
            )
        ):
            state.last_compact_turn = state.turns
            await self.broadcast({
                "type": "system_msg", "subtype": "info",
                "data": {"message": f"Auto-compacting (ctx {state.context_tokens} >= {compact_at})..."},
            })
            return "/compact"

        # --- Background tasks running → wait ---
        if state.background_tasks:
            state.needs_user_attention = None
            await self.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(state, config),
                "panels": state_to_panels_dict(state),
            })
            return await self._await_next_prompt()

        # --- Auto-continue disabled → wait ---
        auto_continue = getattr(state, "_auto_continue", config.auto_continue)
        if not auto_continue:
            ring_bell(state, "turn-done")
            state.needs_user_attention = None
            return await self._await_next_prompt()

        # --- Auto-continue: check sentinels and burst ---
        if DONE_SENTINEL in assistant_text:
            state.needs_user_attention = "done"
            ring_bell(state, "done")
            await self.broadcast({
                "type": "system_msg", "subtype": "done",
                "data": {"message": "Claude finished all tasks."},
            })
            return await self._await_next_prompt()

        if WAITING_SENTINEL in assistant_text:
            state.needs_user_attention = "waiting"
            ring_bell(state, "waiting")
            await self.broadcast({
                "type": "system_msg", "subtype": "waiting",
                "data": {"message": "Claude is waiting for your input."},
            })
            return await self._await_next_prompt()

        # Burst limit check.
        burst_limit = getattr(state, "_burst_limit", config.continue_burst_limit)
        burst_window = getattr(state, "_burst_window", config.continue_burst_window)
        if burst_limit > 0:
            now = time.monotonic()
            state.recent_turn_ends.append(now)
            cutoff = now - burst_window
            while state.recent_turn_ends and state.recent_turn_ends[0] < cutoff:
                state.recent_turn_ends.popleft()
            if len(state.recent_turn_ends) >= burst_limit:
                state.needs_user_attention = "burst"
                ring_bell(state, "stalled")
                await self.broadcast({
                    "type": "system_msg", "subtype": "stalled",
                    "data": {"message": f"Burst limit ({burst_limit} turns in {burst_window:.0f}s)"},
                })
                return await self._await_next_prompt()

        # --- Grace window: wait for user input or auto-continue ---
        state.needs_user_attention = None
        try:
            kind, payload = await asyncio.wait_for(
                self.event_queue.get(),
                timeout=config.continue_response_delay,
            )
            if kind == "message":
                return payload
            if kind in ("quit", "force-quit"):
                return None
            # Other command — put it back and auto-continue.
            self.event_queue.put_nowait((kind, payload))
        except asyncio.TimeoutError:
            pass

        # Auto-continue.
        return state.continue_prompt

    async def _await_next_prompt(self) -> str | None:
        """Block until the user sends a message or quits."""
        while not self.stop_event.is_set():
            kind, payload = await self.event_queue.get()

            if kind == "message":
                self.state.needs_user_attention = None
                return payload
            if kind in ("quit", "force-quit"):
                return None
            if kind == "compact":
                return "/compact"
            if kind == "connect":
                await self.reconnect()
                continue
            if kind == "clear-context":
                await self._clear_context()
                continue
            if kind == "wakeup":
                # Check if we should auto-resume.
                auto_continue = getattr(
                    self.state, "_auto_continue", self.config.auto_continue
                )
                if (
                    auto_continue
                    and payload in ("bg-all-done", "rate-limit reset", "api-status-recovered")
                    and self.state.needs_user_attention not in ("waiting", "done", "burst")
                ):
                    self.state.needs_user_attention = None
                    return self.state.continue_prompt
                continue
            if kind == "effort":
                self.state.effort = None if payload == "auto" else payload
                await self.reconnect()
                continue
            if kind == "model":
                self.state.model = payload
                await self.reconnect()
                continue
            if kind == "thinking":
                if payload == "on":
                    self.state.thinking_enabled = True
                elif payload == "off":
                    self.state.thinking_enabled = False
                else:
                    self.state.thinking_enabled = not self.state.thinking_enabled
                await self.reconnect()
                continue
            if kind == "btw":
                self.state.queued_prompts.append(payload)
                self.state.needs_user_attention = None
                return self.state.queued_prompts.popleft()
            # Unknown kind — drop and loop.

        return None

    async def _clear_context(self) -> None:
        """Clear session state and reconnect (implements /clear)."""
        state = self.state
        await self.disconnect()
        state.session_id = None
        state.session_title = None
        state.context_tokens = 0
        state.turns = 0
        state.total_cost_usd = 0.0
        state.last_usage = {}
        state.last_result_subtype = None
        state.last_compact_trigger = None
        state.compact_during_last_turn = False
        state.needs_user_attention = None
        state.recent_turn_ends.clear()
        state.active_tools.clear()
        state.background_tasks.clear()
        state.tool_history.clear()
        state.next_tool_seq = 1
        state.thinking_history.clear()
        state.next_thinking_seq = 1
        state.current_todos = []
        state.current_turn_tool_seqs = []
        state.current_turn_bg.clear()
        state.next_bg_seq = 1
        self._initial_resume_id = None
        # Reconnect without resume/continue.
        # Temporarily override config behavior.
        original_no_continue = self.config.no_continue
        # Since Config is frozen, we use a flag.
        self._force_no_continue = True
        await self.connect()
        self._force_no_continue = False
        await self.broadcast({
            "type": "system_msg", "subtype": "cleared",
            "data": {"message": "Session cleared — fresh context."},
        })

    # ------------------------------------------------------------------
    # Control methods
    # ------------------------------------------------------------------

    async def interrupt(self) -> None:
        """Interrupt the current turn."""
        self.interrupt_event.set()
        if self.client:
            try:
                await self.client.interrupt()
            except Exception:
                pass

    async def start(self) -> None:
        """Launch the worker loop as a background task."""
        async def _safe_worker():
            try:
                await self.worker_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("worker_loop crashed: %s", exc)
                try:
                    await self.broadcast({
                        "type": "system_msg",
                        "subtype": "error",
                        "data": {"message": f"Worker loop crashed: {exc}"},
                    })
                except Exception:
                    pass

        self._worker_task = asyncio.create_task(
            _safe_worker(), name="sdk-worker",
        )

    async def stop(self) -> None:
        """Gracefully shut down."""
        self.stop_event.set()
        self.event_queue.put_nowait(("quit", ""))
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.disconnect()
