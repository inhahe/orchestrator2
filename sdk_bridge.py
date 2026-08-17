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
import traceback
from pathlib import Path
from typing import Any, Callable, Awaitable

import proc_guard

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
    DISPATCHER_DEAD,
    INTERRUPT_SENTINEL,
    WAKEUP_DEFAULT_DELAY,
    WAKEUP_MAX_DELAY,
    WAKEUP_MIN_DELAY,
    WAKEUP_RESOLVED_PROMPT,
    WAKEUP_SENTINELS,
    Config,
    default_compact_at,
    model_context_window,
)
from state import (
    State,
    apply_rate_limit_info,
    detect_account_info,
    extract_context_tokens,
    fmt_duration,
    fmt_tok,
    humanize_size,
    in_bg_wait,
    ring_bell,
    state_to_panels_dict,
    state_to_status_dict,
)
from session import (
    _classify_user_text,
    check_session_integrity,
    describe_integrity_problem,
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


# How long a shutdown waits for a cancelled task to actually finish.  Generous
# enough that a task unwinding through a slow ``finally`` still gets to log its
# own exit, short enough that a wedged one can't hold a subprocess hostage.
CANCEL_JOIN_TIMEOUT = 10.0


async def cancel_and_join(task: asyncio.Task, what: str) -> None:
    """Cancel *task*, wait for it to finish, and absorb **its** failure — but never
    our own cancellation.

    The obvious spelling of this is a trap::

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    That ``except`` is meant to swallow the awaited task's ``CancelledError``, but
    ``await task`` is also where a ``CancelledError`` aimed at *this* coroutine
    arrives — so the caller silently swallows its own cancellation and carries on to
    its next ``await``.  Under an anyio task group (every Starlette request /
    WebSocket handler runs in one) the group then re-delivers cancellation on *every*
    event-loop iteration forever: the coroutine keeps getting cancelled at whatever
    await it reached next, never finishes, never leaves ``TaskGroup._tasks``, and
    ``anyio._backends._asyncio.CancelScope._deliver_cancellation`` re-arms itself via
    ``call_soon`` each pass.  The loop never sleeps and the process pins one CPU core
    at 100% indefinitely — observed for 60+ CPU-hours before being caught (see
    known-issues.md).

    ``asyncio.wait`` is the fix: it reports the task's outcome instead of re-raising
    it, so the awaitee's cancellation is absorbed while a cancellation aimed at us
    still propagates out of the ``await`` normally.

    The wait is **bounded**.  A task can decline to die — it need only reach an
    await inside a ``finally`` that never completes — and every caller here is a
    shutdown path, where blocking forever on that trades a stuck coroutine for a
    leaked subprocess.  On expiry we log the task's own stack (the one piece of
    evidence that is otherwise unrecoverable: a parked coroutine appears in no
    thread dump, so ``py-spy`` shows only an idle event loop) and carry on.
    """
    task.cancel()
    done, pending = await asyncio.wait({task}, timeout=CANCEL_JOIN_TIMEOUT)
    if pending:
        stack = "".join(traceback.format_stack(
            getattr(task.get_coro(), "cr_frame", None))) or "<no frame>"
        log.error(
            "%s did not stop within %gs of being cancelled — continuing "
            "shutdown without it.  It is parked at:\n%s",
            what, CANCEL_JOIN_TIMEOUT, stack,
        )
        return
    for t in done:
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                log.debug("%s exited with %r", what, exc)


# Hard ceiling on a single ``client.connect()`` call.  Spawning the Claude CLI
# and completing its init handshake normally takes a few seconds; occasionally
# it hangs outright (the subprocess starts but never finishes the handshake),
# which would otherwise leave the UI stuck on "connecting…" forever.  When this
# elapses we tear down the half-open client and let ``worker_loop``'s existing
# connect-retry loop try again — a fresh spawn almost always succeeds.
CONNECT_TIMEOUT = 45.0

# Broadcaster type: async function that sends a dict to all WS clients.
Broadcaster = Callable[[dict[str, Any]], Awaitable[None]]

# --- Why output can continue after an interrupt ---------------------------
#
# From a report: "often when i hit ctrl-c to interrupt a turn, it says it's
# interrupted but then it keeps working and outputting text, and i have to hit
# ctrl-c once or maybe twice more to get it to stop."
#
# Seen in orchestrator2.log (pid 6760, bridge=6990)::
#
#     23:05:11,152  UserMessage during turn: '[Request interrupted by user for tool use]'
#     23:05:11,153  run_turn exit: normal (turns=2)
#     23:05:22,033  async AssistantMessage between turns: ... blocks=['ThinkingBlock']
#     23:05:22,033  ghost turn begin: SDK streaming without active run_turn
#     23:05:24,087  async UserMessage between turns: '[Request interrupted by user]'
#     23:05:24,089  ghost turn end: subtype=error_during_execution compact=False elapsed=2.1s
#
# This is NOT the SDK ignoring the interrupt, and it is not ours to suppress.
# The matching session JSONL shows a *background Bash task* completing 4.2 s
# after the Ctrl-C and enqueueing a <task-notification>; the CLI then fed it to
# the model, which is what produced the 23:05:22 stream.  The CLI builds its
# abortController *per dequeued command* (print.ts:2133) and the SDK `interrupt`
# control request only calls abortController.abort() — it never clears the
# command queue, so anything dequeued afterwards runs with a fresh controller.
# print.ts:2010-2013 says feeding notifications to the model this way "matches
# TUI behavior where useQueueProcessor always feeds notifications to the model
# regardless of coordinator mode", so interactive Claude Code does the same.
#
# The delay is therefore the background task's own runtime plus the API round
# trip, not interrupt latency — measured at 2.1 s / 8.5 s / 10.9 s across the
# three resurrections in 20 logged interrupts.  Discarding that output would
# hide a legitimate result that the CLI has already written to the session
# JSONL, so the live view would disagree with the history on reload.  We render
# it as a normal ghost turn; see _begin_ghost_turn_if_needed.

# Matches the CLI's inline API-error banner, e.g.
#   "API Error: 400 {"type":"error","error":{...}}"
# The captured group is the HTTP status code.  IMPORTANT: this is matched only
# against the *start* of the turn output (see detect_api_error) — the harness
# emits this banner as the entire/leading assistant response when an API call
# fails, so anchoring it to the start avoids a false positive when the *model*
# merely quotes the string "API Error: 401" mid-prose (e.g. a debugging chat).
_API_ERROR_RE = re.compile(r"API Error:\s*(\d{3})\b")

# Matches authentication failures the CLI prints to *stderr* at connect time,
# e.g. "Failed to authenticate: OAuth session expired and could not be
# refreshed" or "Invalid authentication credentials".  Unlike a per-turn 401
# these never reach a ResultMessage — the connect itself dies — so they need a
# separate signal so `/login` can auto-recover them too.  Safe to match as a
# loose substring: stderr is the CLI subprocess's output, never model prose.
_AUTH_FAIL_RE = re.compile(
    r"OAuth session expired"
    r"|could not be refreshed"
    r"|Failed to authenticate"
    r"|Invalid authentication credentials"
    r"|authentication_error"
    r"|Not logged in"
    r"|Please run /login",
    re.IGNORECASE,
)

# Matches a codeless auth-refusal banner the CLI emits as a turn's assistant
# text (no "API Error: NNN") when the stored session is unusable, e.g.
#   "Not logged in · Please run /login"
# ANCHORED to the start of the (stripped) turn output on purpose: a genuine
# refusal is the whole leading response, whereas the model discussing these
# phrases in a debugging conversation never *begins* its entire response with
# them.  Matching loosely here caused false "not authed" flags.
_AUTH_BANNER_RE = re.compile(
    r"\s*(?:"
    r"Not logged in\b"
    r"|Please run [`']?/?login"
    r"|Failed to authenticate\b"
    r"|OAuth session expired"
    r"|Invalid authentication credentials"
    r")",
    re.IGNORECASE,
)


def detect_api_error(msg: Any, assistant_text: str) -> tuple[str | None, str | None]:
    """Detect a turn that ended in an API error.

    The Claude CLI surfaces API failures (rate-limit 429s, malformed-request
    400s, etc.) as inline assistant text like ``API Error: 400 {...}`` and
    then closes the turn — often with ``subtype="success"`` because, from the
    CLI's point of view, it *did* emit output and return control cleanly.
    ``ResultMessage.is_error`` is likewise unreliable for these.

    We therefore check the *start* of the visible assistant text (and
    ``ResultMessage.result`` as a fallback) for the error banner.  The banner is
    matched only at the leading edge of the output — a real failure makes the
    banner the whole/leading response, so this avoids flagging a turn where the
    model merely *quotes* "API Error: 401" mid-prose (which is exactly what
    happens in a chat about auth bugs).

    Returns ``(status_code, signature)`` where ``signature`` is a short,
    stable string identifying the error (used for repeat/loop detection), or
    ``(None, None)`` when the turn did not end in an API error.
    """
    haystacks = []
    if assistant_text:
        haystacks.append(assistant_text)
    result_text = getattr(msg, "result", None)
    if isinstance(result_text, str):
        haystacks.append(result_text)

    for text in haystacks:
        # Anchored at the start of the stripped text (``.match``) so a quoted
        # occurrence deeper in a long response doesn't count.
        m = _API_ERROR_RE.match(text.lstrip())
        if not m:
            continue
        code = m.group(1)
        # Build a stable signature from the code plus the API's own error
        # message, so an identical fault repeating every turn collapses to
        # the same key (used to detect an inescapable poisoned-history loop).
        sig = code
        detail = re.search(r'"message"\s*:\s*"([^"]{0,160})"', text)
        if detail:
            sig = f"{code}:{detail.group(1)}"
        return code, sig
    return None, None


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
        # Set whenever no interrupt wind-down is outstanding.  Cleared only
        # when we interrupt a stream that *no run_turn is consuming* (a ghost
        # turn), because then nobody will absorb the CLI's terminating
        # ResultMessage — and a turn started before it lands would eat it and
        # die instantly.  See _await_interrupt_settled().
        self._interrupt_settled = asyncio.Event()
        self._interrupt_settled.set()

        self._dispatcher_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._initial_resume_id: str | None = None
        self._pending_permission: asyncio.Future | None = None

        # ScheduleWakeup heartbeat.  When the model calls the ScheduleWakeup
        # tool we arm this timer; on fire it re-injects the scheduled prompt as
        # a fresh turn.  A new turn (or stop) cancels any pending timer.
        self._wakeup_task: asyncio.Task | None = None
        self._wakeup_fire_at: float | None = None  # monotonic deadline, for logs
        # Generation counter for dispatcher tasks.  Each connect() bumps
        # this so we can tell live dispatchers apart in logs and detect
        # orphaned ones (stale tasks that survived a reconnect).
        self._dispatcher_gen = 0
        # Session-integrity warning is announced once per bridge, not once per
        # reconnect — the condition is permanent and unfixable mid-session.
        self._integrity_warned = False

    # ------------------------------------------------------------------
    # SDK options
    # ------------------------------------------------------------------

    def _on_sdk_stderr(self, line: str) -> None:
        """Forward the CLI subprocess's stderr to our log.

        The SDK invokes this for each stderr line emitted by the underlying
        ``claude`` CLI.  Without it, fatal subprocess errors only say
        "Check stderr output for details" — which we can't check after the
        fact.  Routing it here makes those details show up in orchestrator2's
        log file.
        """
        line = (line or "").rstrip()
        if line:
            log.warning("[cli stderr] %s", line)
            # A connect-time auth failure (dead OAuth session) only ever shows
            # up here — it never reaches a turn's ResultMessage.  Flag it so
            # `/login` forces a re-auth instead of reporting "already signed in".
            if _AUTH_FAIL_RE.search(line):
                self.state.auth_error = True

    def _make_options(self, resume_id: str | None = None) -> ClaudeAgentOptions:
        """Build ``ClaudeAgentOptions`` from config + state."""
        kwargs: dict[str, Any] = {
            "permission_mode": self.config.permission_mode,
            "cwd": self.config.cwd,
            "setting_sources": ["user", "project", "local"],
            "max_buffer_size": 10 * 1024 * 1024,
            # Pipe the CLI subprocess's stderr into our log so fatal errors
            # ("Check stderr output for details") are actually recoverable.
            "stderr": self._on_sdk_stderr,
            # Opt into session_state_changed events so we learn when the
            # session blocks on a pending action (AskUserQuestion,
            # ExitPlanMode, or — in ask mode — a tool-permission prompt) and
            # can ring the 'requires-action' bell.  Merged onto the inherited
            # environment by the SDK, so this adds the one var without
            # clobbering anything.  The idle/running events it also enables
            # are suppressed in _handle_system_message.
            "env": {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"},
        }

        # Cross-account hub support: if the session's config specifies a
        # non-default CLAUDE_CONFIG_DIR, inject it into the subprocess env
        # so the bridge connects with the correct account.
        if getattr(self.config, "config_dir", None):
            kwargs["env"]["CLAUDE_CONFIG_DIR"] = self.config.config_dir

        # Opt-in workaround for the bundled CLI's cache_control TTL-ordering
        # bug (see Config.disable_prompt_cache): turning off prompt caching
        # stops the CLI emitting any cache_control blocks, so a mid-session
        # 1h↔5m TTL flip can't produce the "must not come after" API 400.
        if getattr(self.config, "disable_prompt_cache", False):
            kwargs["env"]["DISABLE_PROMPT_CACHING"] = "1"

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

        # MCP config — lets the orchestrator act as an MCP client.  The SDK
        # option is named ``mcp_servers`` and accepts a dict of server configs,
        # a path to a JSON file, or an inline JSON string; all forms are passed
        # through to the CLI's ``--mcp-config``.
        if self.config.mcp_config:
            kwargs["mcp_servers"] = self.config.mcp_config

        # System prompt extension.
        if self.config.append_system_prompt:
            kwargs["append_system_prompt"] = self.config.append_system_prompt

        # Permission callback.  Always register it (when the SDK exposes the
        # PermissionResult types) so we can intercept interactive tools like
        # AskUserQuestion — the CLI routes those through can_use_tool *even in
        # bypassPermissions mode* (step 1e of its permission pipeline), and
        # without a callback the SDK raises "canUseTool callback is not
        # provided" and the tool fails.  For ordinary tools the callback only
        # fires under a real permission mode; bypass auto-allows them before
        # ever calling us (see _handle_tool_permission).
        if PermissionResultAllow is not None:
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

        # Refuse to become the second agent on a session someone else is
        # already driving.  The job object (proc_guard) stops *us* leaking
        # orphans, but it can't retroactively clean up one left by a server
        # that died before it existed, nor a second hub, nor a `claude
        # --resume` the user ran in a terminal.  Two agents on one session
        # share a session JSONL and a working tree and commit over each other.
        resume_target = getattr(options, "resume", None)
        if resume_target and not getattr(
                self.config, "allow_duplicate_session", False):
            # psutil scan touches every process — keep it off the event loop.
            dupes = await asyncio.to_thread(
                proc_guard.find_foreign_claude_for_session, resume_target)
            if dupes:
                log.error("refusing to resume %s — already running as PID(s) %s",
                          resume_target, [p.pid for p in dupes])
                raise proc_guard.DuplicateSessionError(resume_target, dupes)

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
            await cancel_and_join(old_task, "orphan dispatcher")

        self.client = ClaudeSDKClient(options=options)

        # Re-read account details each connect so a fresh /login (then /connect)
        # updates the displayed email/plan without a restart.  Scoped to this
        # runtime's account dir — a cross-account session must read its own
        # CLAUDE_CONFIG_DIR, not the hub process's env account.
        try:
            self.state.account = detect_account_info(getattr(self.config, "config_dir", None))
        except Exception:
            pass

        self.state.connecting = True
        started = time.monotonic()
        self.state.connect_started_at = started
        await self.broadcast({"type": "status_update",
                              "status": state_to_status_dict(self.state, self.config)})

        try:
            # Bound the connect so a hung CLI handshake can't wedge the UI on
            # "connecting…" indefinitely.  On timeout/failure, tear down the
            # half-open client so the retry starts from a clean slate (a
            # timed-out connect can leave a dead subprocess and ``self.client``
            # pointing at an unusable client).
            try:
                await asyncio.wait_for(self.client.connect(), timeout=CONNECT_TIMEOUT)
            except BaseException as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    log.error("SDK connect timed out after %.0fs — retrying",
                              CONNECT_TIMEOUT)
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                raise
        finally:
            self.state.connecting = False
            self.state.connect_started_at = None

        # The CLI handshake completed, so any prior auth failure is resolved
        # (a dead OAuth session fails the connect above rather than reaching
        # here).  Clear the sticky flag; a fresh 401 on the first turn — or an
        # auth line on stderr — will re-set it.
        self.state.auth_error = False

        # A fresh CLI is not compacting, whatever the last one was doing when
        # it died.
        self.state.cli_status = None
        self.state.cli_status_started_at = None

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

        # Every connect is a *resume*, and a resume is the moment a broken
        # parentUuid chain silently costs the model its history.  Check it here
        # rather than at startup only: the reports that led to this ("claude
        # seemed not to remember any of the conversation that just happened")
        # all followed a `/model` switch, which reconnects.
        asyncio.create_task(self._report_session_integrity(),
                            name="session-integrity")

    async def _report_session_integrity(self) -> None:
        """Warn if the session we just resumed can't reach its own history."""
        sid = self.state.session_id
        if not sid:
            return
        try:
            report = await asyncio.to_thread(
                check_session_integrity, sid,
                getattr(self.config, "config_dir", None),
            )
        except Exception as exc:                     # diagnostics must not break connect
            log.warning("session integrity check failed: %r", exc)
            return
        if not report:
            return
        # Say it once per bridge — a reconnect loop shouldn't spam the
        # transcript with a condition the user can't fix mid-session.
        already = self._integrity_warned
        self._integrity_warned = True
        log.error(
            "session %s history broken: chain=%s of %s (stranded=%s) "
            "hole_at=%s truncated_at=%s branches=%s",
            sid[:12], report.get("chain"), report.get("total"),
            report.get("stranded"), report.get("hole_at"),
            report.get("truncated_at"), report.get("branches"),
        )
        if already:
            return
        await self.broadcast({
            "type": "system_msg",
            "subtype": "error",
            "data": {"message": describe_integrity_problem(report)},
        })

    def _cli_pid(self) -> int | None:
        """PID of our ``claude.exe``, or None if the SDK doesn't expose it.

        Reaches through SDK-private attributes, so it is written to fail soft:
        if a future SDK reshuffles them we lose MCP cleanup, which is a leak,
        not a crash.
        """
        proc = getattr(getattr(self.client, "_transport", None), "_process", None)
        pid = getattr(proc, "pid", None)
        return pid if isinstance(pid, int) and pid > 0 else None

    async def disconnect(self) -> None:
        """Shut down client and dispatcher, and make sure the CLI is dead.

        Reaps the CLI's own children.  The SDK stops ``claude.exe`` with
        ``terminate()``/``kill()``; on Windows that kills only that one
        process, orphaning the stdio MCP servers it spawned.  The server's job
        object would eventually collect them — but only when the *server*
        exits, so a long-lived hub cycling through sessions (idle teardown,
        ``/cwd``, reconnects) accumulates a full MCP stack per dead session.

        It also **verifies** that ``claude.exe`` itself is gone rather than
        assuming it.  This used to trust the SDK entirely and swallow any
        failure with a bare ``except Exception: pass``, so a subprocess that
        outlived its teardown did so in total silence — and a surviving CLI is
        not a stray handle, it is a live agent still resumed on the session,
        still able to edit files and commit.  Two of them on one session is the
        exact hazard :mod:`proc_guard` exists to prevent, and the guard cannot
        catch this case because the orphan is inside our own process tree.
        """
        if self._dispatcher_task and not self._dispatcher_task.done():
            await cancel_and_join(self._dispatcher_task, "dispatcher")
        if not self.client:
            return

        # Snapshot while the CLI is still alive — once it exits, the parent
        # links needed to find its children are gone, and its own pid may be
        # recycled onto an unrelated process.
        cli_pid = self._cli_pid()
        kids: list[proc_guard.Descendant] = []
        cli: proc_guard.Descendant | None = None
        if cli_pid is not None:
            kids = await asyncio.to_thread(
                proc_guard.snapshot_descendants, cli_pid)
            cli = await asyncio.to_thread(proc_guard.snapshot_process, cli_pid)

        try:
            await self.client.disconnect()
        except Exception:
            # Not fatal — the backstop below still kills the process — but
            # never silent: this is the only place that would explain a CLI
            # that outlived its session.
            log.warning("SDK disconnect failed for claude.exe pid %s",
                        cli_pid, exc_info=True)
        finally:
            self.client = None

        if cli is not None:
            n = await asyncio.to_thread(proc_guard.reap_descendants, [cli])
            if n:
                log.warning(
                    "claude.exe pid %s survived SDK disconnect — killed it "
                    "directly", cli_pid,
                )
        if kids:
            n = await asyncio.to_thread(proc_guard.reap_descendants, kids)
            if n:
                log.info("reaped %d orphaned MCP/tool process(es) left by "
                         "claude.exe pid %s", n, cli_pid)

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
            # False means a live run_turn owns this message.
            if not await self._begin_ghost_turn_if_needed():
                return

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
                    self._maybe_arm_wakeup(name, inp)
                    seq = register_tool_use(state, block.id, name, inp)
                    todos_changed = (
                        name == "TodoWrite" and isinstance(inp.get("todos"), list)
                    )
                    if todos_changed:
                        state.current_todos = inp["todos"]
                    ws_msg = format_tool_use_msg(
                        name, inp, block.id, seq,
                        is_background=bool(is_bg),
                    )
                    await self.broadcast(ws_msg)
                    # Refresh the todos panel immediately instead of waiting
                    # for the next ~2s status ticker.
                    if todos_changed:
                        await self._broadcast_panels()
                elif ThinkingBlock and isinstance(block, ThinkingBlock):
                    thinking_text = getattr(block, "thinking", "") or ""
                    signature = getattr(block, "signature", "") or ""
                    # See the matching branch in run_turn(): newer models
                    # return encrypted reasoning (signature, empty text).
                    if thinking_text.strip():
                        seq = register_thinking(state, thinking_text)
                        await self.broadcast({
                            "type": "thinking",
                            "seq": seq,
                            "content": thinking_text,
                        })
                    elif signature:
                        seq = register_thinking(state, "", encrypted=True)
                        await self.broadcast({
                            "type": "thinking",
                            "seq": seq,
                            "content": "",
                            "encrypted": True,
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
                if not await self._begin_ghost_turn_if_needed():
                    return
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
            if not await self._begin_ghost_turn_if_needed():
                return
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

            # This is the terminator a post-interrupt turn is waiting on:
            # the stream that was running has now stopped, so it's safe to
            # start a new turn without it inheriting this result.
            self._interrupt_settled.set()

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

    async def _broadcast_panels(self) -> None:
        """Push a fresh status_update so panels (todos, tools, bg) refresh now.

        Panels otherwise only refresh on the ~2s server-side ticker, which
        makes fast-changing panels like the todos/plan pane feel laggy — a
        TodoWrite mid-turn wouldn't show up until the next tick.  Call this
        right after mutating panel-backing state to reflect it immediately.
        """
        try:
            await self.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(self.state, self.config),
                "panels": state_to_panels_dict(self.state),
            })
        except Exception as exc:
            log.warning("panels status_update broadcast failed: %r", exc)

    # ------------------------------------------------------------------
    # ScheduleWakeup heartbeat
    # ------------------------------------------------------------------
    #
    # The model paces self-directed autonomous loops with the ``ScheduleWakeup``
    # tool ("resume in N seconds with this prompt").  Claude Code's interactive
    # REPL owns that timer, but the CLI subprocess we drive in streaming mode
    # never re-injects a scheduled prompt on its own.  Without the code below,
    # ScheduleWakeup is a silent no-op: the loop only ever continued by accident
    # (compaction continuations), so it stalled whenever the model was idle —
    # most visibly while waiting on a background task.  We close that gap by
    # arming our own timer and feeding the scheduled prompt back through the
    # event queue as a normal turn — which works even while bg tasks run, since
    # ``_await_next_prompt`` (the bg-wait park point) consumes ``message``
    # events.

    def _maybe_arm_wakeup(self, name: str, inp: dict) -> None:
        """If *name* is ``ScheduleWakeup``, arm/replace the wakeup timer.

        Reads ``delaySeconds`` and ``prompt`` from the tool input, clamps the
        delay to the documented [60, 3600] range, resolves the autonomous-loop
        sentinels to a concrete continue prompt, and (re)arms the timer.  Any
        previously pending wakeup is cancelled — the latest ScheduleWakeup call
        wins, matching the tool's "call again to reschedule" semantics.
        """
        if not getattr(self.config, "wakeup_enabled", True):
            return
        if name != "ScheduleWakeup":
            return

        raw_delay = inp.get("delaySeconds", WAKEUP_DEFAULT_DELAY)
        try:
            delay = float(raw_delay)
        except (TypeError, ValueError):
            delay = WAKEUP_DEFAULT_DELAY
        if delay != delay:  # NaN guard
            delay = WAKEUP_DEFAULT_DELAY
        delay = max(WAKEUP_MIN_DELAY, min(WAKEUP_MAX_DELAY, delay))

        raw_prompt = inp.get("prompt")
        prompt = raw_prompt if isinstance(raw_prompt, str) else ""
        if prompt.strip() in WAKEUP_SENTINELS or not prompt.strip():
            resolved = WAKEUP_RESOLVED_PROMPT
        else:
            resolved = prompt

        self._arm_wakeup(delay, resolved)

    def _arm_wakeup(self, delay: float, prompt: str) -> None:
        """(Re)start the wakeup timer for *delay* seconds carrying *prompt*."""
        self._cancel_wakeup()
        self._wakeup_fire_at = time.monotonic() + delay
        self._wakeup_task = asyncio.create_task(
            self._wakeup_timer(delay, prompt), name="schedule-wakeup",
        )
        log.info("wakeup armed: delay=%.0fs prompt=%r", delay, prompt[:80])

    def _cancel_wakeup(self) -> None:
        """Cancel any pending wakeup timer (idempotent).

        **Never cancels the calling task.**  ``_wakeup_timer`` re-arms itself
        when it fires mid-turn, and ``_arm_wakeup`` starts by cancelling the
        pending timer — which at that moment *is* the caller.  Self-cancelling
        would leave the task marked for cancellation, so anything added after
        the re-arm would silently never run.  That exact shape (a task
        cancelling itself out of its own callback) cost this hub 19 live CLI
        subprocesses via ``server._cancel_idle_timer``; it is not a mistake
        worth making twice.
        """
        t = self._wakeup_task
        self._wakeup_task = None
        self._wakeup_fire_at = None
        if t is None or t.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:      # no running loop
            current = None
        if t is not current:
            t.cancel()

    async def _wakeup_timer(self, delay: float, prompt: str) -> None:
        """Sleep *delay* seconds, then inject *prompt* as a fresh turn."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self.stop_event.is_set():
            return

        # A turn is still running: the loop is not stalled, so it needs no
        # nudge — and injecting one would put "Resume work now…" into a
        # conversation that is already mid-thought, as a user message the model
        # must then obey.  Observed after an auto-compaction: the model
        # scheduled a wakeup, the CLI compacted, the post-compact continuation
        # restarted the work by itself, and the wakeup then fired into that
        # live turn.  Defer instead of dropping — a turn that ends without the
        # model re-arming would otherwise strand the loop for good.
        if self.state.busy:
            log.info(
                "wakeup fired mid-turn — deferring %.0fs (turn still running)",
                WAKEUP_MIN_DELAY,
            )
            self._arm_wakeup(WAKEUP_MIN_DELAY, prompt)
            return

        # Self-clear: this task is firing, so it's no longer "pending".
        self._wakeup_task = None
        self._wakeup_fire_at = None
        log.info("wakeup fired: injecting scheduled prompt %r", prompt[:80])
        # Surface the injected prompt so the user sees what triggered the turn,
        # then queue it as a normal message.  When the worker is parked in
        # _await_next_prompt (idle OR waiting on a background task) this wakes it
        # and starts the next turn; when a turn is already live it is drained as
        # a queued prompt at the next between-turns checkpoint.
        try:
            await self._broadcast_injected_prompt(prompt, during_turn=False)
        except Exception as exc:
            log.warning("wakeup injected-prompt broadcast failed: %r", exc)
        self.event_queue.put_nowait(("message", prompt))

    # ------------------------------------------------------------------
    # Ghost-turn helpers
    # ------------------------------------------------------------------

    async def _begin_ghost_turn_if_needed(self) -> bool:
        """Mark a ghost turn active and push a status_update once.

        Called on every async assistant/user message.  Idempotent: only
        flips state once, until ``_end_ghost_turn`` clears it.

        Returns False when the caller should *not* render the message, because
        a real ``run_turn`` already owns the stream and will render it itself.

        Note this deliberately renders output that arrives shortly after an
        interrupt: that is normally a background task's <task-notification>
        waking the model, which the CLI records in the session JSONL either
        way.  See the "Why output can continue after an interrupt" comment at
        the top of this module.
        """
        state = self.state
        if self.turn_active.is_set():
            return False
        if state.busy:
            return True
        # This ghost turn IS the post-compact continuation that the deferred
        # turn_end was waiting for — it will book the turn and emit its own
        # turn_end, so drop the pending one to avoid a duplicate marker.
        self._claim_pending_compact_turn_end()
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
        return True

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
    # Deferred compact turn_end helpers
    # ------------------------------------------------------------------

    def _schedule_compact_turn_end_flush(self, grace: float = 10.0) -> None:
        """Arm a timer to flush a deferred compact turn_end.

        If a post-compact ghost turn begins first it claims the pending
        turn_end and cancels this timer; otherwise the timer fires and emits
        the turn_end so the completed turn is still marked in the UI.
        """
        t = getattr(self, "_compact_turn_end_task", None)
        if t is not None and not t.done():
            t.cancel()
        self._compact_turn_end_task = asyncio.create_task(
            self._compact_turn_end_timer(grace),
            name="compact-turn-end-flush",
        )

    def _cancel_compact_turn_end_timer(self) -> None:
        t = getattr(self, "_compact_turn_end_task", None)
        if t is not None and not t.done():
            t.cancel()
        self._compact_turn_end_task = None

    async def _compact_turn_end_timer(self, grace: float) -> None:
        try:
            await asyncio.sleep(grace)
        except asyncio.CancelledError:
            return
        await self._flush_pending_compact_turn_end()

    def _claim_pending_compact_turn_end(self) -> None:
        """A ghost turn (or other path) will own the turn_end — drop the
        deferred one and cancel its timer so it isn't emitted twice."""
        self.state.pending_compact_turn_end = None
        self._cancel_compact_turn_end_timer()

    async def _flush_pending_compact_turn_end(self) -> None:
        """Emit the deferred turn_end for a compact-closed turn that had no
        post-compact ghost-turn continuation.  Books the turn (which the
        suppressed compact ResultMessage did not) and broadcasts the marker
        plus a status_update so the turn counter updates."""
        pend = self.state.pending_compact_turn_end
        if not pend:
            return
        self.state.pending_compact_turn_end = None
        self._cancel_compact_turn_end_timer()
        state = self.state
        state.turns += 1
        duration = time.monotonic() - (
            pend.get("started_at") or time.monotonic()
        )
        log.info(
            "flushing deferred compact turn_end: turns=%d subtype=%s",
            state.turns, pend.get("subtype"),
        )
        try:
            await self.broadcast({
                "type": "turn_end",
                "subtype": pend.get("subtype") or "success",
                "duration": fmt_duration(duration),
                "turns": state.turns,
            })
            await self.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(state, self.config),
                "panels": state_to_panels_dict(state),
            })
        except Exception as exc:
            log.warning("deferred compact turn_end broadcast failed: %r", exc)

    async def _await_interrupt_settled(self, timeout: float = 5.0) -> None:
        """Hold a starting turn until an outstanding interrupt has wound down.

        From a report: "i had a queued prompt and then ctrl+c'd the turn and
        then it sent the queued prompt (or at least showed it as coming from
        me) but it didn't start working again, it stayed idle".

        The interrupt hit a **ghost turn** — a background task streaming with
        no ``run_turn`` consuming it — so ``interrupt()`` took the parked-worker
        branch and poked the worker awake.  The worker drained the queue,
        echoed the prompt and started a turn within a millisecond or two, while
        the CLI's answer to our ``client.interrupt()`` was still in flight.  It
        landed ~40 ms later, by which time ``turn_active`` was set, so the
        dispatcher handed the *previous* stream's terminator to the *new* turn::

            20:05:36,935 run_turn enter: turns=37 drained=0
            20:05:36,972 UserMessage during turn: '[Request interrupted by user]'
            20:05:36,973 run_turn ResultMessage: error_during_execution elapsed=0.0s
            20:05:36,974 run_turn exit: normal (turns=38)

        The turn died 40 ms after starting, on a result that wasn't its own —
        prompt consumed, session idle, nothing to show for it.

        Waiting here rather than swallowing the stray result in the message
        loop is deliberate: this runs *before* ``turn_active`` is set, so the
        terminator reaches ``_handle_async_message``, which closes the ghost
        turn properly.  It also means the prompt is only sent to a quiesced
        CLI — swallowing the result instead would leave us guessing whether the
        query we'd already sent had been discarded along with the interrupt.
        """
        if self._interrupt_settled.is_set():
            return
        log.info("run_turn: waiting for interrupt wind-down before starting")
        try:
            await asyncio.wait_for(self._interrupt_settled.wait(), timeout)
        except asyncio.TimeoutError:
            log.warning(
                "run_turn: interrupt wind-down never landed in %.0fs — "
                "starting anyway", timeout,
            )
        finally:
            # Either way the wait is over; a stale clear must not delay
            # every future turn.
            self._interrupt_settled.set()

    async def flush_pending_turn_end(self) -> None:
        """Close out a deferred turn_end *before* echoing the next prompt.

        A turn that ends on a compaction leaves its ``turn_end`` pending: the
        marker is emitted either by a post-compact ghost turn, by the 10 s
        timer, or by ``run_turn`` at the start of the following turn.  That
        last path is the problem — by then the next prompt has already been
        echoed, so the transcript reads::

            you: <next prompt>
            Turn 102 completed (0:26:41) — success

        i.e. the previous turn appears to finish *after* the prompt that
        follows it.  A queued prompt hits this every time (it starts the next
        turn immediately, well inside the 10 s grace); a hand-typed one only
        when the user types quickly, which is why it looked intermittent.

        So every producer that is about to echo a prompt calls this first.
        It's a pure reordering: ``run_turn`` would have flushed the same
        pending marker moments later anyway, and flushing when nothing is
        pending is a no-op.
        """
        await self._flush_pending_compact_turn_end()

    def _apply_pending_rename(self, sid: str) -> bool:
        """Apply a queued ``/rename`` title to session *sid* if one exists.

        Returns True when a pending title was successfully written (and the
        queue cleared).  On failure — most commonly because the CLI hasn't
        flushed the session JSONL to disk yet, so ``write_session_title``
        can't locate the file — the title stays queued in
        ``state.pending_rename`` for a later retry (called again at
        end-of-turn, by which point the file exists).
        """
        state = self.state
        title = state.pending_rename
        if not title:
            return False
        try:
            write_session_title(sid, title, getattr(self.config, "config_dir", None))
        except Exception as exc:
            # Keep the title queued and try again after the turn.
            log.info("pending rename not applied yet (%s) — will retry", exc)
            return False
        state.pending_rename = None
        state.session_title = title
        log.info("applied pending rename: %s → '%s'", sid[:8], title)
        return True

    # ------------------------------------------------------------------
    # System message handling (shared by turn + async paths)
    # ------------------------------------------------------------------

    async def _announce_bg_completion(
        self, *, task_id: str, status: str, summary: str | None,
        output: str | None, source: str,
    ) -> None:
        """Render a background task's completion — and say so in the log.

        Both callers (``task_notification`` and ``task_updated``) used to be
        ``if entry: ...`` with no ``else``, so a completion for a task this
        bridge never saw *start* was dropped with no broadcast and no log line.
        That happens for real: the CLI keeps background tasks running across a
        ``--continue``/``--resume``, and a bridge that attached afterwards never
        received their ``task_started``.  The model would then wake up out of
        an idle session with no visible cause — exactly the "why doesn't it say
        background task X completed?" case, and unobservable after the fact
        because nothing in this path logged.

        Unknown tasks are now rendered from whatever the notification itself
        carries.  Only genuine duplicates stay silent, and they say so in the
        log.  The bg-wait bell and the ``bg-all-done`` wakeup stay gated on a
        *known* entry: an unregistered task was never in ``background_tasks``,
        so "no tasks left" would be vacuously true and would inject a spurious
        wakeup prompt.
        """
        state = self.state
        known = (task_id in state.background_tasks
                 or task_id in state.current_turn_bg)
        entry = complete_bg_task(state, task_id, status, summary=summary)

        if entry is None and known:
            log.info(
                "bg task %s: duplicate %s completion ignored (via %s)",
                task_id[:12] or "?", status, source,
            )
            return
        if entry is None:
            log.warning(
                "bg task %s: %s completion for a task this bridge never saw "
                "start (via %s) — rendering from the notification alone",
                task_id[:12] or "?", status, source,
            )
        else:
            log.info(
                "bg task %s (%s) completed: status=%s (via %s)",
                task_id[:12] or "?", (entry.get("name") or "")[:40],
                status, source,
            )

        cmd = self._bg_task_command(entry) if entry else None
        # Send the model's summary and the task's actual stdout as *separate*
        # fields so the UI can show command, summary AND output.  Drop a
        # "summary" that's just the command repeated.
        model_summary = summary if (summary and summary != cmd) else None
        await self.broadcast({
            "type": "bg_complete",
            "task_id": task_id,
            "seq": (entry or {}).get("seq"),
            "name": (entry or {}).get("name"),
            "status": status,
            "summary": model_summary,
            "output": output,
            "command": cmd,
        })

        if entry is None:
            return
        # Only alert when the session was *parked* waiting on this task
        # (bg-wait), not when the model spawned it mid-turn and kept working —
        # a routine mid-turn completion isn't alert-worthy.
        if in_bg_wait(state):
            ring_bell(state, "bg-done")
            await self._flush_bell()
        # If no more bg tasks, update status immediately and queue a wakeup.
        if not state.background_tasks:
            await self.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(state, self.config),
                "panels": state_to_panels_dict(state),
            })
            self.event_queue.put_nowait(("wakeup", "bg-all-done"))

    async def _handle_system_message(self, msg: SystemMessage, *, during_turn: bool) -> None:
        """Process a SystemMessage from the SDK."""
        state = self.state
        subtype = getattr(msg, "subtype", None) or ""
        data: dict[str, Any] = {}

        if subtype == "init":
            first_init = not state.init_seen
            state.init_seen = True
            # The init SystemMessage has no ``session_id`` attribute — it
            # falls through the SDK parser's generic branch, so the id lives
            # in ``msg.data``.  (Only task_started/task_notification get a
            # dedicated dataclass field.)  Reading the attribute alone left
            # ``state.session_id`` unset until the turn's ResultMessage,
            # which broke ``/rename`` mid-first-turn ("will be applied when
            # the session starts").
            data_dict = getattr(msg, "data", None)
            sid = getattr(msg, "session_id", None)
            if not sid and isinstance(data_dict, dict):
                sid = data_dict.get("session_id")
            old_sid = state.session_id
            if sid:
                state.session_id = sid
                # Don't clobber a user-set pending title with a (title-less)
                # disk read — keep showing what /rename set until we can
                # persist it.
                if not state.pending_rename:
                    # Off-load the disk read: reading a session's title means
                    # scanning its (potentially huge) JSONL line-by-line, which
                    # would block the event loop — freezing the status ticker so
                    # the toolbar stays "idle" through a whole turn.
                    state.session_title = await asyncio.to_thread(
                        read_session_title, sid,
                        getattr(self.config, "config_dir", None),
                    )
                # Apply a pending rename (from /rename before first turn).
                # May legitimately fail here — the CLI often hasn't flushed
                # the session JSONL to disk yet, so the write can't find the
                # file.  _apply_pending_rename keeps the title queued for a
                # retry at end-of-turn instead of dropping it.
                self._apply_pending_rename(sid)
                # Warn if the SDK silently started a fresh session
                # instead of resuming the one we asked for.  This used to be
                # gated on ``first_init`` as well — but ``init_seen`` is set
                # once and never cleared, so after the very first init the
                # check could never fire again.  That disabled it for exactly
                # the case it matters most: every *reconnect* is a resume, and
                # `/model` / `/effort` / `/thinking` / `/connect` all reconnect.
                # ``expected_resume_sid`` is set fresh by `_make_options()` on
                # each connect, so comparing it every time is correct.
                expected = state.expected_resume_sid
                if expected and expected != sid:
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
            # Surface the retry to the UI (no stall detection — that only
            # existed to halt the dead auto-continue loop).
            error_info = getattr(msg, "error", None)
            data = {"error": str(error_info) if error_info else None}

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
            await self._announce_bg_completion(
                task_id=task_id, status=status, summary=summary,
                output=output, source="task_notification",
            )

        elif subtype == "task_started":
            task_id = getattr(msg, "task_id", None) or ""
            task_type = getattr(msg, "task_type", None) or "bash"
            # The SDK's TaskStartedMessage field is ``description``; there is
            # no ``name``.  Reading ``name`` silently yielded "" for every
            # task, so the bg panel showed unlabelled rows.  Keep ``name`` as
            # a fallback in case a future SDK renames it back.
            name = (getattr(msg, "description", None)
                    or getattr(msg, "name", None) or "")
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
            # Logged so a later completion can be matched to its start — a
            # completion with no start line here is the "never saw it start"
            # case in _announce_bg_completion.
            log.info(
                "bg task %s (%s) started: type=%s during_turn=%s",
                task_id[:12] or "?", (name or "")[:40], task_type, during_turn,
            )
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

        elif subtype == "status":
            # The CLI narrating its own work.  The value we care about is
            # ``"compacting"`` (set when an auto-compaction starts, cleared
            # with ``None`` when it ends): a compaction is a single
            # summarisation call over the whole context and can run for
            # minutes with no output whatsoever — one measured run took 141 s —
            # so without this the status bar just says "working" and the
            # session is indistinguishable from a wedged one.
            d = msg.data if isinstance(msg.data, dict) else {}
            new_status = d.get("status")
            if new_status != state.cli_status:
                state.cli_status = new_status
                state.cli_status_started_at = (
                    time.monotonic() if new_status else None)
                log.info("CLI status: %s", new_status or "(cleared)")
                await self._broadcast_panels()
            return

        elif subtype == "session_state_changed":
            # The session lifecycle state lives in ``msg.data['state']``
            # (SystemMessage only carries ``subtype`` + ``data``).  Emitted
            # only when CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS is set, which
            # we do in _make_options.
            d = msg.data if isinstance(msg.data, dict) else {}
            session_state = d.get("state")
            if session_state == "requires_action":
                ring_bell(state, "requires-action")
                await self._flush_bell()
                data = {"state": "requires_action"}
            else:
                # idle/running are lifecycle noise — orchestrator2 already
                # tracks busy/idle itself via run_turn.  Don't surface them.
                return

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
                await self._announce_bg_completion(
                    task_id=task_id, status=status, summary=summary,
                    output=output, source="task_updated",
                )

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
        """SDK ``can_use_tool`` callback.

        Special-cases ``AskUserQuestion`` — an interactive multiple-choice
        tool the CLI invokes even under ``bypassPermissions`` (permission
        pipeline step 1e).  orchestrator2's chat UI has no picker widget, and
        answering the tool requires returning the selections via
        ``updated_input`` (which we don't collect), so letting it run just
        yields empty answers — that's how it "always failed".  Instead we
        surface the questions in the chat and deny with guidance so Claude
        continues the exchange in plain text, which the chat handles
        naturally.  Every other tool keeps the prior behavior.
        """
        if tool_name == "AskUserQuestion":
            await self._surface_ask_user_question(tool_input)
            return PermissionResultDeny(
                message=(
                    "AskUserQuestion isn't supported in this interface, so it "
                    "was not run. Your questions have already been shown to the "
                    "user in the chat. Do not repeat them — end your turn and "
                    "wait for the user's plain-text reply, then continue with "
                    "their answer."
                ),
            )

        # Ordinary tools only reach here under a real permission mode; bypass
        # auto-allows them before calling us.  Preserve that: auto-allow under
        # bypass, otherwise prompt the user over the WebSocket.
        if self.config.permission_mode == "bypassPermissions":
            return PermissionResultAllow()

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

    async def _surface_ask_user_question(self, tool_input: dict) -> None:
        """Render an AskUserQuestion tool call as a readable chat message.

        The tool input carries 1-4 questions, each with 2-4 labelled options
        (an "Other"/free-text choice is always implied).  We format them as a
        system message so the user sees exactly what Claude wanted to ask and
        can answer in their own words.
        """
        questions = []
        if isinstance(tool_input, dict) and isinstance(tool_input.get("questions"), list):
            questions = tool_input["questions"]

        lines: list[str] = []
        lines.append(
            "Claude has a question:" if len(questions) == 1
            else "Claude has some questions:"
        )
        for qi, q in enumerate(questions, 1):
            if not isinstance(q, dict):
                continue
            qtext = str(q.get("question") or "").strip()
            multi = bool(q.get("multiSelect"))
            prefix = f"{qi}. " if len(questions) > 1 else ""
            lines.append("")
            lines.append(
                f"{prefix}{qtext}" + ("  (choose all that apply)" if multi else "")
            )
            opts = q.get("options")
            if isinstance(opts, list):
                for opt in opts:
                    if not isinstance(opt, dict):
                        continue
                    label = str(opt.get("label") or "").strip()
                    desc = str(opt.get("description") or "").strip()
                    lines.append(f"    - {label}" + (f" \u2014 {desc}" if desc else ""))
            lines.append("    - (or answer in your own words)")

        text = "\n".join(lines).rstrip()
        if not text:
            text = "Claude tried to ask a question, but it had no content."
        await self.broadcast({
            "type": "system_msg",
            "subtype": "info",
            "data": {"message": text},
        })

    async def _surface_api_error_loop(self, err_code: str | None) -> None:
        """Break out of a repeating API-error loop and warn the user.

        Called when the same API error has ended two or more turns in a row.
        A transient 429/500 clears on retry, but a 400 from a bad block deep
        in the resumed conversation (e.g. an unsupported image) is re-sent on
        every turn and never clears.  We flag the status bar
        (``needs_user_attention = "api-error"``) and, on the first stuck turn
        only, print a recovery hint.
        """
        state = self.state
        # Flag the status bar.  This is the only attention state still in use,
        # and it survives until the user's next message clears it.
        state.needs_user_attention = "api-error"
        # Only emit the guidance banner once per stuck streak (repeat_count
        # is exactly 2 the first time we detect the loop).
        if state.api_error_repeat_count != 2:
            return
        code = err_code or "?"
        msg = (
            f"Stuck in a repeating API error ({code}). The same request keeps "
            f"failing, so continuing won't help — this usually means a bad "
            f"message earlier in the conversation (e.g. an unsupported image) "
            f"is being re-sent on every turn.\n\n"
            f"Recovery options:\n"
            f"    - Start a fresh session (the surest fix — the poisoned "
            f"history is left behind).\n"
            f"    - Trim the conversation to drop the offending old message, "
            f"then retry.\n"
            f"Nothing is retried automatically — the session waits for your "
            f"next message."
        )
        await self.broadcast({
            "type": "system_msg",
            "subtype": "error",
            "data": {"message": msg},
        })

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

        # An interrupt we issued against a ghost turn may still be winding
        # down; its terminating ResultMessage belongs to *that* stream, not to
        # this turn.  Let it land first (and be absorbed by the between-turns
        # handler) before we claim the message queue.
        await self._await_interrupt_settled()

        # Drain stale messages from the queue.
        drained = 0
        while not self.turn_msg_queue.empty():
            try:
                self.turn_msg_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break

        # If the previous turn ended on a compaction and its turn_end is
        # still pending (no ghost-turn continuation arrived), emit it now —
        # before this new turn's content — so the completed turn is marked.
        if state.pending_compact_turn_end is not None:
            await self._flush_pending_compact_turn_end()

        # A turn is starting, so any wakeup the model scheduled earlier has been
        # superseded — cancel it.  If the model still wants the loop to continue
        # it will call ScheduleWakeup again before this turn ends.
        self._cancel_wakeup()

        self.interrupt_event.clear()
        state.busy = True
        state.turn_started_at = time.monotonic()
        # A compaction cannot span a turn boundary, so anything left here is
        # stale — e.g. the CLI died mid-compaction and never sent the clearing
        # status.  Without this the bar would read "compacting" forever.
        state.cli_status = None
        state.cli_status_started_at = None
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
        # Flip the toolbar to "working" immediately instead of waiting up to
        # ~2s for the next status-ticker cycle (state.busy is already True).
        await self._broadcast_status()

        # Send the prompt.
        await self.client.query(prompt_text)

        assistant_text = ""
        interrupted = False
        current_streaming_text = ""
        normal_completion = False
        interrupt_announced = False
        interrupt_finished = False
        # After a user interrupt we keep draining the SDK's wind-down stream
        # *inside* run_turn until the CLI's terminating ResultMessage lands,
        # so none of it leaks to the between-turns async path as a ghost turn
        # (the "I interrupted but it just kept working" symptom).  This
        # timeout is the safety net for the case the old INTERRUPT_SENTINEL
        # early-break guarded: an interrupt while the SDK isn't streaming,
        # where no further message — and no ResultMessage — ever arrives.
        _INTERRUPT_DRAIN_TIMEOUT = 5.0

        async def _announce_interrupt() -> None:
            """Flag the turn interrupted; give the user instant feedback once.

            The visible ``turn_end`` interrupted marker is deferred to
            ``_finish_interrupt()`` so it lands *after* the SDK's wind-down
            output during the drain, not before it.
            """
            nonlocal interrupted, normal_completion, interrupt_announced
            interrupted = True
            normal_completion = True  # user-initiated, not a silent drop
            if interrupt_announced:
                return
            interrupt_announced = True
            await self.broadcast({
                "type": "system_msg",
                "subtype": "info",
                "data": {"message": "Interrupting…"},
            })

        async def _finish_interrupt() -> None:
            """Emit the interrupted turn_end marker at the true stop point."""
            nonlocal interrupt_finished
            if interrupt_finished:
                return
            interrupt_finished = True
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

        try:
            while True:
                # In drain mode (post-interrupt) bound the wait so a CLI
                # that went quiet without a ResultMessage can't hang us.
                if interrupted:
                    try:
                        msg = await asyncio.wait_for(
                            self.turn_msg_queue.get(),
                            timeout=_INTERRUPT_DRAIN_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        log.info(
                            "run_turn: interrupt drain quiet for %.0fs — stopping",
                            _INTERRUPT_DRAIN_TIMEOUT,
                        )
                        await _finish_interrupt()
                        break
                else:
                    msg = await self.turn_msg_queue.get()

                if msg is DISPATCHER_DEAD:
                    raise RuntimeError("SDK dispatcher died mid-turn")

                # Interrupt poke — begin/continue draining rather than
                # breaking immediately.  Keeping run_turn as the message
                # consumer until the CLI's ResultMessage means the SDK's
                # remaining output is absorbed here instead of resurfacing
                # as a ghost turn between turns.
                if msg is INTERRUPT_SENTINEL:
                    await _announce_interrupt()
                    continue

                # ---- SystemMessage ----
                if isinstance(msg, SystemMessage):
                    await self._handle_system_message(msg, during_turn=True)

                # ---- AssistantMessage ----
                elif isinstance(msg, AssistantMessage):
                    # Real turn content means the API accepted this request, so
                    # any lingering "rejected" rate-limit state no longer holds
                    # (e.g. a stale rejection carried over from before a /login
                    # account switch).  Clear it now so the status flips from
                    # "rate limited" straight to "working" instead of waiting
                    # for the next RateLimitEvent/ResultMessage to report
                    # status="allowed" partway through the turn.
                    if state.rate_limit_status == "rejected":
                        state.rate_limit_status = "allowed"
                        state.rate_limit_resets_at = None
                        state.rate_limit_reset_bell_fired = False
                        await self._broadcast_status()
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
                            self._maybe_arm_wakeup(name, inp)

                            seq = register_tool_use(
                                state, block.id, name, inp,
                            )

                            # Capture TodoWrite plan.
                            todos_changed = (
                                name == "TodoWrite"
                                and isinstance(inp.get("todos"), list)
                            )
                            if todos_changed:
                                state.current_todos = inp["todos"]

                            ws_msg = format_tool_use_msg(
                                name, inp, block.id, seq,
                                is_background=bool(is_bg),
                            )
                            await self.broadcast(ws_msg)
                            # Refresh the todos panel immediately instead of
                            # waiting for the next ~2s status ticker.
                            if todos_changed:
                                await self._broadcast_panels()

                        # Thinking
                        elif ThinkingBlock and isinstance(block, ThinkingBlock):
                            thinking_text = getattr(block, "thinking", "") or ""
                            signature = getattr(block, "signature", "") or ""
                            # Models newer than opus-4-6 (4-8, 5, …) return
                            # *encrypted* reasoning: the block carries a
                            # signature but an empty ``thinking`` string, and
                            # no thinking_delta ever streams.  Don't drop it —
                            # surface the block so the user can still see that
                            # the model reasoned, just without the text.
                            if thinking_text.strip():
                                seq = register_thinking(state, thinking_text)
                                await self.broadcast({
                                    "type": "thinking",
                                    "seq": seq,
                                    "content": thinking_text,
                                })
                            elif signature:
                                seq = register_thinking(state, "", encrypted=True)
                                await self.broadcast({
                                    "type": "thinking",
                                    "seq": seq,
                                    "content": "",
                                    "encrypted": True,
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

                    # Terminating result after a user interrupt: emit the
                    # deferred interrupted turn_end marker now (after the
                    # wind-down output), and don't book the aborted turn.
                    if interrupted:
                        await _finish_interrupt()
                        break

                    subtype = getattr(msg, "subtype", None) or "unknown"
                    state.last_result_subtype = subtype

                    # API-ERROR DETECTION.
                    #
                    # The CLI reports API failures as inline assistant text
                    # ("API Error: 400 {...}") while still closing the turn
                    # with subtype="success".  Detect that so (a) the turn is
                    # labelled "error" instead of "success", and (b) a fault
                    # that repeats every turn — the classic poisoned-history
                    # loop where a bad image/message deep in the resumed
                    # conversation is rejected on every resend — is caught and
                    # the auto-continue loop broken with a recovery hint.
                    err_code, err_sig = detect_api_error(msg, assistant_text)
                    # An auth failure can arrive two ways: a per-turn HTTP 401
                    # ("API Error: 401 {...}"), or a *codeless* banner the CLI
                    # emits as the turn's assistant text when the stored session
                    # is unusable ("Not logged in · Please run /login").  The
                    # latter carries no code, so detect_api_error misses it —
                    # scan the visible text separately so `/login` still knows to
                    # re-auth instead of insisting we're "already signed in".
                    auth_failed = err_code == "401"
                    if not auth_failed:
                        for _t in (assistant_text, getattr(msg, "result", None)):
                            # Anchored (.match on the stripped text): only a turn
                            # whose output *begins* with the refusal banner counts,
                            # so the model discussing "Not logged in" / "/login" in
                            # a debugging chat doesn't trip a false "not authed".
                            if isinstance(_t, str) and _t and _AUTH_BANNER_RE.match(_t.lstrip()):
                                auth_failed = True
                                break
                    api_error_loop = False
                    if err_code:
                        subtype = "error"
                        if err_sig == state.last_api_error_signature:
                            state.api_error_repeat_count += 1
                        else:
                            state.last_api_error_signature = err_sig
                            state.api_error_repeat_count = 1
                        # Two identical API errors back-to-back means we're
                        # stuck (a transient 429/500 clears on retry; a 400
                        # from poisoned history does not).
                        api_error_loop = state.api_error_repeat_count >= 2
                    else:
                        state.last_api_error_signature = None
                        state.api_error_repeat_count = 0

                    # A 401 or a codeless "not logged in" banner means the stored
                    # OAuth session is dead and won't recover on its own.  Flag it
                    # so `/login` forces a re-auth; only a turn that actually
                    # authenticated clears the sticky flag.
                    if auth_failed:
                        subtype = "error"
                        state.auth_error = True
                    else:
                        state.auth_error = False

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

                    # Retry a queued /rename that couldn't be written at
                    # init time (the session JSONL now exists on disk), then
                    # reload the title.  Reload only when nothing is pending
                    # so a fresh title isn't clobbered by a stale disk read.
                    if state.session_id:
                        if not self._apply_pending_rename(state.session_id):
                            # Don't clobber a user-set title that hasn't been
                            # persisted yet (e.g. /rename before the JSONL was
                            # flushed to disk — pending_rename is still queued).
                            #
                            # Only read when we don't already have a title: this
                            # exists to catch the ai-title the CLI generates
                            # after the first exchange (session_title is still
                            # None then).  Once we have one, our own /rename
                            # updates state directly, so re-scanning the
                            # (possibly huge) JSONL every turn end would just
                            # burn I/O — and, done on the event loop, froze the
                            # status ticker at "idle" for the whole turn.
                            if not state.pending_rename and not state.session_title:
                                state.session_title = await asyncio.to_thread(
                                    read_session_title,
                                    state.session_id,
                                    getattr(self.config, "config_dir", None),
                                )

                    # Rate limit info may be on ResultMessage too.
                    rate_info = getattr(msg, "rate_limit_info", None)
                    if rate_info is not None:
                        apply_rate_limit_info(state, rate_info)

                    # Only broadcast ``turn_end`` immediately for *real*
                    # (non-compact) turn ends.
                    #
                    # A compact-induced ResultMessage *may* be followed by a
                    # post-compact ghost turn that owns the user-visible
                    # turn_end — broadcasting one here would produce a
                    # premature marker.  But it may NOT be: when the SDK
                    # auto-compacts mid-turn and the response was already
                    # complete, run_turn consumes the whole post-compact
                    # stream itself and exits with no ghost turn to follow.
                    # In that case suppressing the turn_end loses it (and
                    # never books the turn).  So instead of suppressing, we
                    # *defer*: record the pending turn_end and let either a
                    # ghost turn claim it (``_claim_pending_compact_turn_end``)
                    # or a short timer / the next turn flush it
                    # (``_flush_pending_compact_turn_end``).
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
                    else:
                        state.pending_compact_turn_end = {
                            "subtype": subtype,
                            "started_at": state.turn_started_at or time.monotonic(),
                        }
                        self._schedule_compact_turn_end_flush()

                    # If we're stuck in an API-error loop, break auto-continue
                    # and surface a recovery hint (once per stuck streak).
                    if api_error_loop:
                        await self._surface_api_error_loop(err_code)

                    normal_completion = True
                    break

                # ---- Unknown (e.g. RateLimitEvent) ----
                else:
                    self._handle_unknown_message(msg)

                # Check for a freshly-signalled interrupt.  Don't break:
                # announce the interruption, then loop back into drain mode
                # so the CLI's wind-down stream (and its terminating
                # ResultMessage) is consumed here rather than leaking to the
                # ghost-turn async path.  interrupt() already called
                # client.interrupt(); re-assert it as a belt-and-braces stop
                # in case the CLI hadn't acted on the first request yet.
                if self.interrupt_event.is_set() and not interrupted:
                    if self.client:
                        try:
                            await self.client.interrupt()
                        except Exception:
                            pass
                    await _announce_interrupt()

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
            # Belt and braces: this turn consumed whatever was on the wire, so
            # no wind-down can still be outstanding.  Guarantees a stale clear
            # can never survive a turn and stall the next one.
            self._interrupt_settled.set()
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

            # If a user interrupt was announced but the loop exited before
            # its deferred turn_end marker was emitted (e.g. an exception
            # during the drain), emit it now so the UI still shows the
            # turn ended as interrupted.
            if interrupted and not interrupt_finished:
                try:
                    await _finish_interrupt()
                except BaseException as bx:
                    log.warning("deferred interrupt turn_end failed: %r", bx)

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

        # Flush any bell that was rung during this turn.
        await self._flush_bell()

        return assistant_text, interrupted

    async def _flush_bell(self) -> None:
        """Broadcast any pending bell event to the frontend, then clear it.

        ``ring_bell()`` only records ``state.pending_bell``; it never sends
        anything itself.  Bells rung *between* turns (turn-done, bg-done,
        requires-action, rate-hit) would
        otherwise sit unflushed until the *next* turn ended — which never
        happens while Claude is waiting for the user, so the user never
        hears them.  Every async path that rings a bell must funnel through
        here (and the status ticker flushes as a catch-all safety net).
        """
        if self.state.pending_bell:
            event = self.state.pending_bell
            # Logged at the point the browser is actually told to make a
            # sound — a ring can sit pending for a while, so "rung" and
            # "heard" are different moments and the gap matters when the user
            # is trying to work out what they just heard.  ``session`` is what
            # tells apart a bell from *this* tab and one from another session
            # in the same hub.
            log.info(
                "bell: %s -> browser session=%s title=%r",
                event, (self.state.session_id or "?")[:12],
                (self.state.session_title or "")[:40],
            )
            try:
                await self.broadcast({
                    "type": "bell",
                    "event": event,
                    "session_id": self.state.session_id,
                    "session_title": self.state.session_title,
                })
            except Exception as exc:
                log.warning("bell broadcast failed: %r", exc)
            self.state.pending_bell = None

    async def _broadcast_status(self) -> None:
        """Push a fresh status_update (+panels) so the browser's busy/connecting
        state and (Ns) timer track the backend.  Used from the connect
        retry loop where the frontend would otherwise keep a stale value.
        """
        try:
            await self.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(self.state, self.config),
                "panels": state_to_panels_dict(self.state),
            })
        except Exception as exc:
            log.warning("status_update broadcast failed: %r", exc)

    async def _broadcast_queue(self) -> None:
        """Push the pending-prompt queue to this session's viewers.

        The twin of server.py's ``_broadcast_queue_update`` — the bridge can't
        call that one (it's a server-layer helper bound to the REST endpoints),
        so mutations made *here* need their own push.
        """
        try:
            await self.broadcast({
                "type": "queue_update",
                "queue": [{"index": i, "text": t}
                          for i, t in enumerate(self.state.queued_prompts)],
            })
        except Exception as exc:
            log.warning("queue_update broadcast failed: %r", exc)

    async def _pop_queued_prompt(self) -> str | None:
        """Take the head of the pending-prompt queue and hand it to the UI.

        Returns None when the queue is empty *or* its head is being edited in
        the panel (sending half-typed text would be worse than waiting).

        Every pop has to do three things together, which is why they live in
        one place instead of being repeated at each call site:

        * echo the prompt into the transcript (``user_message``) — the frontend
          skips its own optimistic echo while ``_isBusy``, which is exactly
          when a prompt gets queued;
        * clear ``queue_editing_index``, which is stale the moment the item it
          referred to leaves the queue;
        * **push a fresh ``queue_update``**.  The queue panel is rendered from
          that snapshot, so without it the prompt stays listed as pending while
          the turn it just started is already streaming into the transcript —
          the "it sent the prompt, but it was still also in the queue" report.
          Only the 2 s status ticker corrected it, which is precisely why the
          omission survived so long: the ghost row healed itself just as the
          user reached for the mouse.
        """
        state = self.state
        if not state.queued_prompts or state.queue_editing_index == 0:
            return None
        prompt = state.queued_prompts.popleft()
        state.queue_editing_index = None
        state.needs_user_attention = None
        # Close out the previous turn first — see flush_pending_turn_end().
        await self.flush_pending_turn_end()
        await self.broadcast({"type": "user_message", "content": prompt})
        await self._broadcast_queue()
        return prompt

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
            except proc_guard.DuplicateSessionError as exc:
                # Not transient — retrying just delays a message the user has
                # to act on (kill the other process).  Stop cleanly and leave
                # the explanation on screen instead of burying it under ten
                # backoff attempts.
                state.connecting = False
                state.connect_started_at = None
                await self._broadcast_status()
                await self.broadcast({
                    "type": "system_msg",
                    "subtype": "error",
                    "data": {"message": str(exc)},
                })
                return
            except Exception as exc:
                _connect_attempt += 1
                log.error("SDK connect failed (attempt %d): %s", _connect_attempt, exc)

                if _connect_attempt >= _MAX_CONNECT_ATTEMPTS:
                    # If the user queued prompt(s) while we were retrying,
                    # that's a clear signal to keep trying rather than make
                    # them re-type — restart the auto-retry (capped delay).
                    if state.queued_prompts:
                        _connect_attempt = 0
                        _connect_delay = _MAX_CONNECT_DELAY
                        # Stay "connecting" so further typed prompts keep
                        # landing in the visible queue during the wait.
                        state.connecting = True
                        if state.connect_started_at is None:
                            state.connect_started_at = time.monotonic()
                        await self._broadcast_status()
                        await self.broadcast({
                            "type": "system_msg",
                            "subtype": "error",
                            "data": {"message": f"Still can't connect ({exc}). Retrying every {_MAX_CONNECT_DELAY:.0f}s\u2026"},
                        })
                        try:
                            await asyncio.wait_for(
                                self.stop_event.wait(), timeout=_connect_delay,
                            )
                            return
                        except asyncio.TimeoutError:
                            pass
                        continue
                    # Genuinely give up for now.  Clear connecting so both
                    # the UI and the message router stop treating us as
                    # connecting — a fresh prompt should route to the
                    # event_queue and wake this manual-retry loop.
                    state.connecting = False
                    state.connect_started_at = None
                    await self._broadcast_status()
                    _retry_hint = ("Your Claude login has expired — run /login to "
                                   "re-authenticate, then send a message."
                                   if state.auth_error
                                   else "Send a message to retry.")
                    await self.broadcast({
                        "type": "system_msg",
                        "subtype": "error",
                        "data": {"message": f"SDK connection failed after {_connect_attempt} attempts: {exc}. {_retry_hint}"},
                    })
                    # Fall back to manual retry.  A user message restarts the
                    # auto-retry loop; so does an explicit /connect — the natural
                    # thing to run after a /login re-auth (the connect-failure
                    # message tells the user to do exactly that).
                    while not self.stop_event.is_set():
                        kind, payload = await self.event_queue.get()
                        if kind in ("quit", "force-quit"):
                            return
                        if kind == "connect":
                            _connect_attempt = 0
                            _connect_delay = 2.0
                            state.connecting = True
                            state.connect_started_at = time.monotonic()
                            break  # restart auto-retry loop (no prompt to requeue)
                        if kind == "message":
                            _connect_attempt = 0
                            _connect_delay = 2.0
                            # Re-mark connecting so a re-typed prompt during
                            # the next retry cycle is routed to the visible
                            # queue rather than silently to the event_queue.
                            state.connecting = True
                            state.connect_started_at = time.monotonic()
                            self.event_queue.put_nowait((kind, payload))
                            break  # restart auto-retry loop
                    continue

                # Non-final failure: we're still trying, so re-assert the
                # "connecting" state for the backoff window.  connect()
                # cleared it in its finally, but until a connect actually
                # succeeds both the UI and the message router must keep
                # treating us as connecting — otherwise a prompt typed
                # during the backoff routes to the event_queue (invisible)
                # instead of the visible queued_prompts panel, and in a
                # never-succeeds hang it looks "eaten".  Pushing a fresh
                # status_update also keeps the browser's (Ns) timer live.
                state.connecting = True
                if state.connect_started_at is None:
                    state.connect_started_at = time.monotonic()
                await self._broadcast_status()
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
        elif state.queued_prompts and state.queue_editing_index != 0:
            next_prompt = await self._pop_queued_prompt()
        else:
            # Wait for first user input — through the *same* helper the
            # between-turns path parks in.  This used to be a second,
            # hand-rolled wait loop, and every divergence between the two was
            # a bug that fired only when the user happened to be parked at
            # this one: it dropped /compact, /btw and wakeups outright, and
            # (like _await_next_prompt before the fix above) it never drained
            # a prompt typed during a config command's reconnect.  Having one
            # idle wait means idle behaviour can't depend on which wait point
            # you're standing in.
            #
            # No echo here.  Anything arriving on the event_queue was already
            # echoed at enqueue time by server.py's _enqueue_prompt (or by the
            # browser, which tells the server so).  This used to echo from a
            # ``state.initial_prompt_client_echoed`` flag — a single slot that
            # only ever described the *last* message the WebSocket handler saw,
            # and which the REST producers never set at all.
            next_prompt = await self._await_next_prompt()

        # --- Turn loop ---
        while next_prompt is not None and not self.stop_event.is_set():
            assistant_text = ""
            interrupted = False

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
            # A prompt queued *during* the turn the user then interrupted is
            # the "stop — do this instead" gesture, so it still has to go out.
            # This branch used to return straight to the idle wait, skipping
            # the queue drain below; nothing pokes the worker after an
            # interrupt, so the prompt sat in the panel unsent forever.
            # Whether the user hit that or the working path was pure luck:
            # ``interrupted`` is only True if run_turn noticed the interrupt
            # before the SDK's ResultMessage landed, and on a fast turn the
            # ResultMessage wins and the normal path drains the queue.  Same
            # keypress, two outcomes — hence draining here too.
            #
            # We deliberately do *not* fall through to the auto-continue /
            # compaction logic further down: an interrupted turn must never
            # auto-resume.
            prompt = await self._pop_queued_prompt()
            if prompt is not None:
                return prompt
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
            prompt = await self._pop_queued_prompt()
            # None means the head is being edited in the UI — wait for the
            # user to finish rather than sending half-typed text.
            if prompt is None:
                return await self._await_next_prompt()
            return prompt

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
            if state.needs_user_attention != "api-error":
                state.needs_user_attention = None
            await self.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(state, config),
                "panels": state_to_panels_dict(state),
            })
            return await self._await_next_prompt()

        # --- Turn ended → notify and wait for the user ---
        # Every completed turn returns control to the user here — there is no
        # auto-continue loop (removed 2026-08-14; see known-issues.md).  This
        # is the only turn-completion bell that fires.
        ring_bell(state, "turn-done")
        # Preserve an "api-error" flag so the status bar keeps signalling the
        # stuck state until the user sends their next message (which clears it
        # in _await_next_prompt).  Any other attention flag is cleared here.
        if state.needs_user_attention != "api-error":
            state.needs_user_attention = None
        return await self._await_next_prompt()

    async def _apply_idle_config_command(self, kind: str, payload: str) -> bool:
        """Apply a config-change command issued while no turn is running.

        Covers the commands the user can fire while idle — before the first
        turn *or* parked between turns: ``/model``, ``/effort``, ``/thinking``,
        ``/connect``, ``/clear``.  Each updates the relevant state, tells the
        user, and reconnects so the change takes effect on the already-open SDK
        connection (model/effort/thinking are fixed at connect time).  Returns
        True when *kind* was handled, False for anything else so the caller can
        deal with messages / quit / wakeups itself.

        Note for callers: every branch here reconnects, and ``state.connecting``
        is True for the duration — so a prompt typed during one is routed to
        ``state.queued_prompts``, not to the event_queue.  A caller that returns
        to waiting without draining that queue strands the prompt.  There is
        exactly one such caller (``_await_next_prompt``), and it drains.
        """
        state = self.state
        if kind == "model":
            state.model = payload
            await self.broadcast({"type": "system_msg", "subtype": "info",
                                  "data": {"message": f"model → {payload}"}})
            await self.reconnect()
            return True
        if kind == "effort":
            state.effort = None if payload == "auto" else payload
            await self.broadcast({"type": "system_msg", "subtype": "info",
                                  "data": {"message": f"effort → {payload}"}})
            await self.reconnect()
            return True
        if kind == "thinking":
            if payload == "on":
                state.thinking_enabled = True
            elif payload == "off":
                state.thinking_enabled = False
            else:
                state.thinking_enabled = not state.thinking_enabled
            await self.broadcast({"type": "system_msg", "subtype": "info",
                                  "data": {"message":
                                           f"thinking → {'on' if state.thinking_enabled else 'off'}"}})
            await self.reconnect()
            return True
        if kind == "connect":
            await self.reconnect()
            return True
        if kind == "clear-context":
            await self._clear_context()
            return True
        return False

    async def _await_next_prompt(self) -> str | None:
        """Block until the user sends a message or quits."""
        # Flush any bell rung in the between-turns decision logic
        # (turn-done, interrupt) before we block — otherwise it would never
        # reach the frontend while we wait here.
        await self._flush_bell()
        while not self.stop_event.is_set():
            kind, payload = await self.event_queue.get()

            if kind == "message":
                self.state.needs_user_attention = None
                return payload
            if kind in ("quit", "force-quit"):
                return None
            if kind == "compact":
                return "/compact"
            # Config-change commands (model/effort/thinking/connect/clear) are
            # applied via the shared helper so idle behaviour matches the
            # initial-prompt wait exactly.
            if await self._apply_idle_config_command(kind, payload):
                # Every one of those commands reconnects the SDK, and for the
                # seconds that takes ``state.connecting`` is True — which is
                # precisely the condition under which server.py routes a typed
                # prompt to ``state.queued_prompts`` instead of our event_queue.
                # So a prompt typed *during* a `/model` switch lands in the
                # queue panel, and nothing ever pokes us afterwards: we go
                # straight back to blocking on event_queue.get() while the UI
                # says idle and the prompt sits there unsent forever.  That is
                # the reported "changed the model, typed while it reconnected,
                # then it just sat there".
                #
                # The between-turns path never had this hole — _between_turns()
                # reconnects at the top and drains queued_prompts further down,
                # so the same keystrokes worked or didn't purely on whether a
                # turn happened to be finishing.  Draining here makes the two
                # idle paths agree.
                #
                # Checking after the await is race-free: connect() clears
                # ``connecting`` in a finally block before reconnect() returns,
                # so a prompt is either already in queued_prompts (and popped
                # here) or routes to the event_queue we're about to block on.
                prompt = await self._pop_queued_prompt()
                if prompt is not None:
                    return prompt
                continue
            if kind == "wakeup":
                # Any wakeup is a chance to flush a queued *user* prompt.
                # This covers "queue-edit-done" (edit finished) *and*
                # "bg-all-done" (background tasks drained while we were
                # parked in bg-wait).  When a background task briefly
                # streams, _begin_ghost_turn_if_needed flips state.busy
                # True, so a prompt the user sends in that window is routed
                # to state.queued_prompts by server.py.  If the ghost turn's
                # queue-edit-done poke doesn't reach us (e.g. the bg task
                # completes first and fires bg-all-done instead), nothing
                # else would send it and the prompt sits unsent.  A queued
                # user prompt is always sendable once we're back to waiting.
                # Skip only when the first item is being edited in the UI.
                prompt = await self._pop_queued_prompt()
                if prompt is not None:
                    return prompt
                # No queued prompt — stay parked.  The auto-continue loop that
                # used to auto-resume here (on bg-all-done / rate-limit reset /
                # api-status-recovered) was removed: it sat behind a flag that
                # was permanently False, so it could never fire.  See
                # known-issues.md.
                continue
            if kind == "btw":
                # Send it straight through.  This used to append to the queue
                # and pop the head back off, which is a no-op only when the
                # queue is empty — with anything already pending it returned
                # *that* prompt instead and left the /btw text behind.  We're
                # idle here, so there's nothing to queue behind anyway.
                self.state.needs_user_attention = None
                return payload
            # Unknown kind — drop and loop.

        return None

    async def _clear_context(self) -> None:
        """Clear session state and reconnect (implements /clear)."""
        state = self.state
        # A scheduled wakeup belongs to the session we're wiping — drop it.
        self._cancel_wakeup()
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
        # though the user already saw the interrupt acknowledged.
        # The sentinel lets the loop tick once and notice
        # ``interrupt_event`` is set.
        live = self.turn_active.is_set()
        if live:
            try:
                self.turn_msg_queue.put_nowait(INTERRUPT_SENTINEL)
            except Exception:
                pass
        else:
            # No live turn — the worker is parked in _await_next_prompt (idle,
            # or waiting on background tasks), where the turn_msg_queue
            # sentinel can't reach it and _between_turns will never run.  A
            # prompt can still be sitting in the queue: ``turn_active`` is set
            # only by run_turn, so while a *ghost* turn streams (a background
            # task producing output) ``state.busy`` is True and server.py
            # routes typed prompts to ``queued_prompts`` — with the worker
            # parked the whole time.  Ctrl-C there used to do nothing
            # whatsoever.  A wakeup makes the parked worker drain the queue;
            # if it's empty the branch falls through to the auto-resume check,
            # which ignores an "interrupt" payload, so this is a no-op.
            #
            # If a *ghost turn* is what we're interrupting (state.busy with no
            # run_turn), the CLI will answer with a wind-down UserMessage and a
            # terminating ResultMessage that no turn is consuming.  Hold the
            # next turn until that lands — see _await_interrupt_settled().
            # Only when busy: interrupting a genuinely idle session produces no
            # terminator at all, and waiting for one would stall every
            # subsequent turn for the full timeout.
            if self.state.busy:
                self._interrupt_settled.clear()

        # Issue the interrupt *before* poking the parked worker, so the
        # wind-down is at least in flight before a new turn can be started.
        if self.client:
            try:
                await self.client.interrupt()
            except Exception:
                self._interrupt_settled.set()
        else:
            self._interrupt_settled.set()

        if not live:
            try:
                self.event_queue.put_nowait(("wakeup", "interrupt"))
            except Exception:
                pass

    async def get_mcp_status(self) -> list[dict[str, Any]]:
        """Return the live status of configured MCP servers.

        Queries the CLI via the SDK's ``get_mcp_status`` control request and
        returns the ``mcpServers`` list (each a dict with name/status/tools/…).
        Raises if the SDK isn't connected or the client lacks the method.
        """
        if self.client is None:
            raise RuntimeError("SDK not connected")
        if not hasattr(self.client, "get_mcp_status"):
            raise RuntimeError("this SDK version has no MCP status API")
        resp = await self.client.get_mcp_status()
        if isinstance(resp, dict):
            return list(resp.get("mcpServers", []) or [])
        return []

    async def reconnect_mcp_server(self, name: str) -> None:
        """Reconnect a failed / disconnected MCP server."""
        if self.client is None:
            raise RuntimeError("SDK not connected")
        if not hasattr(self.client, "reconnect_mcp_server"):
            raise RuntimeError("this SDK version has no MCP reconnect API")
        await self.client.reconnect_mcp_server(name)

    async def toggle_mcp_server(self, name: str, enabled: bool) -> None:
        """Enable or disable an MCP server (disconnects / reconnects it)."""
        if self.client is None:
            raise RuntimeError("SDK not connected")
        if not hasattr(self.client, "toggle_mcp_server"):
            raise RuntimeError("this SDK version has no MCP toggle API")
        await self.client.toggle_mcp_server(name, enabled)

    async def start(self) -> None:
        """Launch the worker loop as a background task."""
        # Mark "connecting" immediately so the browser's very first status
        # update (which can arrive before the worker task runs connect()) shows
        # "connecting…" instead of a misleading "idle".  The SDK connect is the
        # slow part of startup; the HTTP server and page are already up, so this
        # tells the user the orchestrator is still initialising.
        self.state.connecting = True
        if self.state.connect_started_at is None:
            self.state.connect_started_at = time.monotonic()

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
        """Gracefully shut down.

        **Shielded**, so a cancellation aimed at whoever called us cannot leave
        a ``claude.exe`` running.  Shutdown here is not a request that can be
        abandoned half-way: the interesting work (killing the CLI) is at the
        *end* of the sequence, so a ``CancelledError`` arriving at the first
        await skips precisely the part that matters and leaves a live agent
        behind with no record that it happened.  That is exactly how the idle
        teardown leaked 19 subprocesses (see ``_cancel_idle_timer``); the
        self-cancel is fixed at its source, but a shutdown path whose
        correctness depends on never being cancelled is a trap, so the
        guarantee is made here too.

        The caller still sees the cancellation — the shield re-raises it — but
        the teardown itself runs to completion in the background.
        """
        await asyncio.shield(self._shutdown())

    async def _shutdown(self) -> None:
        """The actual shutdown sequence.  Never raises; see :meth:`stop`."""
        try:
            self.stop_event.set()
            self._cancel_compact_turn_end_timer()
            self._cancel_wakeup()
            self.event_queue.put_nowait(("quit", ""))
            try:
                if self._worker_task and not self._worker_task.done():
                    await cancel_and_join(self._worker_task, "worker")
            finally:
                # Unconditional: a worker that refuses to die must not also
                # cost us the subprocess.
                await self.disconnect()
        except Exception:
            # A shielded task's exception would otherwise surface as a bare
            # "Task exception was never retrieved" with no context.
            log.exception("bridge shutdown failed")
