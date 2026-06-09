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
import sys
import time
from pathlib import Path
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
    INTERRUPT_SENTINEL,
    WAITING_SENTINEL,
    Config,
    default_compact_at,
    model_context_window,
)
from state import (
    State,
    apply_rate_limit_info,
    extract_context_tokens,
    fmt_duration,
    fmt_tok,
    humanize_size,
    ring_bell,
    state_to_panels_dict,
    state_to_status_dict,
)
from session import (
    _classify_user_text,
    find_most_recent_session_for_cwd,
    find_session_dir,
    read_session_title,
    render_session_history,
    trim_session,
    write_session_title,
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


def _build_graphify_prompt(args: str) -> str:
    """Construct a prompt that tells Claude to run the graphify pipeline."""
    # Load the skill file from the installed graphify package.
    # Prefer platform-specific variant (skill-windows.md) when available.
    try:
        import graphify
        pkg_dir = Path(graphify.__path__[0])
        import sys
        if sys.platform == "win32":
            skill_path = pkg_dir / "skill-windows.md"
            if not skill_path.exists():
                skill_path = pkg_dir / "skill.md"
        else:
            skill_path = pkg_dir / "skill.md"
        skill_text = skill_path.read_text(encoding="utf-8")
    except Exception:
        skill_text = None

    path_arg = args.strip() or "."
    if skill_text:
        return (
            f"The user typed `/graphify {path_arg}`. Follow the graphify "
            f"skill instructions below to build a knowledge graph.\n\n"
            f"{skill_text}"
        )
    # Fallback if skill.md can't be loaded.
    return (
        f"Run the graphify pipeline on `{path_arg}`. graphify is installed "
        f"as a Python package (graphifyy). Run:\n\n"
        f"  python -m graphify --help\n\n"
        f"Then follow the appropriate steps to build a knowledge graph."
    )


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
        # Generation counter for dispatcher tasks.  Each connect() bumps
        # this so we can tell live dispatchers apart in logs and detect
        # orphaned ones (stale tasks that survived a reconnect).
        self._dispatcher_gen = 0

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
        # Always prefer an explicit session id (from cwd lookup or --resume)
        # over the SDK's global continue_conversation, which ignores cwd.
        # IMPORTANT: if no session is found for this cwd, we must NOT let the
        # SDK fall through to its default continue_conversation=True behavior,
        # which resumes the globally most recent session regardless of cwd.
        rid = resume_id or self._initial_resume_id
        if rid:
            kwargs["resume"] = rid
            self.state.expected_resume_sid = rid
        elif not self.config.no_continue and self.state.session_id:
            kwargs["resume"] = self.state.session_id
            self.state.expected_resume_sid = self.state.session_id
        else:
            # No session to resume — start fresh.  Explicitly disable the
            # SDK's global continue so it doesn't pick a random session.
            kwargs["continue_conversation"] = False

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
        log.info(
            "connect: resume=%s, cwd=%s",
            getattr(options, 'resume', None),
            getattr(options, 'cwd', None),
        )
        self._initial_resume_id = None  # one-time use

        # If a previous dispatcher task is somehow still alive (e.g.
        # connect() was called without disconnect() — should be
        # impossible in the current call graph, but the symptom of
        # this bug looks exactly like an orphan dispatcher), kill it
        # before we replace ``self.client``.  Otherwise the orphan
        # keeps iterating the old client and races us on the shared
        # ``turn_active`` / ``turn_msg_queue``.
        old_task = self._dispatcher_task
        if old_task is not None and not old_task.done():
            log.warning(
                "connect: orphan dispatcher task alive — cancelling before "
                "creating new one (task=%s)",
                old_task.get_name(),
            )
            old_task.cancel()
            try:
                await old_task
            except (asyncio.CancelledError, Exception):
                pass

        self.client = ClaudeSDKClient(options=options)

        self.state.connecting = True
        started = time.monotonic()
        self.state.connect_started_at = started
        await self.broadcast({"type": "status_update",
                              "status": state_to_status_dict(self.state, self.config)})

        try:
            await self.client.connect()
        finally:
            self.state.connecting = False
            self.state.connect_started_at = None

        # Sync frontend now that connecting flipped to False.  Without
        # this the browser keeps ``_isBusy=true`` (from the earlier
        # ``connecting=True`` status_update) until some other event
        # forces a refresh — and while ``_isBusy`` is true, ``send()``
        # skips its optimistic echo of typed prompts.  Any prompt sent
        # in that window otherwise lands silently on the backend with
        # no chat echo.
        await self.broadcast({
            "type": "status_update",
            "status": state_to_status_dict(self.state, self.config),
            "panels": state_to_panels_dict(self.state),
        })

        elapsed = time.monotonic() - started
        await self.broadcast({
            "type": "system_msg",
            "subtype": "connected",
            "data": {"elapsed": fmt_duration(abs(elapsed))},
        })

        # Start the message dispatcher.  Bump the generation counter
        # so the dispatcher can detect it's been superseded.
        self._dispatcher_gen += 1
        gen = self._dispatcher_gen
        self._dispatcher_task = asyncio.create_task(
            self._message_dispatcher(gen=gen),
            name=f"sdk-dispatcher-{gen}",
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
        # Purge stale bg tasks — old CLI subprocess is dead.
        n_stale = len(self.state.background_tasks)
        if n_stale:
            self.state.background_tasks.clear()
            self.state.completed_panel_bg.clear()
            log.info("cleared %d stale bg task(s) on reconnect", n_stale)
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

    async def _message_dispatcher(self, *, gen: int) -> None:
        """Read SDK messages and route to turn queue or async handler.

        *gen* is a per-connect generation number used to detect orphaned
        dispatcher tasks in the log.  If two dispatchers log routing
        decisions concurrently, they're racing each other on the same
        ``turn_active``/``turn_msg_queue`` — which silently breaks the
        "no run_turn exit but async path fires" symptom.
        """
        client = self.client  # snapshot for diagnostic clarity
        client_id = id(client) & 0xFFFF
        bridge_id = id(self) & 0xFFFF
        ta_id = id(self.turn_active) & 0xFFFF
        log.info(
            "dispatcher start: gen=%d bridge=%04x client=%04x ta=%04x",
            gen, bridge_id, client_id, ta_id,
        )
        msg_count = 0
        try:
            async for msg in client.receive_messages():
                if self.stop_event.is_set():
                    break
                msg_count += 1
                # Snapshot the routing decision so the log unambiguously
                # records which branch fired and which Event/queue we
                # used.  Without this, "async between turns" appears
                # without proof that turn_active was actually checked.
                active = self.turn_active.is_set()
                # Guard: if we are the *not* the current dispatcher
                # (a newer connect() replaced us), bail out — otherwise
                # we keep feeding the bridge's shared turn_msg_queue
                # from a stale SDK client, which is exactly the
                # "ghost messages during a live turn" symptom.
                if self._dispatcher_task is not None and self._dispatcher_gen != gen:
                    log.warning(
                        "dispatcher gen=%d exiting: bridge gen advanced to %d "
                        "(orphan task — would have routed msg #%d %s, active=%s)",
                        gen, self._dispatcher_gen, msg_count,
                        type(msg).__name__, active,
                    )
                    break
                if active:
                    await self.turn_msg_queue.put(msg)
                else:
                    await self._handle_async_message(msg)
        except asyncio.CancelledError:
            log.info(
                "dispatcher cancelled: gen=%d msgs=%d",
                gen, msg_count,
            )
            raise
        except Exception as exc:
            log.exception("dispatcher crashed: gen=%d %s", gen, exc)
            if self.turn_active.is_set():
                await self.turn_msg_queue.put(DISPATCHER_DEAD)
        else:
            log.info(
                "dispatcher exit: gen=%d msgs=%d (receive_messages returned)",
                gen, msg_count,
            )

    # ------------------------------------------------------------------
    # Async (between-turn) message handling
    # ------------------------------------------------------------------

    async def _handle_async_message(self, msg: Any) -> None:
        """Handle an SDK message that arrives between turns.

        The CLI subprocess can stream a whole turn's worth of content
        outside any active ``run_turn`` call — most notably when we
        ``--resume`` a session that was mid-turn when previously killed.
        We can't drive that work from ``run_turn`` (it would inject a
        fresh ``query()``), so we treat it as a *ghost turn*: flip
        ``state.busy`` based on what arrives so the UI shows working,
        and close it out cleanly when the ``ResultMessage`` lands.
        """
        state = self.state

        if isinstance(msg, SystemMessage):
            await self._handle_system_message(msg, during_turn=False)

        elif isinstance(msg, AssistantMessage):
            # The SDK can stream AssistantMessage content outside an active
            # turn — e.g. after a ResultMessage broke run_turn early, or
            # for a side-channel server-initiated message.  Render the
            # full block set (text + tool uses + thinking), otherwise
            # the UI shows narration referring to tool calls that were
            # silently dropped.

            # Diagnostic: any AssistantMessage outside a turn is suspicious
            # (this is the "idle while streaming" symptom).  Log block
            # types + counts so we can correlate with prior ResultMessage.
            # Tag with bridge id + turn_active id so we can detect a
            # stale dispatcher attached to a different bridge.
            try:
                _types = [type(b).__name__ for b in msg.content]
                log.warning(
                    "async AssistantMessage between turns: bridge=%04x "
                    "ta=%04x ta.is_set=%s blocks=%s last_result_subtype=%s "
                    "compact_last=%s",
                    id(self) & 0xFFFF, id(self.turn_active) & 0xFFFF,
                    self.turn_active.is_set(), _types,
                    state.last_result_subtype,
                    state.compact_during_last_turn,
                )
            except Exception:
                pass

            # Ghost-turn busy flip — the SDK is actively producing,
            # so the UI must reflect that.  Stamp turn_started_at on
            # the first message so duration is meaningful, and push
            # one status_update so the toolbar switches off "idle".
            await self._begin_ghost_turn_if_needed()

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
                elif isinstance(block, ToolUseBlock):
                    name = block.name
                    inp = block.input if isinstance(block.input, dict) else {}
                    is_bg = name == "Bash" and inp.get("run_in_background")
                    seq = register_tool_use(state, block.id, name, inp)
                    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                        state.current_todos = inp["todos"]
                    ws_msg = format_tool_use_msg(
                        name, inp, block.id, seq,
                        is_background=bool(is_bg),
                    )
                    await self.broadcast(ws_msg)
                elif ThinkingBlock and isinstance(block, ThinkingBlock):
                    thinking_text = getattr(block, "thinking", "") or ""
                    if thinking_text.strip():
                        seq = register_thinking(state, thinking_text)
                        await self.broadcast({
                            "type": "thinking",
                            "seq": seq,
                            "content": thinking_text,
                        })

        elif isinstance(msg, UserMessage):
            # A UserMessage between turns is either:
            #   (a) tool-result echoes from a ghost turn's in-flight tools, or
            #   (b) a synthetic prompt the CLI injected to the model
            #       (autonomous-loop tick, /loop reschedule, system
            #        reminder, post-compact nudge, etc.) — the user
            #        never typed it but it's part of the conversation.
            # We surface (b) as ``injected_prompt`` so the user can see
            # what the harness is feeding into Claude.
            content = msg.content if hasattr(msg, "content") else None

            injected = self._extract_injected_text(msg)
            if injected is not None:
                log.warning(
                    "async injected prompt: %r (truncated)",
                    injected[:120],
                )
                await self._begin_ghost_turn_if_needed()
                await self._broadcast_injected_prompt(injected, during_turn=False)
                return

            # Diagnostic: tool results between turns means the SDK is
            # still actively processing a prior turn that we marked done.
            try:
                if isinstance(content, list):
                    n_results = sum(
                        1 for b in content if isinstance(b, ToolResultBlock)
                    )
                    if n_results:
                        log.warning(
                            "async UserMessage between turns: tool_results=%d "
                            "last_result_subtype=%s",
                            n_results, state.last_result_subtype,
                        )
            except Exception:
                pass
            # Diagnostic: log any non-tool-result UserMessage text that
            # failed the injected-prompt classification.  These are the
            # candidates for autonomous-loop wakeups, ScheduleWakeup fires,
            # or other harness-injected prompts whose prefix we don't yet
            # match.  Without this log, missed prefixes are invisible.
            try:
                snippet = ""
                if isinstance(content, str):
                    snippet = content
                elif isinstance(content, list):
                    has_tool_result = any(
                        isinstance(b, ToolResultBlock) for b in content
                    )
                    if not has_tool_result:
                        parts = []
                        for b in content:
                            if isinstance(b, TextBlock):
                                parts.append((b.text or "").strip())
                        snippet = "\n".join(p for p in parts if p)
                if snippet.strip():
                    log.warning(
                        "async UserMessage between turns: unclassified text "
                        "(first 200 chars): %r",
                        snippet[:200],
                    )
            except Exception:
                pass
            # Tool results arriving means the ghost turn is alive too.
            await self._begin_ghost_turn_if_needed()
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        result_text = summarize_tool_result(block)
                        is_err = bool(block.is_error)
                        tool_use_id = block.tool_use_id
                        seq, tool_name, _active = complete_tool(
                            state, tool_use_id, result_text, is_err,
                        )
                        ws_msg = format_tool_result_msg(
                            tool_use_id, seq, tool_name or "?",
                            result_text, is_err,
                        )
                        await self.broadcast(ws_msg)

        elif isinstance(msg, ResultMessage):
            if msg.session_id:
                state.session_id = msg.session_id
            raw_usage = getattr(msg, "usage", None)
            raw_model_usage = getattr(msg, "model_usage", None)
            if isinstance(raw_usage, dict):
                state.last_usage = raw_usage
            elif isinstance(raw_model_usage, dict):
                state.last_usage = raw_model_usage
            ctx = extract_context_tokens(raw_usage, raw_model_usage)
            if ctx:
                state.context_tokens = ctx
            if hasattr(msg, "total_cost_usd"):
                state.total_cost_usd += msg.total_cost_usd or 0
            subtype = getattr(msg, "subtype", None) or "unknown"
            state.last_result_subtype = subtype

            # ALWAYS log: a ResultMessage on the async path is the
            # single most diagnostic event for the "stuck on working"
            # symptom — it tells us a turn finished outside ``run_turn``.
            log.warning(
                "async ResultMessage between turns: bridge=%04x "
                "ta.is_set=%s subtype=%s state.busy=%s",
                id(self) & 0xFFFF, self.turn_active.is_set(),
                subtype, state.busy,
            )

            # If this ResultMessage closes a ghost turn, mirror the
            # cleanup that run_turn's finally block does so the UI
            # returns to idle and the next turn can start clean.
            if state.busy and not self.turn_active.is_set():
                await self._end_ghost_turn(subtype)

        else:
            # RateLimitEvent (top-level message, NOT a SystemMessage).
            self._handle_unknown_message(msg)

    # ------------------------------------------------------------------
    # Injected-prompt extraction
    # ------------------------------------------------------------------

    def _extract_injected_text(self, msg: Any) -> str | None:
        """Pull harness-injected prompt text out of a ``UserMessage``.

        The bundled Claude Code CLI sends synthetic prompts to the
        model (autonomous-loop ticks, ``/loop`` reschedules,
        post-compact continuation summaries) as ``type:"user"`` records
        — same field shape as the user's own typed messages.  The only
        reliable signal is the content prefix.  Use the shared
        ``_classify_user_text`` from session.py so the live path and
        the history-replay path agree.

        Returns the injected text, or ``None`` if this ``UserMessage``
        is a tool-result echo, the user's own typed message, or has no
        surfaceable text.
        """
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            text = content.strip()
            if not text:
                return None
            return text if _classify_user_text(text) == "injected_prompt" else None
        if isinstance(content, list):
            # Mixed payloads containing ToolResultBlock are tool-result
            # echoes — not injected prompts.
            parts: list[str] = []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    return None
                if isinstance(block, TextBlock):
                    t = (block.text or "").strip()
                    if t:
                        parts.append(t)
            if not parts:
                return None
            joined = "\n".join(parts)
            return joined if _classify_user_text(joined) == "injected_prompt" else None
        return None

    async def _broadcast_injected_prompt(self, text: str, *, during_turn: bool) -> None:
        """Surface a synthetic user prompt to the frontend."""
        await self.broadcast({
            "type": "injected_prompt",
            "content": text,
            "during_turn": during_turn,
        })

    # ------------------------------------------------------------------
    # Ghost-turn helpers
    # ------------------------------------------------------------------

    async def _begin_ghost_turn_if_needed(self) -> None:
        """Mark a ghost turn active and push a status_update once.

        Called on every async assistant/user message.  Idempotent: only
        flips state once, until ``_end_ghost_turn`` clears it.
        """
        state = self.state
        if state.busy or self.turn_active.is_set():
            return
        state.busy = True
        state.turn_started_at = time.monotonic()
        # Reset compact flag — any prior compact has been absorbed by
        # whatever turn just closed; this is a fresh stream.
        state.compact_during_last_turn = False
        log.warning(
            "ghost turn begin: SDK streaming without active run_turn "
            "(session_id=%s)", (state.session_id or "?")[:12],
        )
        try:
            await self.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(state, self.config),
                "panels": state_to_panels_dict(state),
            })
        except Exception as exc:
            log.warning("ghost-turn status_update broadcast failed: %r", exc)

    async def _end_ghost_turn(self, subtype: str) -> None:
        """Close out a ghost turn cleanly when its ResultMessage lands."""
        state = self.state
        was_compact = state.compact_during_last_turn
        duration = time.monotonic() - (state.turn_started_at or time.monotonic())

        # Mirror run_turn's finally cleanup.
        state.busy = False
        state.turn_started_at = None
        state.active_tools.clear()

        # Book the turn unless this was an internal compact event.
        if not was_compact:
            state.turns += 1
        state.compact_during_last_turn = False

        log.warning(
            "ghost turn end: subtype=%s compact=%s elapsed=%.1fs turns=%d",
            subtype, was_compact, duration, state.turns,
        )

        try:
            await self.broadcast({
                "type": "turn_end",
                "subtype": subtype,
                "duration": fmt_duration(duration),
                "turns": state.turns,
            })
        except Exception as exc:
            log.warning("ghost-turn turn_end broadcast failed: %r", exc)
        try:
            await self.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(state, self.config),
                "panels": state_to_panels_dict(state),
            })
        except Exception as exc:
            log.warning("ghost-turn status_update broadcast failed: %r", exc)

        # If the user queued a prompt while this ghost turn was running,
        # server.py saw state.busy == True and routed it to
        # state.queued_prompts rather than the event_queue.  The worker
        # loop is parked in _await_next_prompt() blocked on event_queue
        # and has no idea a prompt is waiting, so it would sit unsent
        # forever.  Poke the worker with a queue-edit-done wakeup (the
        # same path used after a queue edit) so it pops and sends the
        # queued prompt.  Skip if the first item is being edited.
        if state.queued_prompts and state.queue_editing_index != 0:
            try:
                self.event_queue.put_nowait(("wakeup", "queue-edit-done"))
            except Exception as exc:
                log.warning("ghost-turn queue poke failed: %r", exc)

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
                # Apply a pending rename (from /rename before first turn).
                if state.pending_rename:
                    title = state.pending_rename
                    state.pending_rename = None
                    try:
                        write_session_title(sid, title)
                        state.session_title = title
                        log.info("applied pending rename: %s → '%s'", sid[:8], title)
                    except Exception as exc:
                        # Never let a rename failure kill the turn.
                        log.warning("pending rename failed: %s", exc)
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
            # The SDK stores actual task output in a file.
            output_file = getattr(msg, "output_file", None)
            output = None
            if output_file:
                try:
                    output = Path(output_file).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            entry = complete_bg_task(state, task_id, status, summary=summary)
            if entry:
                cmd = self._bg_task_command(entry)
                # Use output_file content as the display output.  Fall back
                # to summary, but skip it if it's just the command repeated.
                display_output = output or (summary if summary != cmd else None)
                data = {
                    "task_id": task_id,
                    "seq": entry.get("seq"),
                    "name": entry.get("name"),
                    "status": status,
                    "summary": display_output,
                    "command": cmd,
                }
                await self.broadcast({"type": "bg_complete", **data})
                ring_bell(state, "bg-done")
                # If no more bg tasks, update status immediately and queue a wakeup.
                if not state.background_tasks:
                    await self.broadcast({
                        "type": "status_update",
                        "status": state_to_status_dict(state, self.config),
                        "panels": state_to_panels_dict(state),
                    })
                    self.event_queue.put_nowait(("wakeup", "bg-all-done"))

        elif subtype == "task_started":
            task_id = getattr(msg, "task_id", None) or ""
            task_type = getattr(msg, "task_type", None) or "bash"
            name = getattr(msg, "name", None) or ""
            tool_use_id = getattr(msg, "tool_use_id", None)
            seq = register_bg_task(
                state, task_id, task_type, name, tool_use_id=tool_use_id,
            )
            # Look up the Bash command from the linked tool use.
            entry = state.background_tasks.get(task_id) or {}
            cmd = self._bg_task_command(entry)
            # Store command on the entry for panel display.
            if cmd:
                entry["command"] = cmd
            data = {
                "task_id": task_id,
                "seq": seq,
                "name": name,
                "task_type": task_type,
                "command": cmd,
            }
            await self.broadcast({"type": "bg_started", **data})

        elif subtype == "compact_boundary":
            meta = getattr(msg, "compact_metadata", None) or {}
            trigger = meta.get("trigger", "sdk")
            pre_tokens = meta.get("pre_tokens", 0) or state.context_tokens
            state.compact_during_last_turn = True
            state.last_compact_trigger = trigger
            log.warning(
                "compact_boundary received: trigger=%s pre_tokens=%s during_turn=%s",
                trigger, pre_tokens, during_turn,
            )
            # Reset context tokens — the post-compact size is much smaller.
            state.context_tokens = 0
            # Broadcast a visible message matching the TUI's format.
            tok_str = fmt_tok(pre_tokens) if pre_tokens else "?"
            data = {
                "message": f"[compacted -- {trigger} -- was ~{tok_str} tok]",
            }

        elif subtype == "session_state_changed":
            session_state = getattr(msg, "session_state", None)
            if session_state == "requires_action":
                ring_bell(state, "requires-action")
                data = {"state": "requires_action"}

        elif subtype == "task_updated":
            # Patch-based status update — surface terminal states as completions.
            # No typed subclass — fields are in msg.data.
            d = msg.data if isinstance(msg.data, dict) else {}
            task_id = d.get("task_id") or ""
            patch = d.get("patch") if isinstance(d.get("patch"), dict) else {}
            status = patch.get("status")
            if status in ("completed", "failed", "stopped", "cancelled"):
                summary = patch.get("summary")
                output_file = patch.get("output_file") or d.get("output_file")
                output = None
                if output_file:
                    try:
                        output = Path(output_file).read_text(
                            encoding="utf-8", errors="replace",
                        )
                    except Exception:
                        pass
                entry = complete_bg_task(state, task_id, status, summary=summary)
                if entry:
                    cmd = self._bg_task_command(entry)
                    display_output = output or (summary if summary != cmd else None)
                    data = {
                        "task_id": task_id,
                        "seq": entry.get("seq"),
                        "name": entry.get("name"),
                        "status": status,
                        "summary": display_output,
                        "command": cmd,
                    }
                    await self.broadcast({"type": "bg_complete", **data})
                    ring_bell(state, "bg-done")
                    if not state.background_tasks:
                        await self.broadcast({
                            "type": "status_update",
                            "status": state_to_status_dict(state, self.config),
                            "panels": state_to_panels_dict(state),
                        })
                        self.event_queue.put_nowait(("wakeup", "bg-all-done"))

        # Rate limit info (may be on any system message).
        rate_info = getattr(msg, "rate_limit_info", None)
        if rate_info is not None:
            apply_rate_limit_info(state, rate_info)

        # rate_limit_event subtype: info is inside msg.data, not a top-level attr.
        if subtype == "rate_limit_event":
            d = getattr(msg, "data", None)
            if isinstance(d, dict):
                info = d.get("rate_limit_info") or d
                apply_rate_limit_info(state, info)

        # High-frequency / noisy subtypes — suppress from the frontend.
        if subtype in ("hook_started", "hook_ended", "task_output"):
            return

        # Track progress timestamps for stale detection.
        if subtype == "task_progress":
            task_id = getattr(msg, "task_id", None) or ""
            if task_id and task_id in state.background_tasks:
                state.background_tasks[task_id]["last_progress_at"] = time.monotonic()
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

    def _bg_task_command(self, entry: dict) -> str | None:
        """Look up the Bash command for a background task via its tool_use_id."""
        tuid = entry.get("tool_use_id")
        if not tuid:
            return None
        # Check active tools first, then history.
        tool = self.state.active_tools.get(tuid)
        if tool:
            return (tool.get("input") or {}).get("command")
        for h in self.state.tool_history:
            if h.get("tool_use_id") == tuid:
                return (h.get("input") or {}).get("command")
        return None

    def resolve_permission(self, allow: bool) -> None:
        """Called by the WS handler when the user responds to a permission prompt."""
        if self._pending_permission and not self._pending_permission.done():
            self._pending_permission.set_result(allow)

    def _handle_unknown_message(self, msg: Any) -> None:
        """Handle SDK messages that aren't SystemMessage/AssistantMessage/etc.

        The SDK sends ``RateLimitEvent`` as a top-level message type (NOT a
        SystemMessage subclass).  Detect it by class name so we don't need
        an import that may not exist in every SDK version.
        """
        cls_name = type(msg).__name__
        if cls_name == "RateLimitEvent":
            info = getattr(msg, "rate_limit_info", None)
            if info is not None:
                apply_rate_limit_info(self.state, info)
                log.debug("RateLimitEvent: status=%s", getattr(info, "status", "?"))

    # ------------------------------------------------------------------
    # run_turn() — process one complete turn
    # ------------------------------------------------------------------

    async def run_turn(self, prompt_text: str) -> tuple[str, bool]:
        """Send a prompt and process the full turn.

        Returns ``(assistant_text, was_interrupted)``.
        """
        state = self.state
        config = self.config

        # Diagnostic: log at the *very* top (before queue drain) so we
        # can prove this function actually started, distinct from the
        # later "enter" log that follows turn_active.set().  If a turn
        # ever appears to silently disappear, these two logs let us
        # localise where in run_turn the impossible state occurred.
        _prompt_preview = prompt_text.replace("\n", " ")[:80]
        _bridge_id = id(self) & 0xFFFF
        _ta_id = id(self.turn_active) & 0xFFFF
        log.info(
            "run_turn start: bridge=%04x ta=%04x ta.is_set=%s "
            "queue_size=%d state.busy=%s prompt=%r",
            _bridge_id, _ta_id, self.turn_active.is_set(),
            self.turn_msg_queue.qsize(), state.busy, _prompt_preview,
        )

        # Drain stale messages from the queue.
        drained = 0
        while not self.turn_msg_queue.empty():
            try:
                self.turn_msg_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break

        self.interrupt_event.clear()
        state.busy = True
        state.turn_started_at = time.monotonic()
        state.compact_during_last_turn = False
        state.current_turn_tool_seqs = []

        # Route SDK messages to turn_msg_queue.
        self.turn_active.set()

        # Diagnostic: every turn entry, so we can correlate the "silent
        # idle" symptom with the prompt that started the turn.  Confirm
        # turn_active really was set (defensive — a False here would
        # immediately explain the symptom).
        log.info(
            "run_turn enter: turns=%d drained=%d ta.is_set=%s prompt=%r",
            state.turns, drained, self.turn_active.is_set(), _prompt_preview,
        )

        await self.broadcast({"type": "turn_start", "prompt": prompt_text})

        # Send the prompt.
        await self.client.query(prompt_text)

        assistant_text = ""
        interrupted = False
        current_streaming_text = ""
        normal_completion = False

        try:
            while True:
                msg = await self.turn_msg_queue.get()

                if msg is DISPATCHER_DEAD:
                    raise RuntimeError("SDK dispatcher died mid-turn")

                # Interrupt poke — break out, emit a synthetic turn_end
                # so the UI shows the turn marker, and let the finally
                # block clear ``state.busy``.  The SDK may still send a
                # ResultMessage on its own afterwards, but we don't
                # depend on it.
                if msg is INTERRUPT_SENTINEL:
                    interrupted = True
                    normal_completion = True  # user-initiated
                    try:
                        duration = time.monotonic() - (
                            state.turn_started_at or time.monotonic()
                        )
                    except Exception:
                        duration = 0.0
                    await self.broadcast({
                        "type": "turn_end",
                        "subtype": "interrupted",
                        "duration": fmt_duration(duration),
                        "turns": state.turns,
                    })
                    break

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

                # ---- UserMessage (tool results OR injected prompts) ----
                elif isinstance(msg, UserMessage):
                    content = msg.content if hasattr(msg, "content") else None

                    # Synthetic prompts the harness fed to Claude
                    # (autonomous-loop ticks, /loop reschedules, system
                    # reminders) — surface them so the user can see
                    # what's actually entering the conversation.
                    injected = self._extract_injected_text(msg)
                    if injected is not None:
                        log.info("injected prompt during turn: %r", injected[:120])
                        await self._broadcast_injected_prompt(injected, during_turn=True)
                    elif isinstance(content, list):
                        # Diagnostic: log unclassified text-only UserMessages
                        # during a turn.  Tool results are expected here, but
                        # raw text without tool results indicates a harness-
                        # injected prompt whose prefix we don't yet match.
                        try:
                            has_tool_result = any(
                                isinstance(b, ToolResultBlock) for b in content
                            )
                            if not has_tool_result:
                                parts = []
                                for b in content:
                                    if isinstance(b, TextBlock):
                                        parts.append((b.text or "").strip())
                                snippet = "\n".join(p for p in parts if p)
                                if snippet.strip():
                                    log.warning(
                                        "UserMessage during turn: unclassified "
                                        "text (first 200 chars): %r",
                                        snippet[:200],
                                    )
                        except Exception:
                            pass
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
                    elif isinstance(content, str) and content.strip():
                        # String-typed UserMessage during a turn with text
                        # that didn't classify as injected.
                        log.warning(
                            "UserMessage during turn: unclassified string "
                            "(first 200 chars): %r",
                            content[:200],
                        )

                # ---- ResultMessage (end of turn) ----
                elif isinstance(msg, ResultMessage):
                    if current_streaming_text.strip():
                        assistant_text += current_streaming_text
                        current_streaming_text = ""

                    if msg.session_id:
                        state.session_id = msg.session_id

                    subtype = getattr(msg, "subtype", None) or "unknown"
                    state.last_result_subtype = subtype

                    # COMPACT-INDUCED ResultMessage handling.
                    #
                    # When the SDK runs an internal context compaction
                    # mid-turn, it closes the current turn with its own
                    # ResultMessage and then keeps streaming the
                    # post-compact response on a fresh implicit turn.
                    # We *must* break out here — waiting for a "second"
                    # ResultMessage to land on ``turn_msg_queue`` hangs
                    # run_turn (the post-compact stream arrives on the
                    # async path, not in the queue, so ``queue.get()``
                    # blocks indefinitely; observed as the 5-minute
                    # "stuck on working" symptom).
                    #
                    # Instead: exit run_turn cleanly, skip the
                    # ``turn_end`` broadcast so the UI doesn't flicker
                    # idle, and let ``_begin_ghost_turn_if_needed`` /
                    # ``_end_ghost_turn`` cover the post-compact stream.
                    was_compact = state.compact_during_last_turn
                    state.compact_during_last_turn = False

                    if not was_compact:
                        # Real end of turn — book metrics.
                        state.turns += 1
                        if hasattr(msg, "total_cost_usd") and msg.total_cost_usd:
                            state.total_cost_usd += msg.total_cost_usd
                        raw_usage = getattr(msg, "usage", None)
                        raw_model_usage = getattr(msg, "model_usage", None)
                        if isinstance(raw_usage, dict):
                            state.last_usage = raw_usage
                        elif isinstance(raw_model_usage, dict):
                            state.last_usage = raw_model_usage
                        ctx = extract_context_tokens(raw_usage, raw_model_usage)
                        if ctx:
                            state.context_tokens = ctx

                    log.info(
                        "run_turn ResultMessage: subtype=%s compact=%s "
                        "elapsed=%.1fs",
                        subtype, was_compact,
                        time.monotonic() - (state.turn_started_at or time.monotonic()),
                    )

                    # Reload title.
                    if state.session_id:
                        state.session_title = read_session_title(state.session_id)

                    # Rate limit info may be on ResultMessage too.
                    rate_info = getattr(msg, "rate_limit_info", None)
                    if rate_info is not None:
                        apply_rate_limit_info(state, rate_info)

                    # Only broadcast ``turn_end`` for *real* turn ends.
                    # A compact-induced ResultMessage will be followed
                    # by a ghost turn that owns the user-visible
                    # turn_end — broadcasting one here would produce a
                    # premature "turn ended" marker in the UI.
                    if not was_compact:
                        duration = time.monotonic() - (
                            state.turn_started_at or time.monotonic()
                        )
                        await self.broadcast({
                            "type": "turn_end",
                            "subtype": subtype,
                            "duration": fmt_duration(duration),
                            "turns": state.turns,
                        })
                    normal_completion = True
                    break

                # ---- Unknown (e.g. RateLimitEvent) ----
                else:
                    self._handle_unknown_message(msg)

                # Check for interrupt.
                if self.interrupt_event.is_set():
                    interrupted = True
                    if self.client:
                        try:
                            await self.client.interrupt()
                        except Exception:
                            pass
                    normal_completion = True  # user-initiated, not a silent drop
                    break

        finally:
            # Cleanup ALWAYS runs first — even on CancelledError or any
            # other BaseException.  Doing it before any further awaits
            # ensures state.busy can never get stuck True when the loop
            # has stopped.
            exit_exc = sys.exc_info()[1]
            # Diagnostic: log entry into finally *before* any cleanup,
            # so that even if a subsequent line throws (unlikely but
            # possible) we still have proof the finally ran.  Critical
            # for diagnosing "run_turn enter without run_turn exit"
            # cases where the cleanup path is suspected.
            log.info(
                "run_turn finally: normal_completion=%s exit_exc=%s "
                "ta.is_set=%s",
                normal_completion,
                None if exit_exc is None else type(exit_exc).__name__,
                self.turn_active.is_set(),
            )
            self.turn_active.clear()
            state.busy = False
            state.turn_started_at = None
            # Clear foreground active tools (bg tasks survive).
            state.active_tools.clear()

            # Log the exit path so we have concrete evidence when this
            # goes wrong in production (the "turn dies silently after
            # hours" symptom needs a paper trail).
            if normal_completion:
                log.info("run_turn exit: normal (turns=%d)", state.turns)
            elif exit_exc is not None:
                log.warning(
                    "run_turn exit: exception %s: %s",
                    type(exit_exc).__name__, exit_exc,
                )
            else:
                log.warning("run_turn exit: silent (no exception, no ResultMessage)")

            # Emit a synthetic turn_end so the user sees that the turn
            # ended even when there was no ResultMessage.  Wrapped in
            # BaseException so a CancelledError mid-broadcast can't
            # also swallow the warning silently.
            if not normal_completion:
                exc_label = (
                    f"{type(exit_exc).__name__}: {exit_exc}"
                    if exit_exc is not None else "no exception"
                )
                try:
                    duration = time.monotonic() - (state.turn_started_at or time.monotonic())
                except Exception:
                    duration = 0.0
                # Broadcast each piece independently — a failure on one
                # shouldn't take the other down with it.
                try:
                    await self.broadcast({
                        "type": "turn_end",
                        "subtype": "interrupted",
                        "duration": fmt_duration(duration),
                        "turns": state.turns,
                    })
                except BaseException as bx:
                    log.warning("synthetic turn_end broadcast failed: %r", bx)
                try:
                    await self.broadcast({
                        "type": "system_msg",
                        "subtype": "warning",
                        "data": {"message": (
                            "Turn ended without a result message "
                            f"({exc_label}). The SDK may have disconnected, "
                            "errored, or the dispatcher died."
                        )},
                    })
                except BaseException as bx:
                    log.warning("synthetic warning broadcast failed: %r", bx)

            # Push a fresh status_update so the UI reflects the new
            # state.busy == False immediately.  Without this, the
            # toolbar can stay stuck on "working" after an interrupt or
            # after a turn that completed via the synthetic-turn_end
            # path — the frontend only refreshes its busy-class from
            # status_update messages, not from turn_end.
            try:
                await self.broadcast({
                    "type": "status_update",
                    "status": state_to_status_dict(state, self.config),
                    "panels": state_to_panels_dict(state),
                })
            except BaseException as bx:
                log.warning("post-turn status_update broadcast failed: %r", bx)

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

        # --- Connect (with auto-retry) ---
        _connect_delay = 2.0
        _MAX_CONNECT_DELAY = 30.0
        _MAX_CONNECT_ATTEMPTS = 10
        _connect_attempt = 0
        while not self.stop_event.is_set():
            try:
                await self.connect()
                if _connect_attempt:
                    log.info("SDK connected after %d retries", _connect_attempt)
                    await self.broadcast({
                        "type": "system_msg",
                        "subtype": "info",
                        "data": {"message": "SDK connected."},
                    })
                break
            except Exception as exc:
                _connect_attempt += 1
                log.error("SDK connect failed (attempt %d): %s", _connect_attempt, exc)

                if _connect_attempt >= _MAX_CONNECT_ATTEMPTS:
                    await self.broadcast({
                        "type": "system_msg",
                        "subtype": "error",
                        "data": {"message": f"SDK connection failed after {_connect_attempt} attempts: {exc}. Send a message to retry."},
                    })
                    # Fall back to manual retry.
                    while not self.stop_event.is_set():
                        kind, payload = await self.event_queue.get()
                        if kind in ("quit", "force-quit"):
                            return
                        if kind == "message":
                            _connect_attempt = 0
                            _connect_delay = 2.0
                            self.event_queue.put_nowait((kind, payload))
                            break  # restart auto-retry loop
                    continue

                await self.broadcast({
                    "type": "system_msg",
                    "subtype": "error",
                    "data": {"message": f"SDK connection failed (attempt {_connect_attempt}/{_MAX_CONNECT_ATTEMPTS}), retrying in {_connect_delay:.0f}s\u2026"},
                })
                # Drain any user messages that arrived during the wait
                # so they're not lost.
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=_connect_delay,
                    )
                    return  # stop_event was set
                except asyncio.TimeoutError:
                    pass
                _connect_delay = min(_connect_delay * 2, _MAX_CONNECT_DELAY)

        # --- Initial prompt ---
        # Messages sent during connect go to state.queued_prompts (so
        # they appear in the queue panel).  Check those first.
        next_prompt: str | None = None
        if config.initial_prompt:
            next_prompt = config.initial_prompt
        elif state.queued_prompts:
            next_prompt = state.queued_prompts.popleft()
            state.queue_editing_index = None
            await self.broadcast({"type": "user_message", "content": next_prompt})
            await self.broadcast({
                "type": "queue_update",
                "queue": [{"index": i, "text": t}
                          for i, t in enumerate(state.queued_prompts)],
            })
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
            # Echo the first prompt to chat IF the frontend didn't.
            # Messages that land on the event_queue (rather than
            # ``queued_prompts``) normally rely on the frontend's
            # optimistic echo in ``send()`` — but that echo is skipped
            # when ``_isBusy`` is true, and ``_isBusy`` stays true from
            # the initial ``connecting=True`` status_update until
            # something else flips it.  A prompt the user typed during
            # connecting can therefore arrive here with no chat echo at
            # all, making it look like the turn started spontaneously.
            # The frontend stamps each message with ``client_echoed`` so
            # we can echo here only when needed (avoiding duplicates
            # when ``_isBusy`` was correctly false at send time).
            if next_prompt is not None and not state.initial_prompt_client_echoed:
                await self.broadcast({
                    "type": "user_message",
                    "content": next_prompt,
                })
            # One-shot: clear so subsequent paths can't accidentally
            # re-read this flag.
            state.initial_prompt_client_echoed = None

        # --- Turn loop ---
        while next_prompt is not None and not self.stop_event.is_set():
            assistant_text = ""
            interrupted = False
            self._is_auto_turn = next_prompt == state.continue_prompt

            try:
                assistant_text, interrupted = await self.run_turn(next_prompt)
            except asyncio.CancelledError:
                # Cancellation must propagate (shutdown / reload), but
                # log it loudly first.  Without this the worker dies
                # silently, the dispatcher keeps consuming SDK output
                # into _handle_async_message, and the UI is left
                # showing "idle" while messages stream in.
                log.warning("run_turn cancelled — worker_loop exiting")
                raise
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
            if state.queue_editing_index == 0:
                # First prompt is being edited in the UI — wait for the
                # user to finish before sending it.
                return await self._await_next_prompt()
            prompt = state.queued_prompts.popleft()
            state.queue_editing_index = None  # clear stale editing state
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
                "data": {"message": f"Auto-compacting (ctx {fmt_tok(state.context_tokens)} >= {fmt_tok(compact_at)})..."},
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
        # Surface the synthetic continue prompt to the frontend as an
        # injected prompt so the user sees a collapsed box for it, the
        # same way compaction's harness-injected prompt is rendered.
        # Without this broadcast the auto-continue loop runs invisibly
        # and the user only sees Claude's response with no indication
        # of the prompt that triggered it.
        await self._broadcast_injected_prompt(
            state.continue_prompt, during_turn=False,
        )
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
                # Queue edit finished — send the first queued prompt.
                if payload == "queue-edit-done" and self.state.queued_prompts:
                    prompt = self.state.queued_prompts.popleft()
                    self.state.needs_user_attention = None
                    await self.broadcast({
                        "type": "user_message",
                        "content": prompt,
                    })
                    return prompt
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
                    # Same as the grace-window path in _between_turns:
                    # surface the synthetic continue prompt so the user
                    # sees a collapsed injected-prompt box for the
                    # auto-resume turn.
                    await self._broadcast_injected_prompt(
                        self.state.continue_prompt, during_turn=False,
                    )
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
        # Poke the run_turn loop awake.  Without this, an interrupt
        # while the SDK isn't streaming leaves ``run_turn`` blocked on
        # ``await turn_msg_queue.get()`` indefinitely — ``state.busy``
        # never clears and the toolbar stays stuck on "working" even
        # though the user already saw the "Turn interrupted." system
        # message.  The sentinel lets the loop tick once and notice
        # ``interrupt_event`` is set.
        if self.turn_active.is_set():
            try:
                self.turn_msg_queue.put_nowait(INTERRUPT_SENTINEL)
            except Exception:
                pass
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
