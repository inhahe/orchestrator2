"""FastAPI + WebSocket server — entry point for the orchestrator2 web UI.

Binds all backend modules together:
- ``config`` / ``state`` for startup configuration and runtime state
- ``session`` for JSONL history loading
- ``commands`` for slash-command dispatch
- ``sdk_bridge`` for Claude Agent SDK integration
- ``tool_manager`` for structured tool tracking

WebSocket protocol:
  Client → Server:
    {"type": "message", "text": "..."}          user prompt
    {"type": "command", "text": "/help"}         slash command
    {"type": "interrupt"}                        abort current turn
    {"type": "permission_response", "allow": T}  tool-permission answer

  Server → Client:
    assistant_text, tool_use, tool_result, thinking,
    bg_started, bg_complete, turn_start, turn_end,
    system_msg, status_update, panel_update, permission_request,
    bell, clear_screen, command_data, history, completion_list
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import logging.handlers
import os
import socket as _socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import dataclasses
import webbrowser

# NOTE: fastapi / uvicorn / sdk_bridge (which pulls in claude_agent_sdk → mcp,
# ~0.5s on its own) are intentionally NOT imported at module top.  Importing
# them here would make ``import server`` cost ~1.1s — a cost the --detach
# launcher parent pays in full even though it only binds a port, spawns the
# child and opens the browser (it never serves HTTP itself).  They're imported
# lazily instead: fastapi/uvicorn in ``build_app()`` / ``main()`` and
# sdk_bridge in the deferred bridge startup.  This keeps ``import server`` at
# ~0.2s so the browser tab appears fast.
#
# TYPE_CHECKING-only imports so annotations (all strings under
# ``from __future__ import annotations``) still resolve for tooling without
# paying the runtime import cost.
from typing import TYPE_CHECKING
if TYPE_CHECKING:  # pragma: no cover
    from fastapi import Request, WebSocket, WebSocketDisconnect  # noqa: F401
    from fastapi.responses import (  # noqa: F401
        FileResponse, HTMLResponse, RedirectResponse, Response,
    )
    from sdk_bridge import SDKBridge  # noqa: F401

from config import (
    Config,
    DEFAULT_EXTERNAL_PASSWORD,
    DEFAULT_PORT,
    _PICKER_SENTINEL,
    fetch_available_models,
    parse_args,
)
from state import (
    State,
    config_dir_path,
    detect_account_info,
    init_state_from_config,
    reset_rate_limit,
    state_to_panels_dict,
    state_to_status_dict,
)
from session import (
    find_most_recent_session_for_cwd,
    find_session_cwd,
    find_session_dir,
    list_projects,
    list_sessions_for_project,
    load_persisted_queue,
    read_session_title,
    render_session_history,
    save_persisted_queue,
    write_session_title,
)
from commands import (
    classify,
    format_mcp_status,
    get_command_completions,
    try_immediate_command,
)
from tool_manager import format_tool_header
from theme import (
    Theme,
    load_theme,
    save_theme,
    load_saved_colors,
    save_color,
    remove_saved_color,
    PRESET_THEMES,
)
import auth

log = logging.getLogger("orchestrator2")


def _ensure_logged_in(*, block: bool) -> bool:
    """Auto-login to the Claude account (the way Claude Code does) if needed.

    Runs under the CLAUDE_CONFIG_DIR already pinned by ``main()``.  When not
    signed in, spawns ``claude auth login`` in a visible console.  With
    ``block=True`` (the --detach parent) it then waits until the login
    completes so the headless child starts already authenticated; with
    ``block=False`` it returns immediately and the worker's connect-retry
    picks up the credentials once they land.
    """
    if auth.is_logged_in():
        return True
    ok, msg = auth.launch_login()
    print(f"Not signed in to Claude — {msg}")
    if not ok or not block:
        return auth.is_logged_in()
    print("Waiting for Claude login to complete...")
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        if auth.is_logged_in():
            print("Login complete.")
            return True
        time.sleep(1.5)
    print("Login not completed in time — continuing anyway.")
    return auth.is_logged_in()

# ---------------------------------------------------------------------------
# Global singletons (initialised in lifespan)
# ---------------------------------------------------------------------------

config: Config | None = None
state: State | None = None
bridge: SDKBridge | None = None
theme: Theme | None = None

# True when --resume was passed without a session ID (picker sentinel).
# The SDK start is deferred until the user picks a session via the UI.
_picker_mode: bool = False

# Connected WebSocket clients.
#
# Multi-session hub: the server can host several live sessions, each a
# ``SessionRuntime`` with its own client set.  ``_ws_clients`` is the canonical
# "all connected tabs" set (every socket, regardless of which runtime it's
# attached to) — used for server-wide broadcasts and shutdown accounting.
# ``_ws_runtime`` maps a socket to the runtime it's currently attached to.
# ``lobby_clients`` holds tabs sitting at the lobby, not attached to any
# session.
from session_runtime import SessionRuntime  # noqa: E402

_ws_clients: set[WebSocket] = set()
_ws_lock = asyncio.Lock()

runtimes: dict[str, SessionRuntime] = {}
lobby_clients: set[WebSocket] = set()
# Tabs that are attached to a session but have the lobby *overlay* open, so
# they want live ``session_list`` pushes too (viewer counts, busy dots, new
# sessions appearing).  Distinct from ``lobby_clients``, which are detached
# tabs sitting at the lobby with no session.
_lobby_watchers: set[WebSocket] = set()
_ws_runtime: dict[WebSocket, SessionRuntime] = {}
_default_runtime: SessionRuntime | None = None


def _lobby_targets() -> set[WebSocket]:
    """Every tab that should receive live session-list broadcasts."""
    return lobby_clients | _lobby_watchers


def _runtime_for_ws(ws: WebSocket) -> SessionRuntime | None:
    """The session runtime the socket is currently attached to (if any)."""
    return _ws_runtime.get(ws) or _default_runtime


def _runtime_by_rid(rid: str | None) -> SessionRuntime | None:
    """Look up a live runtime by its internal ``rid`` (or None)."""
    if not rid:
        return None
    return runtimes.get(rid)


# ---------------------------------------------------------------------------
# Lobby — the list of joinable sessions (live runtimes + recent on-disk)
# ---------------------------------------------------------------------------

# The recent-on-disk scan hits the filesystem, so cache it briefly: the lobby
# is refreshed every ticker cycle while tabs sit in it, and rescanning every
# couple of seconds is wasteful.
_disk_sessions_cache: tuple[float, list[dict[str, Any]]] | None = None
_DISK_CACHE_TTL = 8.0


def _recent_disk_sessions(limit: int = 40) -> list[dict[str, Any]]:
    """Recent on-disk sessions for this account, excluding ones already live.

    Cached for ``_DISK_CACHE_TTL`` seconds to keep lobby refreshes cheap.
    """
    global _disk_sessions_cache
    now = time.monotonic()
    if _disk_sessions_cache is not None:
        stamp, cached = _disk_sessions_cache
        if now - stamp < _DISK_CACHE_TTL:
            return cached

    live_ids = {
        getattr(rt.state, "session_id", None)
        for rt in runtimes.values()
    }
    # Scan every Claude account/config dir on this machine — not just the hub's
    # own — so sessions launched under a different account still appear in the
    # lobby.  Each session is tagged with the ``account`` (config dir) it lives
    # under so opening it re-launches the runtime with the right account.
    try:
        from copy_session import discover_claude_dirs
        config_dirs = [str(p) for p in discover_claude_dirs()]
    except Exception:
        log.warning("failed to discover Claude config dirs", exc_info=True)
        config_dirs = [os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")]

    out: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    for cdir in config_dirs:
        try:
            for proj in list_projects(config_dir=cdir):
                for sess in list_sessions_for_project(Path(proj["project_dir"])):
                    sid = sess.get("session_id")
                    if not sid or sid in live_ids or sid in seen_sids:
                        continue
                    seen_sids.add(sid)
                    out.append({
                        "session_id": sid,
                        "title": sess.get("title"),
                        "cwd": proj.get("cwd"),
                        "account": cdir,
                        "mtime": sess.get("mtime", 0),
                        "age": _format_age(sess.get("mtime", 0)),
                        "first_user_msg": _truncate(sess.get("first_user_msg"), 100),
                    })
        except Exception:
            log.warning("failed to scan disk sessions in %s", cdir, exc_info=True)
    out.sort(key=lambda s: s.get("mtime", 0), reverse=True)
    out = out[:limit]
    # Persist any newly-resolved titles so the next process start is fast.
    try:
        from session import flush_title_index
        flush_title_index()
    except Exception:
        log.debug("title index flush failed", exc_info=True)
    _disk_sessions_cache = (now, out)
    return out


async def _session_list_payload() -> dict[str, Any]:
    """The ``session_list`` message: running runtimes + recent disk sessions.

    The recent-disk scan now walks every Claude account on the machine, which
    can touch many JSONL files, so it's run in a thread to avoid blocking the
    event loop (a stalled loop froze every session, not just the lobby).
    """
    running = [rt.meta() for rt in runtimes.values()]
    running.sort(key=lambda m: m.get("last_activity", 0), reverse=True)
    loop = asyncio.get_running_loop()
    recent = await loop.run_in_executor(None, _recent_disk_sessions)
    return {
        "type": "session_list",
        "running": running,
        "recent": recent,
    }


async def lobby_broadcast(msg: dict[str, Any]) -> None:
    """Send *msg* to every lobby tab and every tab watching the lobby overlay."""
    targets = _lobby_targets()
    if not targets:
        return
    data = json.dumps(msg, default=str)
    stale: list[WebSocket] = []
    for ws in list(targets):
        try:
            await ws.send_text(data)
        except Exception:
            stale.append(ws)
    for ws in stale:
        lobby_clients.discard(ws)
        _lobby_watchers.discard(ws)


async def _push_session_list(target: WebSocket | None = None) -> None:
    """Push the current session list to one tab, or to all lobby tabs."""
    payload = await _session_list_payload()
    if target is not None:
        await send_to(target, payload)
    else:
        await lobby_broadcast(payload)


def _account_email_for(claude_dir: Path) -> str | None:
    """Read the signed-in email for a ``.claude`` account directory.

    Mirrors ``state.detect_account_info`` but for an *arbitrary* config dir
    (not just the process env), so ``/switch`` can label each account.
    """
    try:
        with open(claude_dir / ".claude.json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if isinstance(oauth, dict):
        em = oauth.get("emailAddress")
        if isinstance(em, str) and em:
            return em
    return None


def _same_config_dir(a: str | None, b: str | None) -> bool:
    """True when two runtime config dirs resolve to the same account store."""
    try:
        return config_dir_path(a).resolve() == config_dir_path(b).resolve()
    except OSError:
        return (a or "") == (b or "")


async def _propagate_login_to_siblings(
    origin: "SessionRuntime", cfg_dir: str | None, info: dict,
) -> None:
    """Refresh account / auth / rate-limit on sibling sessions sharing a login.

    A ``/login`` rewrites the *shared* ``<config-dir>/.credentials.json``, so
    every other live runtime on the same config dir is re-authenticated to the
    same account.  Update their ``account`` field, clear any sticky auth error
    and stale rate-limit rejection, and push a status refresh — so re-logging in
    one tab fixes them all instead of the user repeating it per session.
    """
    for other in list(runtimes.values()):
        if other is origin or other.state is None or other.config is None:
            continue
        if not _same_config_dir(getattr(other.config, "config_dir", None), cfg_dir):
            continue
        other.state.account = info
        other.state.auth_error = False
        reset_rate_limit(other.state)
        try:
            await other.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(other.state, other.config),
                "panels": _enrich_panels(state_to_panels_dict(other.state)),
            })
        except Exception as exc:
            log.warning("sibling login-propagate broadcast failed: %r", exc)


async def _watch_login(rt: "SessionRuntime") -> None:
    """After ``/login`` launches the browser OAuth, wait for it to finish.

    The interactive login runs in a separate console and rewrites
    ``<config-dir>/.credentials.json`` when it completes.  We poll for that
    file's mtime advancing (reliable, unlike ``claude auth status``), then
    refresh ``state.account`` from the freshly-written ``.claude.json``, clear
    any sticky auth error, and broadcast: a confirmation naming the account plus
    a ``status_update`` so the toolbar's *account* field updates immediately.
    """
    state = rt.state
    config = rt.config
    cfg_dir = getattr(config, "config_dir", None)

    start_mtime = await asyncio.to_thread(auth.credentials_mtime, cfg_dir)
    deadline = time.monotonic() + 300.0  # 5 min to complete the browser flow
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        mtime = await asyncio.to_thread(auth.credentials_mtime, cfg_dir)
        # Completed when the credentials file is (re)written after we started.
        if mtime is not None and (start_mtime is None or mtime > start_mtime):
            info = await asyncio.to_thread(detect_account_info, cfg_dir)
            state.account = info
            state.auth_error = False
            # A completed sign-in (typically to a *different* account) makes the
            # previous login's rate-limit state moot — not just a "rejected"
            # lockout but also the per-window utilisation percentages shown in
            # the toolbar's usage display.  Fully reset it so nothing from the
            # old account lingers after re-authenticating.
            reset_rate_limit(state)
            email = info.get("email")
            who = f" as {email}" if email else ""
            await rt.broadcast({
                "type": "system_msg", "subtype": "info",
                "data": {"message": f"Signed in to Claude{who}. "
                                    f"Run /connect to reconnect this session."},
            })
            await rt.broadcast({
                "type": "status_update",
                "status": state_to_status_dict(state, config),
                "panels": _enrich_panels(state_to_panels_dict(state)),
            })
            # The re-login rewrote the *shared* <config-dir>/.credentials.json,
            # so every OTHER live session on the same config dir is now
            # re-authenticated to this account too.  Propagate the account /
            # auth-error / stale-rate-limit refresh to them so the user doesn't
            # have to repeat /login (or /connect) in each sibling tab.
            await _propagate_login_to_siblings(rt, cfg_dir, info)
            return

    await rt.broadcast({
        "type": "system_msg", "subtype": "warning",
        "data": {"message": "Didn't detect sign-in completing within 5 minutes. "
                            "If you finished, run /connect; otherwise /login force."},
    })


def _switch_accounts_payload(current_cfg: str | None) -> list[dict[str, Any]]:
    """Build the account list for the ``/switch`` picker.

    Each entry: ``{config_dir, name, email, is_current}``.  Scans the same
    ``.claude*`` directories the copy TUI discovers, reusing
    ``copy_session.discover_claude_dirs``.
    """
    from copy_session import discover_claude_dirs
    cur_norm = os.path.normpath(current_cfg) if current_cfg else None
    accounts: list[dict[str, Any]] = []
    for d in discover_claude_dirs():
        accounts.append({
            "config_dir": str(d),
            "name": d.name,
            "email": _account_email_for(d),
            "is_current": cur_norm is not None
            and os.path.normpath(str(d)) == cur_norm,
        })
    return accounts

# Background tasks for periodic updates.
_ticker_task: asyncio.Task | None = None

# Auto-shutdown: when all tabs close, shut down after a grace period.
_shutdown_timer: asyncio.Task | None = None
_has_had_clients: bool = False          # True once the first tab connects
_SHUTDOWN_GRACE_SECONDS = 30

# Signalled by the lifespan once the server is fully initialised and ready
# to serve requests.  The browser-open thread waits on this before opening.
import threading as _threading
_server_ready = _threading.Event()

# Set once the SDK bridge has been constructed (and started, when not in
# picker mode).  The WebSocket handler awaits this before touching ``bridge``
# — the bridge is now built off the hot startup path, so there's a brief
# window after the page loads where ``bridge is None``.
_bridge_ready = asyncio.Event()

# Resume id stashed by lifespan for _deferred_bridge_startup to apply once the
# bridge object exists.
_pending_resume_id: str | None = None


# ---------------------------------------------------------------------------
# Broadcaster — sends a dict to all connected clients
# ---------------------------------------------------------------------------

async def broadcast(msg: dict[str, Any]) -> None:
    """Send *msg* as JSON to **every** connected tab, across all runtimes.

    Server-wide only (e.g. ``server_shutdown``).  Per-session updates must go
    through the owning runtime's ``rt.broadcast`` so they reach just that
    session's viewers, not tabs watching other sessions.
    """
    if not _ws_clients:
        return
    data = json.dumps(msg, default=str)
    # Snapshot to avoid mutation during iteration.
    async with _ws_lock:
        clients = list(_ws_clients)
    stale: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            stale.append(ws)
    if stale:
        async with _ws_lock:
            for ws in stale:
                _ws_clients.discard(ws)


async def _broadcast_shutdown(reason: str) -> None:
    """Tell every connected browser tab the server is about to exit."""
    await broadcast({"type": "server_shutdown", "reason": reason})


async def send_to(ws: WebSocket, msg: dict[str, Any]) -> None:
    """Send *msg* as JSON to a single WebSocket client."""
    try:
        await ws.send_text(json.dumps(msg, default=str))
    except Exception:
        # Log if this was a history payload — those failures are hard to
        # diagnose because the client just sees a blank screen.
        if msg.get("type") == "history":
            log.warning("failed to send history message (%d items) to client",
                        len(msg.get("messages", [])), exc_info=True)
        pass


# ---------------------------------------------------------------------------
# Panel enrichment — add headers to active_tools for the frontend
# ---------------------------------------------------------------------------

def _enrich_panels(panels: dict[str, Any]) -> dict[str, Any]:
    """Add ``header`` dicts to active_tools entries for the frontend."""
    for t in panels.get("active_tools", []):
        if "header" not in t:
            t["header"] = format_tool_header(t.get("name", "?"), t.get("input") or {})
    return panels


# ---------------------------------------------------------------------------
# Status ticker — periodic status + panel pushes
# ---------------------------------------------------------------------------

async def _status_ticker() -> None:
    """Push status_update + panel_update to each session's viewers every ~2s.

    Iterates every live runtime so, once the hub hosts several sessions, each
    set of viewers gets its own session's status.  In the single-session case
    there's just the default runtime (whose state is the process globals).
    """
    while True:
        await asyncio.sleep(2.0)
        for rt in list(runtimes.values()):
            st, cfg = rt.state, rt.config
            if st is None or cfg is None:
                continue
            if not rt.clients:
                continue
            try:
                # Expire panel grace items.
                now = time.monotonic()
                expired = [k for k, v in st.completed_panel_tools.items()
                           if v.get("grace_end", 0) < now]
                for k in expired:
                    st.completed_panel_tools.pop(k, None)
                expired_bg = [k for k, v in st.completed_panel_bg.items()
                              if v.get("grace_end", 0) < now]
                for k in expired_bg:
                    st.completed_panel_bg.pop(k, None)

                panels = _enrich_panels(state_to_panels_dict(st))
                await rt.broadcast({
                    "type": "status_update",
                    "status": state_to_status_dict(st, cfg),
                    "panels": panels,
                })

                # Catch-all bell flush: bells rung from sync contexts (e.g.
                # rate-limit hits) set state.pending_bell but have no async
                # path to broadcast it.  Flush here so they reach the frontend
                # within one tick instead of waiting for the next turn to end.
                if st.pending_bell:
                    event = st.pending_bell
                    st.pending_bell = None
                    await rt.broadcast({"type": "bell", "event": event})
            except Exception:
                log.exception("ticker error")

        # Keep lobby tabs' session list live (viewer counts, busy dots).
        if _lobby_targets():
            try:
                await _push_session_list()
            except Exception:
                log.exception("lobby ticker error")


# ---------------------------------------------------------------------------
# Queued-prompt disk persistence
# ---------------------------------------------------------------------------

def _attach_queue_persistence(st, cwd: str) -> None:
    """Load any queue persisted for *cwd* into *st.queued_prompts*, then wire
    the deque's on_change callback so future mutations are saved to disk.

    Persisting the queue lets typed-but-not-yet-run prompts survive a full
    server restart (browser reload already survives via server-side state).
    """
    try:
        saved = load_persisted_queue(cwd)
    except Exception:
        saved = []
    if saved:
        # Populate before wiring on_change so the initial load doesn't trigger
        # a redundant re-save of what we just read.
        st.queued_prompts.extend(saved)
    st.queued_prompts.on_change = (
        lambda: save_persisted_queue(cwd, list(st.queued_prompts))
    )


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

import contextlib
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on startup, clean up on shutdown."""
    global config, state, bridge, theme, _ticker_task, _picker_mode
    global _default_runtime

    # --- Startup ---
    # This runs *before* uvicorn begins accepting connections, so keep it
    # cheap: no heavy imports, no SDK connect.  The single biggest startup
    # cost — importing sdk_bridge (→ claude_agent_sdk → mcp) and spawning the
    # Claude CLI — is pushed into a background task (_deferred_bridge_startup)
    # that runs *after* we yield, so the page serves immediately and shows a
    # "connecting…" status while the SDK finishes wiring up.
    if config is None:
        config = parse_args()
    state = init_state_from_config(config)
    _attach_queue_persistence(state, config.cwd)
    theme = load_theme()

    # The default session runtime wraps the process-wide globals and is the
    # session tabs auto-attach to on connect.  It has its own ``clients`` set
    # (per-runtime broadcast targets only its viewers); ``_ws_clients`` tracks
    # *all* connected tabs across every runtime + the lobby, for the global
    # ``broadcast()`` (server-wide messages) and auto-shutdown accounting.
    _default_runtime = SessionRuntime(config=config, state=state)
    runtimes[_default_runtime.rid] = _default_runtime

    _ticker_task = asyncio.create_task(_status_ticker(), name="status-ticker")

    # Warm the live model-list cache off the event loop so /model (which runs
    # on the loop) can read it without blocking on a network call.  Best
    # effort — falls back to the hardcoded list if this fails.
    asyncio.create_task(
        asyncio.to_thread(fetch_available_models), name="warm-model-cache",
    )

    # Seed session_id early (cheap disk reads) so the browser gets history on
    # first connect, even before the deferred bridge is up.  The resume id is
    # stashed for _deferred_bridge_startup to apply once the bridge exists.
    global _pending_resume_id
    _pending_resume_id = None
    if config.resume and config.resume != _PICKER_SENTINEL:
        # Resolve the resume value to a real session UUID — it might be a
        # title/name rather than an ID (e.g. ``--resume fastpyb``).
        _resolved_resume = config.resume
        if not find_session_dir(config.resume, config.config_dir):
            _match = _find_session_by_name(config.resume)
            if _match:
                _resolved_resume = _match
                config = dataclasses.replace(config, resume=_match)
        state.session_id = _resolved_resume
        state.session_title = read_session_title(_resolved_resume, config.config_dir)
        _pending_resume_id = _resolved_resume
    elif not config.no_continue:
        recent = find_most_recent_session_for_cwd(config.cwd)
        if recent is not None:
            state.session_id = recent.stem
            state.session_title = read_session_title(recent.stem, config.config_dir)
            _pending_resume_id = recent.stem

    # The seeding block above may have rebound ``config`` (e.g. resolving a
    # --resume title to a real UUID via dataclasses.replace).  Re-point the
    # default runtime at the finalized config so its ``config`` isn't stale.
    _default_runtime.config = config

    # If --resume was passed without an argument, defer SDK start until the
    # user picks a session from the graphical picker.
    if config.resume == _PICKER_SENTINEL:
        _picker_mode = True
        log.info("picker mode — waiting for session selection on port %s", config.port)
        # Build the bridge off the hot path (but don't start it — the user
        # picks a session first via /api/resume).  _deferred_bridge_startup
        # sets _bridge_ready once the object exists, so the resume handler's
        # `await _bridge_ready.wait()` unblocks only when bridge is real.
        asyncio.create_task(_deferred_bridge_startup(start=False),
                            name="deferred-bridge")
    else:
        asyncio.create_task(_deferred_bridge_startup(start=True),
                            name="deferred-bridge")

    _server_ready.set()
    yield

    # --- Shutdown ---
    await _broadcast_shutdown("Server shutting down.")
    if bridge:
        await bridge.stop()
    if _ticker_task and not _ticker_task.done():
        _ticker_task.cancel()
        try:
            await _ticker_task
        except asyncio.CancelledError:
            pass


async def _deferred_bridge_startup(*, start: bool) -> None:
    """Import sdk_bridge, build the bridge and (optionally) start it.

    Runs as a background task scheduled from ``lifespan`` *after* it yields,
    so importing ``sdk_bridge`` (which pulls in claude_agent_sdk → mcp) and
    spawning the Claude CLI don't delay the HTTP server accepting connections.
    Sets ``_bridge_ready`` when done so the WebSocket handler can proceed.
    """
    global bridge
    try:
        from sdk_bridge import SDKBridge  # heavy import, kept off the hot path
        _bcast = _default_runtime.broadcast if _default_runtime is not None else broadcast
        bridge = SDKBridge(config=config, state=state, broadcaster=_bcast)
        if _default_runtime is not None:
            _default_runtime.bridge = bridge
        if _pending_resume_id:
            bridge._initial_resume_id = _pending_resume_id
        if start:
            await bridge.start()
            log.info("server started on port %s, cwd=%s",
                     getattr(config, "port", "?"), getattr(config, "cwd", "?"))
    except Exception:
        log.exception("deferred bridge startup failed")
    finally:
        _bridge_ready.set()


# ---------------------------------------------------------------------------
# Runtime lifecycle — spin up / tear down live sessions on demand
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _env_config_dir(config_dir: str | None):
    """Temporarily set ``CLAUDE_CONFIG_DIR`` for session-discovery calls.

    Cross-account hub sessions store their session files under a different
    config dir.  The session-discovery helpers in ``session.py`` read
    ``os.environ["CLAUDE_CONFIG_DIR"]`` to find the right directory.
    """
    if not config_dir:
        yield
        return
    orig = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = config_dir
    try:
        yield
    finally:
        if orig is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = orig


async def _create_runtime(
    *,
    cwd: str | None = None,
    resume: str | None = None,
    no_continue: bool = False,
    model: str | None = None,
    effort: str | None = None,
    config_dir: str | None = None,
) -> SessionRuntime:
    """Spin up a fresh live session runtime (config clone + state + bridge).

    * ``resume`` — resume a specific on-disk session id (implies no auto-continue).
    * ``no_continue`` — start empty (a brand-new session).
    * ``model`` / ``effort`` — per-session overrides from the launcher.
    * ``config_dir`` — ``CLAUDE_CONFIG_DIR`` override for cross-account sessions.
    * otherwise — continue the most recent session in *cwd*.

    Registers the runtime, starts its bridge, and refreshes the lobby.
    """
    assert config is not None
    resolved_cwd = (
        str(Path(cwd).resolve(strict=False)) if cwd else config.cwd
    )
    overrides: dict[str, Any] = {
        "cwd": resolved_cwd,
        "resume": resume,
        "no_continue": no_continue or bool(resume),
        "copy": False,
    }
    if model:
        overrides["model"] = model
    if effort:
        overrides["effort"] = effort
    if config_dir:
        overrides["config_dir"] = config_dir
    cfg = dataclasses.replace(config, **overrides)
    # Build the state *inside* the session's config-dir scope so account
    # detection (detect_account_info / detect_subscription*) reads the
    # launched session's CLAUDE_CONFIG_DIR — not the hub's.  Otherwise a
    # cross-account session shows the hub account's email/subscription.
    with _env_config_dir(config_dir):
        st = init_state_from_config(cfg)
        _attach_queue_persistence(st, cfg.cwd)
    rt = SessionRuntime(config=cfg, state=st)
    runtimes[rt.rid] = rt

    from sdk_bridge import SDKBridge  # already imported by now; cheap
    br = SDKBridge(config=cfg, state=st, broadcaster=rt.broadcast)
    rt.bridge = br

    # Decide which on-disk session (if any) seeds this runtime.
    # Session files live under CLAUDE_CONFIG_DIR, so scope the lookup to
    # the session's account when it differs from the hub's.
    seed_id: str | None = None
    with _env_config_dir(config_dir):
        if resume:
            seed_id = resume
        elif not no_continue:
            recent = find_most_recent_session_for_cwd(resolved_cwd)
            if recent is not None:
                seed_id = recent.stem
        if seed_id:
            st.session_id = seed_id
            br._initial_resume_id = seed_id

    if seed_id:
        # Read the display title in the background.  Scanning the session's
        # JSONL (which can be hundreds of MB — a 276 MB file takes ~2.7s) would
        # block the event loop right here, delaying this tab's attach + history
        # AND freezing every other tab and the status ticker.  The title isn't
        # needed to render history or interact, so fetch it off the hot path and
        # push a status_update + lobby refresh once it's known.
        asyncio.create_task(
            _load_runtime_title(rt, seed_id, config_dir),
            name=f"title-{rt.rid}",
        )

    await br.start()
    log.info("runtime %s started (cwd=%s, resume=%s)",
             rt.rid, resolved_cwd, seed_id)
    await _push_session_list()   # lobby tabs see the new session appear
    return rt


async def _load_runtime_title(
    rt: SessionRuntime, sid: str, config_dir: str | None
) -> None:
    """Read a runtime's session title off the event loop and publish it.

    Called as a background task from ``_create_runtime`` so the (potentially
    multi-second) JSONL scan never blocks attach/history.  Pushes a fresh
    status_update to the runtime's viewers and refreshes the lobby once known.
    """
    try:
        title = await asyncio.to_thread(read_session_title, sid, config_dir)
    except Exception:
        log.debug("background title read failed for %s", sid, exc_info=True)
        return
    if not title:
        return
    st, cfg = rt.state, rt.config
    if st is None or cfg is None:
        return
    # Don't clobber a title the user set via /rename (or one the SDK-init path
    # already resolved) while we were reading.
    if st.pending_rename or st.session_title:
        return
    st.session_title = title
    try:
        await rt.broadcast({
            "type": "status_update",
            "status": state_to_status_dict(st, cfg),
            "panels": _enrich_panels(state_to_panels_dict(st)),
        })
        await _push_session_list()
    except Exception:
        log.debug("title publish failed for %s", sid, exc_info=True)


async def _teardown_runtime(rt: SessionRuntime) -> None:
    """Stop a runtime's bridge and drop it from the registry.

    The default runtime is the process's primary session and is never torn
    down here — the server-level auto-shutdown handles the whole-process case.
    """
    if rt is _default_runtime:
        return
    if runtimes.get(rt.rid) is not rt:
        return  # already gone
    runtimes.pop(rt.rid, None)
    _cancel_idle_timer(rt)
    if rt.bridge is not None:
        try:
            await rt.bridge.stop()
        except Exception:
            log.warning("error stopping bridge for runtime %s",
                        rt.rid, exc_info=True)
    log.info("runtime %s torn down", rt.rid)
    await _push_session_list()


def _cancel_idle_timer(rt: SessionRuntime) -> None:
    """Cancel a runtime's pending idle-teardown timer, if any."""
    if rt.idle_timer is not None:
        rt.idle_timer.cancel()
        rt.idle_timer = None
    rt.idle_deadline = None


def _maybe_start_idle_timer(rt: SessionRuntime) -> None:
    """Start the idle-teardown countdown for a viewer-less runtime.

    No-op for the default runtime (never idle-torn-down), for runtimes that
    still have viewers, or when idle teardown is disabled (timeout <= 0).
    """
    if rt is _default_runtime or rt.clients:
        return
    timeout = getattr(config, "session_idle_timeout", 300) if config else 300
    if timeout <= 0:
        return
    _cancel_idle_timer(rt)
    rt.idle_deadline = time.time() + timeout
    rt.idle_timer = asyncio.create_task(
        _idle_teardown_after(rt, timeout), name=f"idle-{rt.rid}",
    )


async def _idle_teardown_after(rt: SessionRuntime, timeout: int) -> None:
    """Wait out the idle grace period, then tear the runtime down."""
    try:
        await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        return
    if not rt.clients and runtimes.get(rt.rid) is rt:
        log.info("runtime %s idle for %ds — tearing down", rt.rid, timeout)
        await _teardown_runtime(rt)


# ---------------------------------------------------------------------------
# Lazy route registry
# ---------------------------------------------------------------------------
#
# Routes are declared with the lightweight ``@_route(...)`` / ``@_ws_route(...)``
# decorators below, which only *record* the handler (no fastapi import needed).
# ``build_app()`` replays them onto a real FastAPI instance — called only by the
# process that actually serves HTTP (the child / non-detach path), so the
# --detach parent never imports fastapi.

# (method, path, kwargs, handler) for HTTP routes; (path, handler) for WS.
_HTTP_ROUTES: list[tuple[str, str, dict[str, Any], Any]] = []
_WS_ROUTES: list[tuple[str, Any]] = []

# Placeholder; the real FastAPI instance is created by build_app().
app: Any = None


def _route(method: str, path: str, **kwargs: Any):
    """Record an HTTP route without importing fastapi."""
    def deco(fn):
        _HTTP_ROUTES.append((method, path, kwargs, fn))
        return fn
    return deco


def _ws_route(path: str):
    """Record a WebSocket route without importing fastapi."""
    def deco(fn):
        _WS_ROUTES.append((path, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# LAN-only authentication middleware
# ---------------------------------------------------------------------------
#
# Connections from private/loopback IPs pass through freely.  Connections from
# public IPs require HTTP Basic Auth (password set via ``--external-password``
# or the ``ORCH2_EXTERNAL_PASSWORD`` environment variable; defaults to None
# which blocks ALL external access).  This protects the hub from the internet
# while keeping the LAN workflow frictionless.

_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(ip_str: str) -> bool:
    """Return True for loopback, LAN, and link-local addresses."""
    try:
        addr = ipaddress.ip_address(ip_str.split("%")[0])  # strip IPv6 zone id
        return any(addr in net for net in _PRIVATE_NETS)
    except ValueError:
        return False


_AUTH_COOKIE = "orch2_auth"


class _ExternalAuthMiddleware:
    """ASGI middleware: require Basic Auth for non-LAN clients.

    * LAN / loopback → pass through (no credentials needed).
    * External + correct password → pass through.
    * External + wrong / missing credentials → 401.
    * ``password is None`` → block ALL external access (no password set).

    A successful external HTTP auth also sets a session cookie holding a token
    derived from the password.  This is what makes the WebSocket work from
    outside the LAN: browsers cache HTTP Basic-Auth credentials but do **not**
    replay the ``Authorization`` header on a ``new WebSocket()`` upgrade
    (iOS Safari never does), so a password-protected hub would authenticate the
    page yet silently reject its socket (close 1008), leaving the app stuck
    "reconnecting" — which looks exactly like a rejected password.  Cookies, by
    contrast, *are* sent on same-origin WS upgrades, so once the page load sets
    the cookie the socket authenticates automatically.  A token (not the raw
    password) is stored so the secret never lands in a cookie jar or log.

    **Brute-force throttle.**  Wrong-password attempts are counted *globally*
    (one shared counter, NOT per source IP).  Per-IP throttling would be trivial
    to defeat with a botnet — each of thousands of addresses would get its own
    fresh attempt budget — so instead every wrong guess, from wherever, advances
    a single counter.  The first ``_FREE_ATTEMPTS`` failures cost nothing; after
    that the door is locked for an exponentially growing window
    (``_BASE_LOCKOUT * 2**(n-1)`` seconds, capped at ``_MAX_LOCKOUT``) during
    which every external request is refused with ``429`` *before* the password is
    even examined — so the whole hub allows only a handful of guesses per
    escalating window no matter how many machines are trying.  A single
    successful auth clears the counter.  (Trade-off: an attacker can keep the
    lockout armed and lock out the owner's *remote* access — but LAN/loopback
    always bypasses the throttle, and the cap keeps any single lockout ≤5 min.)
    Password / token comparisons use ``hmac.compare_digest`` so a network
    attacker can't recover the secret via response-timing analysis.
    """

    # Throttle tuning.  Small at first (a fat-fingered password shouldn't lock
    # things for long) then grows fast against a sustained guessing campaign.
    _FREE_ATTEMPTS = 5          # failures allowed before lockouts begin
    _BASE_LOCKOUT = 2.0         # seconds for the first lockout past the threshold
    _MAX_LOCKOUT = 300.0        # cap on the lockout window (5 min)

    def __init__(self, app: Any, *, password: str | None) -> None:
        self.app = app
        self.password = password
        # Deterministic token derived from the password: lets the cookie
        # authenticate without carrying the literal secret.
        self.token = (
            hashlib.sha256(("orch2-auth:" + password).encode()).hexdigest()
            if password is not None else None
        )
        # Global failure tracking (shared across all source IPs — see the
        # brute-force note above).  In-process only; a restart forgives.
        self._fails = 0
        self._locked_until = 0.0    # time.monotonic() deadline

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            client_ip = client[0] if client else "127.0.0.1"
            if not _is_private_ip(client_ip):
                # External connection — check credentials.
                if self.password is None:
                    # No password configured → external access is off.  Don't
                    # send a Basic-Auth challenge (it would prompt for a
                    # password that can never work); just refuse.
                    await self._reject(scope, receive, send,
                                       "External access is disabled.",
                                       challenge=False)
                    return
                # Locked out? Refuse before touching the password so a guesser
                # gets nothing (not even a timing signal) during the window.
                retry_after = self._lockout_remaining()
                if retry_after > 0:
                    await self._reject(
                        scope, receive, send,
                        f"Too many failed attempts. Try again in "
                        f"{int(retry_after) + 1}s.",
                        challenge=False, status=429, retry_after=retry_after)
                    return
                headers = dict(scope.get("headers", []))
                auth = headers.get(b"authorization", b"").decode("utf-8", "replace")
                qs = (scope.get("query_string") or b"").decode("utf-8", "replace")
                pw_param = dict(
                    p.split("=", 1) for p in qs.split("&") if "=" in p
                ).get("password")
                if pw_param is not None:
                    pw_param = unquote(pw_param)
                cookie_ok = self._cookie_ok(headers.get(b"cookie", b""))
                header_ok = self._check_basic_auth(auth)
                param_ok = pw_param is not None and hmac.compare_digest(
                    pw_param, self.password)
                if not (header_ok or param_ok or cookie_ok):
                    self._record_failure()
                    await self._reject(scope, receive, send,
                                       "Authentication required.")
                    return
                # Authenticated — forgive all prior failures.
                self._record_success()
                # If this HTTP request proved the password via the Basic header
                # or ?password= (i.e. not already via cookie), plant the auth
                # cookie so the follow-up WebSocket upgrade — on which the
                # browser won't resend Basic credentials — carries it.
                if scope["type"] == "http" and not cookie_ok:
                    send = self._send_with_cookie(scope, send)
        await self.app(scope, receive, send)

    def _lockout_remaining(self) -> float:
        """Seconds remaining on the global lockout window (0 if not locked)."""
        remaining = self._locked_until - time.monotonic()
        return remaining if remaining > 0 else 0.0

    def _record_failure(self) -> None:
        """Count a failed attempt and (past the threshold) arm the lockout."""
        self._fails += 1
        over = self._fails - self._FREE_ATTEMPTS
        if over > 0:
            window = min(self._BASE_LOCKOUT * (2 ** (over - 1)), self._MAX_LOCKOUT)
            self._locked_until = time.monotonic() + window

    def _record_success(self) -> None:
        """Clear the failure history after a valid credential."""
        self._fails = 0
        self._locked_until = 0.0

    def _check_basic_auth(self, auth_header: str) -> bool:
        """Validate ``Authorization: Basic <b64>`` against the configured password."""
        if not auth_header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        # Accept any username; only the password matters.
        parts = decoded.split(":", 1)
        return len(parts) == 2 and hmac.compare_digest(parts[1], self.password)

    def _cookie_ok(self, cookie_header: bytes) -> bool:
        """True when the request carries a valid ``orch2_auth`` cookie."""
        if not cookie_header or self.token is None:
            return False
        raw = cookie_header.decode("utf-8", "replace")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == _AUTH_COOKIE and hmac.compare_digest(value, self.token):
                return True
        return False

    def _send_with_cookie(self, scope: dict, send: Any) -> Any:
        """Wrap *send* to attach the auth ``Set-Cookie`` to the HTTP response."""
        secure = "https" in (scope.get("scheme") or "")
        cookie = (
            f"{_AUTH_COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else "")
        ).encode()

        async def wrapped(message: dict) -> None:
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"set-cookie", cookie)
                ]
            await send(message)

        return wrapped

    @staticmethod
    async def _reject(scope: dict, receive: Any, send: Any, message: str,
                      *, challenge: bool = True, status: int = 401,
                      retry_after: float | None = None) -> None:
        """Reject an unauthenticated external connection.

        *challenge* controls whether a ``WWW-Authenticate: Basic`` header is
        sent (which makes browsers show the login prompt).  It's suppressed
        when external access is disabled outright, so users aren't asked for a
        password that can never be accepted.  *status* / *retry_after* let the
        throttle emit a ``429`` with a ``Retry-After`` hint during a lockout.
        """
        if scope["type"] == "http":
            body = message.encode("utf-8")
            resp_headers = [
                [b"content-type", b"text/plain; charset=utf-8"],
                [b"content-length", str(len(body)).encode()],
            ]
            if challenge:
                resp_headers.append(
                    [b"www-authenticate", b'Basic realm="orchestrator2"'])
            if retry_after is not None:
                resp_headers.append(
                    [b"retry-after", str(int(retry_after) + 1).encode()])
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": resp_headers,
            })
            await send({"type": "http.response.body", "body": body})
        else:
            # WebSocket: accept then immediately close with a reason code.
            # (ASGI doesn't let us send an HTTP 401 on a WS scope.)
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008,
                        "reason": message})


def build_app() -> Any:
    """Construct the FastAPI application (imports fastapi lazily).

    Called only from the serving process.  Populates the fastapi response /
    request classes into module globals so route bodies and FastAPI's
    signature introspection (``get_type_hints``) resolve the string
    annotations left by ``from __future__ import annotations``.
    """
    global app, Request, WebSocket, WebSocketDisconnect
    global FileResponse, HTMLResponse, RedirectResponse, Response
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import (
        FileResponse, HTMLResponse, RedirectResponse, Response,
    )
    app = FastAPI(
        title="Orchestrator 2", docs_url=None, redoc_url=None, lifespan=lifespan,
    )
    for method, path, kwargs, fn in _HTTP_ROUTES:
        app.add_api_route(path, fn, methods=[method.upper()], **kwargs)
    for path, fn in _WS_ROUTES:
        app.add_api_websocket_route(path, fn)

    # Wrap the app with authentication for external connections.
    app = _ExternalAuthMiddleware(app, password=_resolve_external_password(config))

    return app


def _resolve_external_password(config: Config | None) -> str | None:
    """Resolve the non-LAN password from CLI flag, env var, then built-in default.

    Precedence:
      1. ``--external-password <pw>`` → use it verbatim.  An explicit empty
         string (``--external-password ""``) disables external access → None.
      2. ``ORCH2_EXTERNAL_PASSWORD`` env var (only when the flag wasn't given).
      3. the built-in default ``DEFAULT_EXTERNAL_PASSWORD``.

    The CLI flag defaults to None ("unspecified"), which is what lets us tell
    "not passed" (→ env/default) apart from "passed empty" (→ disable).
    """
    cfg_pw = getattr(config, "external_password", None) if config is not None else None
    if cfg_pw is None:
        return ((os.environ.get("ORCH2_EXTERNAL_PASSWORD") or "").strip()
                or DEFAULT_EXTERNAL_PASSWORD)
    if cfg_pw == "":
        return None   # explicitly disabled
    return cfg_pw


# ---------------------------------------------------------------------------
# Static files — serve the web UI
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"


@_route("get", "/", response_model=None)
async def index(request: Request):
    """Serve the frontend entry point, with theme CSS injected.

    Supports URL query parameters for session control:

    - ``/?resume``           — open the session picker
    - ``/?resume=<id>``      — resume a specific session (by id or title)
    - ``/?cwd=<path>``       — set the working directory
    - ``/?continue``         — continue most recent session in cwd (default)

    After handling a reconfiguration param the client is redirected to
    ``/`` so the URL stays clean and refreshing won't re-trigger.
    """
    # --- URL query-param driven reconfiguration ---
    resume_param = request.query_params.get("resume")
    cwd_param = request.query_params.get("cwd")

    # ?open / ?new / ?rid are *client-driven* session launches: the frontend
    # reads them (plus cwd/account as context) and sends an open/new WS message
    # that spins up a SEPARATE runtime.  In that case cwd is not a standalone
    # "reconfigure the default session in place" command, so we must NOT run the
    # legacy resume/cwd reconfigure below — doing so hijacks the default runtime
    # (repointing this tab's session at the opened session's cwd) instead of
    # creating an isolated one.  Fall through to serve the page untouched.
    _client_launch = (
        request.query_params.get("open") is not None
        or request.query_params.get("new") is not None
        or request.query_params.get("rid") is not None
    )

    if not _client_launch and resume_param is not None:
        if resume_param == "":
            # Bare ?resume → show the picker.
            return _serve_page("resume.html", fallback="<h1>Session Picker</h1>")
        # ?resume=<id> → reconfigure and redirect.
        ok, err = await _reconfigure(resume=resume_param, cwd=cwd_param)
        if not ok:
            return HTMLResponse(
                f"<h1>Resume failed</h1><p>{err}</p><p><a href='/?resume'>Pick another</a></p>",
                status_code=400,
            )
        return RedirectResponse("/", status_code=303)

    if not _client_launch and cwd_param is not None:
        ok, err = await _reconfigure(cwd=cwd_param)
        if not ok:
            return HTMLResponse(f"<h1>Error</h1><p>{err}</p>", status_code=400)
        return RedirectResponse("/", status_code=303)

    # --- Normal page serve ---
    if _picker_mode:
        return _serve_page("resume.html", fallback="<h1>Session Picker</h1>")

    return _serve_page("index.html", fallback="<h1>orchestrator2</h1><p>No index.html yet.</p>")


def _serve_page(filename: str, *, fallback: str = "") -> HTMLResponse:
    """Read an HTML file from STATIC_DIR, inject theme CSS, and return it."""
    page = STATIC_DIR / filename
    if not page.exists():
        return HTMLResponse(fallback)
    html = page.read_text(encoding="utf-8")
    if theme:
        css = theme.to_css_overrides()
        if css:
            inject = f'<style id="theme-overrides">\n{css}\n</style>'
            html = html.replace("</head>", f"{inject}\n</head>", 1)
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


async def _reconfigure(
    *,
    resume: str | None = None,
    cwd: str | None = None,
) -> tuple[bool, str | None]:
    """Stop the current bridge, update config, and start a new bridge.

    Returns ``(True, None)`` on success or ``(False, error_message)``
    on failure.
    """
    global config, state, bridge, _picker_mode

    if config is None:
        return False, "server not initialised"

    # --- Resolve cwd ---
    new_cwd = config.cwd
    if cwd:
        resolved = str(Path(cwd).resolve(strict=False))
        if not Path(resolved).exists():
            return False, f"directory not found: {cwd}"
        new_cwd = resolved

    # --- Resolve resume ---
    new_resume: str | None = None
    if resume:
        # Check if it's a UUID (or UUID prefix).
        session_dir = find_session_dir(resume, config.config_dir)
        if session_dir:
            new_resume = resume
        else:
            # Try matching as a title / name substring.
            match = _find_session_by_name(resume)
            if match:
                new_resume = match
            else:
                return False, f"session not found: {resume}"

    # --- Stop existing bridge ---
    if bridge is not None:
        try:
            await bridge.stop()
        except Exception:
            log.warning("error stopping bridge during reconfigure", exc_info=True)

    # --- Build new config ---
    overrides: dict[str, Any] = {"cwd": new_cwd}
    if new_resume:
        overrides["resume"] = new_resume
    config = dataclasses.replace(config, **overrides)

    # --- Reinitialise state + bridge ---
    from sdk_bridge import SDKBridge  # already imported once by now; cheap
    state = init_state_from_config(config)
    _attach_queue_persistence(state, config.cwd)
    _bcast = _default_runtime.broadcast if _default_runtime is not None else broadcast
    bridge = SDKBridge(config=config, state=state, broadcaster=_bcast)
    _bridge_ready.set()

    # Keep the default runtime pointing at the fresh config/state/bridge so the
    # ticker (which reads per-runtime state) and per-runtime routing see the
    # reconfigured session.  Its viewers (``_default_runtime.clients``) carry over.
    if _default_runtime is not None:
        _default_runtime.config = config
        _default_runtime.state = state
        _default_runtime.bridge = bridge

    if new_resume:
        state.session_id = new_resume
        title = read_session_title(new_resume, config.config_dir)
        if title:
            state.session_title = title
        session_cwd = find_session_cwd(new_resume)
        if session_cwd:
            resolved_cwd = str(Path(session_cwd).resolve(strict=False))
            if Path(resolved_cwd).exists():
                os.chdir(resolved_cwd)
        bridge._initial_resume_id = new_resume
    elif cwd:
        os.chdir(new_cwd)
        # Seed session from the new cwd (continue most recent).
        if not config.no_continue:
            recent = find_most_recent_session_for_cwd(new_cwd)
            if recent is not None:
                state.session_id = recent.stem
                state.session_title = read_session_title(recent.stem, config.config_dir)
                bridge._initial_resume_id = recent.stem

    _picker_mode = False
    await bridge.start()
    log.info("reconfigured: resume=%s cwd=%s", new_resume, new_cwd)
    return True, None


def _find_session_by_name(name: str) -> str | None:
    """Search all sessions for one whose title matches *name* (case-insensitive)."""
    name_lower = name.lower()
    for proj in list_projects():
        for sess in list_sessions_for_project(Path(proj["project_dir"])):
            title = sess.get("title") or ""
            if title.lower() == name_lower:
                return sess["session_id"]
    # Partial match fallback.
    for proj in list_projects():
        for sess in list_sessions_for_project(Path(proj["project_dir"])):
            title = sess.get("title") or ""
            if name_lower in title.lower():
                return sess["session_id"]
    return None


# Serve static files with explicit no-cache headers.  Using a regular
# route (not app.mount) so the Cache-Control header is guaranteed to
# be set — Starlette middleware doesn't reliably intercept sub-apps.
_MIME_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".html": "text/html",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}

@_route("get", "/static/{path:path}")
async def serve_static(path: str):
    """Serve a static file with no-cache headers."""
    file_path = STATIC_DIR / path
    if not file_path.exists() or not file_path.is_file():
        return Response("Not found", status_code=404)
    # Security: ensure the resolved path is inside STATIC_DIR.
    try:
        file_path.resolve().relative_to(STATIC_DIR.resolve())
    except ValueError:
        return Response("Forbidden", status_code=403)
    suffix = file_path.suffix.lower()
    media_type = _MIME_TYPES.get(suffix, "application/octet-stream")
    content = file_path.read_bytes()
    return Response(
        content,
        media_type=media_type,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@_route("post", "/api/shutdown")
async def api_shutdown() -> dict[str, Any]:
    """Gracefully shut down the whole server process (all sessions).

    Used by ``--detach`` when a new server needs to take over the port
    occupied by an old instance, by the lobby "Shut down server" button,
    and for manual cleanup.  Stops *every* runtime's bridge (not just the
    default) so no SDK subprocesses are orphaned, tells connected tabs the
    server is going down, then hard-exits.
    """
    log.info("shutdown requested via /api/shutdown (%d runtimes)", len(runtimes))
    try:
        await _broadcast_shutdown("Server shut down from the lobby.")
    except Exception:
        pass
    for rt in list(runtimes.values()):
        if rt.bridge is not None:
            try:
                await rt.bridge.stop()
            except Exception:
                pass
    # Schedule a hard exit after a short delay so the response gets sent.
    asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
    return {"ok": True, "message": "shutting down"}


@_route("post", "/api/restart")
async def api_restart() -> dict[str, Any]:
    """Restart the whole server process in place (kill + relaunch).

    Spawns a fresh headless server that inherits this process's launch
    command (so it picks up any code changes), waits for us to release the
    port (``--wait-port``), and resumes the current primary session.  We
    verify the replacement didn't immediately crash *before* exiting — if it
    did, we stay alive and report the error rather than leaving no server at
    all.  Once the child is confirmed running we tell tabs to reload, stop
    every bridge, and hard-exit so the child can take the port.
    """
    import subprocess

    port = config.port if config is not None else 8420

    # Rebuild argv from our own launch command, normalising the bits that
    # must change for an in-place restart.
    argv = sys.argv
    child = [sys.executable, argv[0]]
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("--detach", "--open", "--copy", "--wait-port",
                 "--skip-auto-login", "--no-continue", "--continue"):
            i += 1
            continue
        if a == "--port":
            i += 2
            continue
        if a.startswith("--port="):
            i += 1
            continue
        if a == "--resume":
            i += 1
            if i < len(argv) and not argv[i].startswith("-"):
                i += 1
            continue
        if a.startswith("--resume="):
            i += 1
            continue
        child.append(a)
        i += 1
    child.extend(["--port", str(port), "--skip-auto-login", "--wait-port"])
    # Resume exactly the current primary session on the fresh process.
    sid = state.session_id if state is not None else None
    if sid:
        child.extend(["--resume", sid])

    # Preserve the account so session discovery + the SDK subprocess use it.
    env = dict(os.environ)
    cfg_dir = getattr(config, "config_dir", None) if config is not None else None
    if cfg_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg_dir

    # Give the replacement its OWN visible console rather than running it
    # headless.  Two reasons, both learned the hard way:
    #   * The SDK's ``claude`` CLI subprocess inherits the server's console.
    #     A headless (CREATE_NO_WINDOW) server has none, so Windows hands the
    #     child ``claude`` its *own* console window — the stray "claude" window.
    #     A real console for the server means the subprocess draws into it
    #     (no stray window).
    #   * A headless restart that fails to come up dies silently, leaving no
    #     server and no visible error.  A console makes the failure visible.
    # The old parent is about to exit, so the child gets a fresh console
    # (CREATE_NEW_CONSOLE) rather than sharing ours (which would close with us).
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        kwargs["start_new_session"] = True

    try:
        # Inherit stdio so the server's (and claude's) output shows in the new
        # console — that's what makes a failed restart debuggable.
        proc = subprocess.Popen(child, env=env, **kwargs)
    except Exception as e:
        log.exception("restart: failed to spawn replacement")
        return {"ok": False, "error": f"spawn failed: {e}"}

    # Crash-check window: the child can't bind the port until we exit, so we
    # can't poll for "serving" — but we CAN catch an immediate crash (bad
    # argv, import error) so we never kill ourselves with no replacement.
    # The error text is visible in the child's console.
    for _ in range(15):  # ~3s
        await asyncio.sleep(0.2)
        rc = proc.poll()
        if rc is not None:
            log.error("restart: replacement exited early rc=%s", rc)
            return {"ok": False,
                    "error": f"replacement crashed (exit {rc})",
                    "detail": "See the new console window for the error."}

    log.info("restart: replacement alive (pid %s), handing over port %s",
             proc.pid, port)
    try:
        await broadcast({"type": "server_restart",
                         "reason": "Server restarting\u2026"})
    except Exception:
        pass
    for rt in list(runtimes.values()):
        if rt.bridge is not None:
            try:
                await rt.bridge.stop()
            except Exception:
                pass
    asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
    return {"ok": True, "message": "restarting", "port": port}


@_route("get", "/api/status")
async def api_status() -> dict[str, Any]:
    """Return current status bar + panel data."""
    if state is None or config is None:
        return {"status": {}, "panels": {}}
    return {
        "status": state_to_status_dict(state, config),
        "panels": state_to_panels_dict(state),
    }


@_route("get", "/api/ready")
async def api_ready() -> dict[str, Any]:
    """Trivial readiness probe used by the startup splash page.

    The instant-startup splash (served by a stdlib pre-server while
    fastapi/uvicorn load) polls this endpoint; once uvicorn is up it answers
    200 here and the splash redirects to the real app.  The pre-server itself
    answers 503 for this path, so a 200 unambiguously means "uvicorn serving".
    """
    return {"ready": True}


@_route("get", "/api/whoami")
async def api_whoami() -> dict[str, Any]:
    """Identify this server so a new launch can decide whether to reuse it.

    A launcher probes this before binding a port: if a live orchestrator2 is
    already serving, the launch joins it — even across accounts — instead of
    starting a second server.
    """
    return {
        "app": "orchestrator2",
        "pid": os.getpid(),
        "account": os.environ.get("CLAUDE_CONFIG_DIR") or "",
        "port": getattr(config, "port", None) if config else None,
        "sessions": len(runtimes),
    }


@_route("post", "/api/session/launch", response_model=None)
async def api_session_launch(body: dict[str, Any]) -> dict[str, Any]:
    """Spin up (or reuse) a session runtime in this hub; return its ``rid``.

    Called over HTTP by a second launch that found this hub already running.
    Mirrors the WebSocket ``open``/``new`` lobby actions but is reachable
    before any browser tab has attached.
    """
    if config is None:
        return {"ok": False, "error": "server not initialised"}
    cwd = body.get("cwd")
    resume = (body.get("resume") or "").strip() or None
    no_continue = bool(body.get("no_continue"))
    model = (body.get("model") or "").strip() or None
    effort = (body.get("effort") or "").strip() or None
    config_dir = (body.get("config_dir") or "").strip() or None

    # Resolve the on-disk session this launch would land on (an explicit
    # resume id, or — for a plain continue — the most recent session in cwd).
    # If a runtime is already hosting it, reuse that one so we never spin up a
    # second bridge against the same session file.
    target_sid = resume
    if target_sid is None and not no_continue:
        with _env_config_dir(config_dir):
            recent = find_most_recent_session_for_cwd(cwd or config.cwd)
        if recent is not None:
            target_sid = recent.stem
    if target_sid:
        existing = next(
            (r for r in runtimes.values()
             if getattr(r.state, "session_id", None) == target_sid),
            None,
        )
        if existing is not None:
            # Apply model/effort overrides from this launch to the existing
            # runtime so the launcher's --model / --effort take effect even
            # when the session is already live.  Reconnect the bridge so the
            # new options are picked up by the SDK immediately.
            need_reconnect = False
            if model and existing.state is not None and existing.state.model != model:
                existing.state.model = model
                need_reconnect = True
            if effort and existing.state is not None:
                new_effort = None if effort == "auto" else effort
                if existing.state.effort != new_effort:
                    existing.state.effort = new_effort
                    need_reconnect = True
            if need_reconnect and existing.bridge is not None:
                asyncio.create_task(existing.bridge.reconnect())
            return {"ok": True, "rid": existing.rid, "reused": True}
    try:
        rt = await _create_runtime(cwd=cwd, resume=resume, no_continue=no_continue,
                                   model=model, effort=effort,
                                   config_dir=config_dir)
    except Exception as exc:
        log.exception("hub session launch failed")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "rid": rt.rid}


@_route("get", "/api/completions")
async def api_completions() -> dict[str, Any]:
    """Return all slash commands for tab-complete."""
    return {"commands": get_command_completions()}


# ---------------------------------------------------------------------------
# Session picker API (for --resume graphical picker)
# ---------------------------------------------------------------------------

def _format_age(mtime: float) -> str:
    """Human-readable age from mtime (e.g. '3h ago', '2d ago')."""
    if mtime <= 0:
        return "?"
    delta = time.time() - mtime
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = int(delta / 60)
        return f"{m}m ago"
    if delta < 86400:
        h = int(delta / 3600)
        return f"{h}h ago"
    d = int(delta / 86400)
    if d == 1:
        return "yesterday"
    if d < 30:
        return f"{d}d ago"
    if d < 365:
        mo = int(d / 30)
        return f"{mo}mo ago"
    y = d / 365
    return f"{y:.1f}y ago"


def _format_size(size: int) -> str:
    """Human-readable file size."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _truncate(s: str | None, max_len: int) -> str:
    """Truncate with ellipsis."""
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "\u2026"


@_route("get", "/api/sessions")
async def api_sessions() -> dict[str, Any]:
    """Return all projects and their sessions for the picker UI."""
    projects = list_projects()
    result: list[dict[str, Any]] = []
    for proj in projects:
        sessions = list_sessions_for_project(Path(proj["project_dir"]))
        formatted: list[dict[str, Any]] = []
        for s in sessions:
            formatted.append({
                "session_id": s["session_id"],
                "title": s.get("title"),
                "first_user_msg": _truncate(s.get("first_user_msg"), 120),
                "last_user_msg": _truncate(s.get("last_user_msg"), 120),
                "age": _format_age(s.get("mtime", 0)),
                "msg_count": s.get("msg_count", 0),
                "size": _format_size(s.get("size", 0)),
                "mtime": s.get("mtime", 0),
            })
        result.append({
            "project_slug": proj["project_slug"],
            "cwd": proj.get("cwd"),
            "session_count": proj["session_count"],
            "newest_mtime": proj["newest_mtime"],
            "age": _format_age(proj["newest_mtime"]),
            "sessions": formatted,
        })
    return {"projects": result}


@_route("post", "/api/resume")
async def api_resume(body: dict[str, Any]) -> dict[str, Any]:
    """Select a session from the picker and start the SDK bridge."""
    global _picker_mode

    if not _picker_mode:
        return {"ok": False, "error": "Not in picker mode"}

    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "No session_id provided"}

    # In picker mode the bridge is built off the hot path — wait for it.
    if bridge is None:
        await _bridge_ready.wait()
    assert bridge is not None and config is not None and state is not None

    # Verify the session exists on disk (scoped to this runtime's account).
    session_dir = find_session_dir(session_id, config.config_dir)
    if session_dir is None:
        return {"ok": False, "error": f"Session {session_id} not found"}

    # Update state with session info.
    state.session_id = session_id
    title = read_session_title(session_id, config.config_dir)
    if title:
        state.session_title = title

    # Optionally switch cwd to match the session's recorded cwd.
    session_cwd = find_session_cwd(session_id)
    if session_cwd:
        resolved = str(Path(session_cwd).resolve(strict=False))
        if Path(resolved).exists():
            os.chdir(resolved)
            log.info("switched cwd to %s for session %s", resolved, session_id[:8])

    # Start the bridge with the selected session.
    bridge._initial_resume_id = session_id
    _picker_mode = False
    await bridge.start()

    log.info("resumed session %s", session_id[:8])
    return {"ok": True, "session_id": session_id, "title": title}


# ---------------------------------------------------------------------------
# Pending-prompt queue API
# ---------------------------------------------------------------------------

def _resolve_queue_rt(body: dict[str, Any] | None = None,
                      ) -> tuple[SessionRuntime | None, State | None, SDKBridge | None]:
    """Resolve runtime/state/bridge for a queue API call.

    The frontend includes ``rid`` in each request body.  Falls back to the
    module-level globals (= default runtime) when ``rid`` is absent or
    doesn't match a live runtime — backwards-compatible with older clients.
    """
    rid = (body or {}).get("rid") if body else None
    rt = _runtime_by_rid(rid) if rid else None
    if rt is not None:
        return rt, rt.state, rt.bridge
    # Fallback: module-level globals (default runtime).
    return _default_runtime, state, bridge


async def _broadcast_queue_update(rt: SessionRuntime | None = None) -> None:
    """Push a runtime's queue state to that session's viewers.

    Defaults to the default runtime (used by the REST queue endpoints, which
    operate on the primary session).
    """
    rt = rt or _default_runtime
    if rt is None or rt.state is None:
        return
    items = [
        {"index": i, "text": text}
        for i, text in enumerate(rt.state.queued_prompts)
    ]
    await rt.broadcast({"type": "queue_update", "queue": items})


@_route("post", "/api/queue/delete")
async def api_queue_delete(body: dict[str, Any]) -> dict[str, Any]:
    """Delete a pending prompt by index."""
    rt, st, br = _resolve_queue_rt(body)
    if st is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")
    if not isinstance(idx, int) or idx < 0 or idx >= len(st.queued_prompts):
        return {"ok": False, "error": f"invalid index: {idx}"}
    removed = st.queued_prompts[idx]
    del st.queued_prompts[idx]
    await _broadcast_queue_update(rt)
    return {"ok": True, "removed": _truncate(removed, 80)}


@_route("post", "/api/queue/send")
async def api_queue_send(body: dict[str, Any]) -> dict[str, Any]:
    """Send a queued prompt by index.

    If idle, pops the prompt and puts it on the event queue so the bridge
    picks it up immediately.  If busy, moves it to the front of the queue
    so it executes next when the current turn ends.
    """
    rt, st, br = _resolve_queue_rt(body)
    if st is None or br is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")
    if not isinstance(idx, int) or idx < 0 or idx >= len(st.queued_prompts):
        return {"ok": False, "error": f"invalid index: {idx}"}
    prompt = st.queued_prompts[idx]
    if st.busy:
        if idx == 0:
            # Already first in line — nothing to do.
            return {"ok": True, "already_next": True}
        # Move to front so it's the next prompt after the current turn.
        del st.queued_prompts[idx]
        st.queued_prompts.appendleft(prompt)
        await _broadcast_queue_update(rt)
        return {"ok": True, "moved_to_front": True}
    del st.queued_prompts[idx]
    br.event_queue.put_nowait(("message", prompt))
    await _broadcast_queue_update(rt)
    return {"ok": True, "sent": _truncate(prompt, 80)}


@_route("post", "/api/queue/edit")
async def api_queue_edit(body: dict[str, Any]) -> dict[str, Any]:
    """Edit a pending prompt by index.

    Also clears the editing lock atomically so the bridge doesn't send
    the old text before the update arrives.
    """
    rt, st, br = _resolve_queue_rt(body)
    if st is None or br is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")
    text = (body.get("text") or "").strip()
    if not isinstance(idx, int) or idx < 0 or idx >= len(st.queued_prompts):
        return {"ok": False, "error": f"invalid index: {idx}"}
    if not text:
        return {"ok": False, "error": "text cannot be empty"}
    # Update text and clear editing lock in one step.
    st.queued_prompts[idx] = text
    st.queue_editing_index = None
    await _broadcast_queue_update(rt)
    # Wake the bridge if idle so it can send the (now updated) prompt.
    if st.queued_prompts and not st.busy:
        br.event_queue.put_nowait(("wakeup", "queue-edit-done"))
    return {"ok": True}


@_route("post", "/api/queue/editing")
async def api_queue_editing(body: dict[str, Any]) -> dict[str, Any]:
    """Notify the server that the user is editing (or finished editing) a queue item.

    While a queue item is being edited, the bridge will hold off on
    sending it so the user can finish their edit first.
    """
    rt, st, br = _resolve_queue_rt(body)
    if st is None or br is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")  # int to start editing, None to stop
    if idx is not None and not isinstance(idx, int):
        return {"ok": False, "error": "index must be int or null"}
    st.queue_editing_index = idx
    if idx is None and st.queued_prompts and not st.busy:
        # Edit finished and bridge is idle — wake it up to send the prompt.
        br.event_queue.put_nowait(("wakeup", "queue-edit-done"))
    return {"ok": True}


@_route("post", "/api/queue/merge")
async def api_queue_merge(body: dict[str, Any]) -> dict[str, Any]:
    """Merge all queued prompts into one, separated by newlines."""
    rt, st, _br = _resolve_queue_rt(body)
    if st is None:
        return {"ok": False, "error": "not ready"}
    if len(st.queued_prompts) < 2:
        return {"ok": False, "error": "need at least 2 prompts to merge"}
    merged = "\n".join(st.queued_prompts)
    st.queued_prompts.clear()
    st.queued_prompts.append(merged)
    await _broadcast_queue_update(rt)
    return {"ok": True, "count": 1}


# ---------------------------------------------------------------------------
# Todos (plan) API
# ---------------------------------------------------------------------------

@_route("post", "/api/todos/clear")
async def api_todos_clear() -> dict[str, Any]:
    """Remove completed items from the plan panel."""
    if state is None:
        return {"ok": False, "error": "not ready"}
    state.current_todos = [
        t for t in state.current_todos
        if t.get("status") != "completed"
    ]
    panels = state_to_panels_dict(state)
    _bcast = _default_runtime.broadcast if _default_runtime is not None else broadcast
    await _bcast({
        "type": "status_update",
        "status": state_to_status_dict(state, config),
        "panels": panels,
    })
    return {"ok": True}


# ---------------------------------------------------------------------------
# Theme / settings API
# ---------------------------------------------------------------------------

@_route("get", "/api/theme")
async def api_theme() -> dict[str, Any]:
    """Return current theme tokens, grouped for the settings UI."""
    t = theme or load_theme()
    return {
        "groups": t.groups(),
        "saved_colors": load_saved_colors(),
        "presets": list(PRESET_THEMES.keys()),
    }


@_route("post", "/api/theme")
async def api_theme_update(body: dict[str, Any]) -> dict[str, Any]:
    """Update one or more theme tokens and persist to theme.conf."""
    global theme
    if theme is None:
        theme = load_theme()

    changes = body.get("tokens", {})
    errors: list[str] = []
    for name, value in changes.items():
        if not theme.set_token(name, value):
            errors.append(f"invalid: {name}={value}")

    save_theme(theme)
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "css": theme.to_css_overrides(),
    }


@_route("post", "/api/theme/preset")
async def api_theme_preset(body: dict[str, Any]) -> dict[str, Any]:
    """Apply a preset theme."""
    global theme
    name = body.get("name", "default")
    overrides = PRESET_THEMES.get(name)
    if overrides is None:
        return {"ok": False, "error": f"unknown preset: {name}"}
    theme = Theme(overrides)
    save_theme(theme)
    return {"ok": True, "css": theme.to_css_overrides(), "groups": theme.groups()}


@_route("post", "/api/theme/reset")
async def api_theme_reset() -> dict[str, Any]:
    """Reset all tokens to defaults."""
    global theme
    theme = Theme()
    save_theme(theme)
    return {"ok": True, "css": "", "groups": theme.groups()}


@_route("post", "/api/theme/saved-colors")
async def api_saved_colors(body: dict[str, Any]) -> dict[str, Any]:
    """Add or remove a saved colour swatch."""
    action = body.get("action", "add")
    color = body.get("color", "")
    if action == "add":
        ok = save_color(color)
    elif action == "remove":
        ok = remove_saved_color(color)
    else:
        return {"ok": False, "error": f"unknown action: {action}"}
    return {"ok": ok, "saved_colors": load_saved_colors()}


@_route("get", "/settings", response_model=None)
async def settings_page():
    """Serve the theme settings page."""
    page = STATIC_DIR / "settings.html"
    if page.exists():
        return FileResponse(page, media_type="text/html")
    return HTMLResponse("<h1>Settings</h1><p>settings.html not found.</p>")


# ---------------------------------------------------------------------------
# Auto-shutdown — exit when all browser tabs have closed
# ---------------------------------------------------------------------------

def _cancel_shutdown_timer() -> None:
    global _shutdown_timer
    if _shutdown_timer is not None and not _shutdown_timer.done():
        _shutdown_timer.cancel()
        _shutdown_timer = None


def _maybe_start_shutdown_timer() -> None:
    global _shutdown_timer
    if _ws_clients:
        return  # still have connected tabs
    if not _has_had_clients:
        return  # never had a tab yet (startup / picker mode)
    if config is None or not config.auto_shutdown:
        return  # only auto-shutdown when enabled (--open, --detach, --auto-shutdown)
    _cancel_shutdown_timer()
    _shutdown_timer = asyncio.create_task(
        _shutdown_after_grace(), name="shutdown-timer"
    )


async def _shutdown_after_grace() -> None:
    """Wait for the grace period, then exit if no tabs reconnected."""
    await asyncio.sleep(_SHUTDOWN_GRACE_SECONDS)
    if not _ws_clients:
        log.info(
            "no browser tabs connected for %ds — shutting down",
            _SHUTDOWN_GRACE_SECONDS,
        )
        # Let every runtime's bridge clean up, then hard-exit.  os._exit
        # avoids blocking on uvicorn's graceful-shutdown timeout.
        for rt in list(runtimes.values()):
            if rt.bridge is not None:
                try:
                    await rt.bridge.stop()
                except Exception:
                    pass
        os._exit(0)


# ---------------------------------------------------------------------------
# Attach / detach — move a tab between the lobby and a session runtime
# ---------------------------------------------------------------------------

async def _attach_ws(ws: WebSocket, rt: SessionRuntime) -> None:
    """Attach *ws* to runtime *rt* and send it that session's initial state.

    Detaches from any previous runtime first (arming that runtime's idle
    timer if it's now viewer-less) and refreshes the lobby viewer counts.
    """
    old = _ws_runtime.get(ws)
    if old is not None and old is not rt:
        old.discard_client(ws)
        _maybe_start_idle_timer(old)
    lobby_clients.discard(ws)
    _cancel_idle_timer(rt)
    rt.add_client(ws)
    _ws_runtime[ws] = rt
    await _send_initial_state(ws)
    await _push_session_list()   # viewer counts changed


async def _enter_lobby(ws: WebSocket, notice: str | None = None) -> None:
    """Detach *ws* from its runtime (if any) and send it the session list.

    *notice*, when given, is delivered as a ``lobby_notice`` message the
    frontend shows as a visible banner *on the lobby overlay* — unlike a chat
    ``system_msg``, which would be hidden behind the overlay.
    """
    old = _ws_runtime.pop(ws, None)
    if old is not None:
        old.discard_client(ws)
        _maybe_start_idle_timer(old)
    lobby_clients.add(ws)
    await _push_session_list(ws)
    if notice:
        await send_to(ws, {"type": "lobby_notice", "message": notice})
    if old is not None:
        await _push_session_list()   # others see the viewer count drop


def _cleanup_ws(ws: WebSocket) -> None:
    """Remove a disconnected socket from all registries (no I/O)."""
    old = _ws_runtime.pop(ws, None)
    lobby_clients.discard(ws)
    _lobby_watchers.discard(ws)
    if old is not None:
        old.discard_client(ws)
        _maybe_start_idle_timer(old)


# ---------------------------------------------------------------------------
# WebSocket endpoint — main real-time channel
# ---------------------------------------------------------------------------

@_ws_route("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Handle one WebSocket client connection."""
    await ws.accept()

    global _has_had_clients
    _cancel_shutdown_timer()
    _has_had_clients = True
    async with _ws_lock:
        _ws_clients.add(ws)

    try:
        # Landing behaviour:
        #   * ``/?rid=<rid>``  → attach straight to that session.  The
        #     sentinel ``default`` resolves to the hub's primary session.
        #   * ``/`` (no rid)   → auto-attach to the most recently active
        #     runtime (so a bare ``localhost:8420`` shows the session
        #     immediately with its scrollback).  Falls back to the default
        #     runtime, then to the lobby.
        # Either way the lobby-aware frontend can switch sessions afterwards.
        requested_rid: str | None = None
        try:
            requested_rid = ws.query_params.get("rid")
        except Exception:
            requested_rid = None
        # ?lobby=1 forces the lobby (used by the ☰ Sessions button).
        force_lobby = False
        try:
            force_lobby = ws.query_params.get("lobby") == "1"
        except Exception:
            pass

        if requested_rid:
            # The sentinel ``default`` resolves to the hub's primary session
            # (used by launches that join a running hub without a specific rid).
            if requested_rid == "default":
                target = _default_runtime
            else:
                target = _runtime_by_rid(requested_rid)
            if target is not None:
                await _attach_ws(ws, target)
            else:
                # The rid is stale (session was torn down, hub was restarted,
                # the computer rebooted and restored old tabs, etc.).  Land in
                # the lobby with a *visible* banner so the user understands why
                # this tab didn't reopen its old session.
                await _enter_lobby(ws, notice=(
                    "This tab's session is no longer running — the server was "
                    "restarted (or the session was closed) since the tab was "
                    "opened. Pick a session below to continue."))
        elif force_lobby:
            await _enter_lobby(ws)
        else:
            # No ?rid= — always land in the lobby.  A bare
            # ``localhost:8420`` (typed manually, or opened in a new tab)
            # is the hub's front door: show the session list so the user
            # picks which session to view.  Auto-attaching to the primary
            # session here was confusing — every new tab silently reopened
            # the default session instead of the hub.  Launches that want
            # to drop straight into a session open ``/?rid=<rid>`` (or the
            # ``default`` sentinel) instead of a bare URL.
            await _enter_lobby(ws)

        # --- Message loop ---
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send_to(ws, {"type": "system_msg", "subtype": "error",
                                   "data": {"message": "invalid JSON"}})
                continue

            await _handle_ws_message(ws, msg)

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        async with _ws_lock:
            _ws_clients.discard(ws)
        _cleanup_ws(ws)
        _maybe_start_shutdown_timer()


async def _send_initial_state(ws: WebSocket) -> None:
    """Send session history, status, panels, and completions on attach.

    Scoped to the runtime *ws* is attached to, so each tab receives the
    status/history of the session it's actually viewing.
    """
    rt = _runtime_for_ws(ws)
    if rt is None or rt.state is None or rt.config is None:
        return
    state = rt.state
    config = rt.config

    # In picker mode the default runtime's chat UI isn't active yet.
    if _picker_mode and rt is _default_runtime:
        await send_to(ws, {
            "type": "system_msg",
            "subtype": "info",
            "data": {"message": "Select a session to resume."},
        })
        return

    # Tell the tab which session it's now viewing (rid + meta).
    await send_to(ws, {"type": "attached", "session": rt.meta()})

    # Status + panels.
    panels = _enrich_panels(state_to_panels_dict(state))
    await send_to(ws, {
        "type": "status_update",
        "status": state_to_status_dict(state, config),
        "panels": panels,
    })

    # Tab-completions.
    await send_to(ws, {
        "type": "completion_list",
        "commands": get_command_completions(),
    })

    # Clear any stale chat content from a previously-viewed session.
    await send_to(ws, {"type": "clear_screen"})

    # Session history (if this session continues an on-disk one).
    # render_session_history does synchronous file I/O that can take seconds
    # on large JSONL files (200+ MB), so run it in a thread to avoid blocking
    # the event loop (which would stall the WebSocket and potentially cause
    # the client to time out before the history arrives).
    if state.session_id:
        log.info("[history] loading for session_id=%s rt=%s",
                 state.session_id, rt.rid)
        # Show an immediate "loading session…" placeholder so the freshly
        # opened tab isn't blank while the (possibly large) history renders.
        # The frontend clears it automatically when the history replay lands;
        # the branches below turn it off explicitly when no history is sent.
        await send_to(ws, {"type": "session_loading", "on": True})
        try:
            loop = asyncio.get_running_loop()
            # Scope the lookup to this session's account so a cross-account
            # hub session's history is found under *its* config dir, not the
            # hub's.  Passed explicitly (not via env) to stay thread-safe.
            _cfg_dir = getattr(config, "config_dir", None)
            session_dir = await loop.run_in_executor(
                None, find_session_dir, state.session_id, _cfg_dir,
            )
            log.info("[history] find_session_dir → %s", session_dir)
            if session_dir:
                jsonl = session_dir / f"{state.session_id}.jsonl"
                _count, history_msgs, _orphans = await loop.run_in_executor(
                    None, render_session_history, jsonl,
                )
                log.info("[history] rendered %d msgs (%d records) for %s",
                         len(history_msgs), _count, state.session_id)
                if history_msgs:
                    await send_to(ws, {
                        "type": "history",
                        "messages": history_msgs,
                    })
                    log.info("[history] sent %d history messages for %s",
                             len(history_msgs), state.session_id)
                else:
                    log.info("[history] no messages to send for %s",
                             state.session_id)
                    await send_to(ws, {"type": "session_loading", "on": False})
            else:
                log.warning("[history] session_dir not found for %s",
                            state.session_id)
                await send_to(ws, {"type": "session_loading", "on": False})
        except Exception as exc:
            log.warning("[history] failed to load history for %s: %s",
                        state.session_id, exc, exc_info=True)
            await send_to(ws, {"type": "session_loading", "on": False})
    else:
        log.info("[history] no session_id on state (rt=%s)", rt.rid)


async def _do_switch(ws: WebSocket, msg: dict[str, Any]) -> None:
    """Copy this tab's current session into another account and attach here.

    The ``/switch`` flow: copy the live session's JSONL into the chosen
    account's projects tree under a *fresh* session id (so it never collides
    with an existing one), set the entered name as its custom title, spin up a
    runtime bound to that account resuming the copy, and attach THIS socket to
    it — so the conversation seamlessly continues in the current window under
    the new account.  Reuses ``copy_session.copy_session_file`` (which rewrites
    the ``sessionId`` fields so the copy resumes cleanly).
    """
    import uuid
    from copy_session import copy_session_file

    rt = _ws_runtime.get(ws) or _runtime_for_ws(ws)
    if rt is None or rt.state is None or rt.config is None \
            or not rt.state.session_id:
        await send_to(ws, {"type": "switch_error",
                           "message": "No active session to switch."})
        return

    target_cfg = (msg.get("config_dir") or "").strip()
    new_name = (msg.get("new_name") or "").strip()
    if not target_cfg:
        await send_to(ws, {"type": "switch_error",
                           "message": "No target account selected."})
        return
    if not new_name:
        await send_to(ws, {"type": "switch_error",
                           "message": "Please enter a name for the new session."})
        return

    src_cfg = getattr(rt.config, "config_dir", None)
    sid = rt.state.session_id
    cwd = rt.config.cwd
    loop = asyncio.get_running_loop()

    # Locate the source JSONL under the current account.
    src_dir = await loop.run_in_executor(None, find_session_dir, sid, src_cfg)
    if src_dir is None:
        await send_to(ws, {"type": "switch_error",
                           "message": "Couldn't find this session's file on disk."})
        return
    src_jsonl = src_dir / f"{sid}.jsonl"
    slug = src_dir.name           # same project-slug under the new account
    dest_proj = Path(target_cfg) / "projects" / slug
    # Fresh session id — regenerate on the (astronomically unlikely) chance the
    # id already exists, so a switch NEVER overwrites an existing session file.
    new_id = str(uuid.uuid4())
    dest_jsonl = dest_proj / f"{new_id}.jsonl"
    while dest_jsonl.exists():
        new_id = str(uuid.uuid4())
        dest_jsonl = dest_proj / f"{new_id}.jsonl"

    # Copy (rewrites sessionId → new_id) off the event loop — the JSONL can be
    # hundreds of MB, which would otherwise freeze every tab and the ticker.
    try:
        await loop.run_in_executor(
            None, copy_session_file, src_jsonl, dest_jsonl, new_id)
    except OSError as exc:
        await send_to(ws, {"type": "switch_error",
                           "message": f"Copy failed: {exc}"})
        return

    # Name the copy (append a custom-title record) under the target account.
    try:
        await asyncio.to_thread(
            write_session_title, new_id, new_name, target_cfg)
    except Exception:
        log.warning("switch: failed to set title on copied session",
                    exc_info=True)

    # Spin up a runtime bound to the target account, resuming the copy.
    try:
        new_rt = await _create_runtime(
            cwd=cwd, resume=new_id, config_dir=target_cfg)
    except Exception as exc:
        log.exception("switch: failed to start runtime for copied session")
        await send_to(ws, {"type": "switch_error",
                           "message": f"Couldn't start the switched session: {exc}"})
        return

    # Reflect the just-written title immediately (the background title read in
    # _create_runtime may not have completed the JSONL scan yet).
    if new_rt.state is not None and not new_rt.state.session_title:
        new_rt.state.session_title = new_name

    await send_to(ws, {"type": "switch_done", "rid": new_rt.rid})
    # Attach THIS socket to the new runtime → history + status flow into the
    # current window, continuing the conversation under the new account.
    await _attach_ws(ws, new_rt)


async def _handle_lobby_message(ws: WebSocket, msg: dict[str, Any]) -> bool:
    """Handle lobby / session-switching messages.  Returns True if handled.

    These don't need a bridge and must not reference the per-session
    ``bridge``/``state``/``config``/``broadcast`` shadows, so they live in
    their own function (Python would otherwise treat those names as locals
    across the whole function once ``_handle_ws_message`` rebinds them).
    """
    msg_type = msg.get("type", "")

    if msg_type == "list":
        await _push_session_list(ws)
        return True

    if msg_type == "lobby_watch":
        # The tab opened/closed its lobby overlay while staying attached to its
        # session.  While open it joins ``_lobby_watchers`` for live pushes.
        if msg.get("on"):
            _lobby_watchers.add(ws)
            await _push_session_list(ws)   # immediate snapshot
        else:
            _lobby_watchers.discard(ws)
        return True

    if msg_type == "detach":
        await _enter_lobby(ws)
        return True

    if msg_type == "switch_list":
        # /switch step 1: send the available accounts (name + email), marking
        # the one this tab's session currently runs under.
        cur = _ws_runtime.get(ws) or _runtime_for_ws(ws)
        cur_cfg = getattr(cur.config, "config_dir", None) if cur and cur.config else None
        cur_sid = getattr(cur.state, "session_id", None) if cur and cur.state else None
        cur_title = getattr(cur.state, "session_title", None) if cur and cur.state else None
        accounts = await asyncio.to_thread(_switch_accounts_payload, cur_cfg)
        await send_to(ws, {
            "type": "switch_accounts",
            "accounts": accounts,
            "current_session_id": cur_sid,
            "current_title": cur_title,
            "has_session": bool(cur_sid),
        })
        return True

    if msg_type == "switch_do":
        await _do_switch(ws, msg)
        return True

    if msg_type == "attach":
        rt = _runtime_by_rid(msg.get("rid"))
        if rt is None:
            await send_to(ws, {"type": "system_msg", "subtype": "error",
                               "data": {"message": "That session is no longer running."}})
            await _push_session_list(ws)
            return True
        await _attach_ws(ws, rt)
        return True

    if msg_type == "new":
        cur = _runtime_for_ws(ws)
        default_cwd = cur.config.cwd if cur and cur.config else (
            config.cwd if config else None)
        cwd = msg.get("cwd") or default_cwd
        try:
            rt = await _create_runtime(cwd=cwd, no_continue=True)
        except Exception as exc:
            log.exception("failed to create new session")
            await send_to(ws, {"type": "system_msg", "subtype": "error",
                               "data": {"message": f"Couldn't start a new session: {exc}"}})
            return True
        await _attach_ws(ws, rt)
        return True

    if msg_type == "open":
        sid = (msg.get("session_id") or "").strip()
        if not sid:
            await send_to(ws, {"type": "system_msg", "subtype": "error",
                               "data": {"message": "No session_id provided."}})
            return True
        # Already live?  Just attach to the existing runtime.
        existing = next(
            (r for r in runtimes.values()
             if getattr(r.state, "session_id", None) == sid),
            None,
        )
        if existing is not None:
            await _attach_ws(ws, existing)
            return True
        cur = _runtime_for_ws(ws)
        default_cwd = cur.config.cwd if cur and cur.config else (
            config.cwd if config else None)
        account = (msg.get("account") or "").strip() or None
        # Verify the session still exists on disk before spinning up a runtime
        # to resume it.  Without this a stale ?open=<sid> (e.g. a browser-
        # restored tab pointing at a deleted/moved session) would create an
        # empty runtime that just sits idle with blank state and no history —
        # which looks broken with no explanation.  Land in the lobby instead.
        loop = asyncio.get_running_loop()
        session_dir = await loop.run_in_executor(
            None, find_session_dir, sid, account)
        if session_dir is None:
            await _enter_lobby(ws, notice=(
                "That session could no longer be found on disk — it may have "
                "been deleted or moved. Pick a session below to continue."))
            return True
        cwd = msg.get("cwd") or find_session_cwd(sid, account) or default_cwd
        try:
            rt = await _create_runtime(cwd=cwd, resume=sid, config_dir=account)
        except Exception as exc:
            log.exception("failed to open session %s", sid)
            await send_to(ws, {"type": "system_msg", "subtype": "error",
                               "data": {"message": f"Couldn't open that session: {exc}"}})
            return True
        await _attach_ws(ws, rt)
        return True

    return False


async def _handle_ws_message(ws: WebSocket, msg: dict[str, Any]) -> None:
    """Dispatch an incoming WebSocket message."""
    # Lobby / session-switching messages route independently of any bridge.
    if await _handle_lobby_message(ws, msg):
        return

    # Everything below operates on the runtime this tab is attached to.
    # Use the *genuine* attachment (``_ws_runtime``) rather than
    # ``_runtime_for_ws``, whose default-runtime fallback would silently route
    # a message from an unattached (lobby) socket into the default session's
    # queue — where the sender, not being in that runtime's client set, never
    # sees the resulting ``queue_update`` and the prompt appears to vanish.
    rt = _ws_runtime.get(ws)
    if rt is None:
        # Socket isn't attached to any session (e.g. it reloaded into the
        # lobby).  A session op (message/command) means the user wants to act
        # on a session, so attach them to the default runtime first — that
        # sends the session's initial state and, crucially, registers the
        # socket as a viewer so it actually receives queue/status updates.
        rt = _default_runtime
        if rt is None:
            await send_to(ws, {"type": "system_msg", "subtype": "warning",
                               "data": {"message": "No session attached."}})
            return
        await _attach_ws(ws, rt)
    # The default runtime's bridge is built off the hot startup path, so a very
    # fast message can arrive before it exists.  Rather than blocking (which
    # hides the prompt from the queue panel), queue user messages immediately
    # so they're visible, then let the bridge drain them once ready.
    if rt.bridge is None:
        state = rt.state
        if state is not None and msg.get("type") == "message":
            text = (msg.get("text") or "").strip()
            if text:
                kind, _payload = classify(text)
                if kind == "message":
                    state.queued_prompts.append(text)
                    await _broadcast_queue_update(rt)
                    # Tell the user their prompt isn't running yet — otherwise a
                    # message queued against a not-yet-connected (or wedged)
                    # bridge just sits there with no "working" state and no
                    # error, looking like it vanished.
                    await send_to(ws, {
                        "type": "system_msg", "subtype": "warning",
                        "data": {"message":
                            "Session is still starting — your prompt is queued "
                            "and will run once it connects."},
                    })
                    return
        # For non-message types, wait for the bridge.
        if rt is _default_runtime:
            await _bridge_ready.wait()

    bridge = rt.bridge
    state = rt.state
    config = rt.config
    broadcast = rt.broadcast
    if bridge is None or state is None or config is None:
        await send_to(ws, {"type": "system_msg", "subtype": "error",
                           "data": {"message": "Session not ready yet — try again."}})
        return
    msg_type = msg.get("type", "")

    # --- User message ---
    if msg_type == "message":
        text = (msg.get("text") or "").strip()
        if not text:
            return

        # Check if it's a slash command.
        kind, payload = classify(text)

        if kind == "empty":
            return

        if kind == "error":
            await send_to(ws, {"type": "system_msg", "subtype": "error",
                               "data": {"message": payload}})
            return

        if kind == "interrupt":
            await bridge.interrupt()
            await broadcast({"type": "system_msg", "subtype": "interrupted",
                             "data": {"message": "Turn interrupted."}})
            return

        if kind in ("quit", "force-quit"):
            await bridge.stop()
            await send_to(ws, {"type": "system_msg", "subtype": "shutdown",
                               "data": {"message": "Shutting down."}})
            return

        if kind in ("switch-cwd", "resume", "resume-pick") and rt is not _default_runtime:
            # These reconfigure the *default* runtime in place; from a
            # secondary session, the way to change folder/session is the
            # lobby (Sessions → open/new).
            await send_to(ws, {"type": "system_msg", "subtype": "info",
                               "data": {"message": "Use Sessions to open a different folder or session."}})
            return

        if kind == "switch-cwd":
            await send_to(ws, {"type": "system_msg", "subtype": "info",
                               "data": {"message": f"Switching to {payload} ..."}})
            ok, err = await _reconfigure(cwd=payload)
            if ok:
                await _send_initial_state(ws)
            else:
                await send_to(ws, {"type": "system_msg", "subtype": "error",
                                   "data": {"message": f"Switch failed: {err}"}})
            return

        if kind == "resume-pick":
            # Open the session picker in the browser.
            await send_to(ws, {"type": "navigate", "url": "/?resume"})
            return

        if kind == "resume":
            await send_to(ws, {"type": "system_msg", "subtype": "info",
                               "data": {"message": f"Resuming {payload} ..."}})
            ok, err = await _reconfigure(resume=payload)
            if ok:
                await _send_initial_state(ws)
            else:
                await send_to(ws, {"type": "system_msg", "subtype": "error",
                                   "data": {"message": f"Resume failed: {err}"}})
            return

        # /mcp — list/manage MCP servers.  Needs the live SDK client
        # (async control requests), so it can't be a synchronous immediate
        # command; handle it here like the other bridge-backed commands.
        if kind == "mcp":
            await _handle_mcp(ws, rt, payload)
            return

        # Try immediate command.
        result = try_immediate_command(kind, payload, state, config)
        if result is not None:
            for m in result.messages:
                await send_to(ws, m)
            if result.state_updates:
                panels = _enrich_panels(state_to_panels_dict(state))
                await broadcast({
                    "type": "status_update",
                    "status": state_to_status_dict(state, config),
                    "panels": panels,
                })
            if result.forward_to_sdk and result.forward_payload:
                bridge.event_queue.put_nowait(("message", result.forward_payload))
            if result.login_launched:
                # Browser OAuth completes asynchronously; watch for it, then
                # refresh the account field / status bar for this runtime.
                asyncio.create_task(_watch_login(rt))
            return

        # /graphify explain|path|diagnose — read-only queries against the
        # existing graph.  Run the graphify CLI directly (fast, deterministic,
        # no LLM turn) and stream its output back into the transcript.
        if kind == "graphify-cli":
            await _run_graphify_cli(ws, payload)
            return

        # /graphify — build the prompt here so we can give feedback and
        # route as a plain message (handled everywhere in the bridge).
        if kind == "graphify":
            path_arg = payload.strip() or "."
            await broadcast({"type": "system_msg", "subtype": "info",
                             "data": {"message": f"/graphify {path_arg} — loading skill..."}})
            from sdk_bridge import _build_graphify_prompt
            prompt = _build_graphify_prompt(payload)
            if state.busy:
                state.queued_prompts.append(prompt)
                await _broadcast_queue_update(rt)
            else:
                bridge.event_queue.put_nowait(("message", prompt))
            return

        # User messages while busy or connecting go to the pending queue
        # (displayed in the queue panel).  The bridge's _between_turns()
        # drains state.queued_prompts between turns; worker_loop() also
        # checks queued_prompts after the initial connect completes.
        if kind == "message" and (state.busy or state.connecting):
            state.queued_prompts.append(payload)
            await _broadcast_queue_update(rt)
            return

        # Everything else (commands, messages while idle) → event queue.
        # For a plain user-typed ``message`` kind, capture the
        # ``client_echoed`` flag from the wire payload so the worker_loop
        # initial-prompt fallback can decide whether the backend needs
        # to broadcast a ``user_message`` itself.  The frontend echoes
        # optimistically only when its ``_isBusy=false`` at send time;
        # when busy/connecting (real or stale-true) it skips the echo
        # and expects the backend to handle it.
        if kind == "message":
            state.initial_prompt_client_echoed = bool(
                msg.get("client_echoed", False)
            )
        bridge.event_queue.put_nowait((kind, payload))

        # The frontend shows user messages optimistically (app.js send()),
        # so we don't echo back here — that would cause duplicates.

    # --- Explicit command (alternative to prefixed message) ---
    elif msg_type == "command":
        text = (msg.get("text") or "").strip()
        if text and not text.startswith("/"):
            text = "/" + text
        if text:
            # Re-dispatch as a message.
            await _handle_ws_message(ws, {"type": "message", "text": text})

    # --- Interrupt ---
    elif msg_type == "interrupt":
        await bridge.interrupt()
        await broadcast({"type": "system_msg", "subtype": "interrupted",
                         "data": {"message": "Turn interrupted."}})

    # --- Permission response ---
    elif msg_type == "permission_response":
        allow = bool(msg.get("allow", False))
        bridge.resolve_permission(allow)

    else:
        await send_to(ws, {"type": "system_msg", "subtype": "warning",
                           "data": {"message": f"unknown message type: {msg_type}"}})


# ---------------------------------------------------------------------------
# graphify read-only CLI subcommands
# ---------------------------------------------------------------------------

def _build_graphify_cli_argv(payload: str) -> tuple[list[str] | None, str | None]:
    """Parse ``/graphify <sub> ...`` into an argv for ``python -m graphify``.

    Only the read-only subcommands (explain / path / diagnose) are handled
    here.  Returns ``(argv, None)`` on success or ``(None, usage_error)``.
    """
    import shlex

    parts = payload.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    base = [sys.executable, "-m", "graphify"]

    if sub == "explain":
        if not rest:
            return None, "usage: /graphify explain <node>"
        # Single node label — honour surrounding quotes, otherwise pass the
        # whole remainder as one argument (labels may contain spaces).
        toks = shlex.split(rest)
        label = toks[0] if len(toks) == 1 else rest
        return base + ["explain", label], None

    if sub == "path":
        toks = shlex.split(rest)
        if len(toks) != 2:
            return None, "usage: /graphify path <A> <B>  (quote names with spaces)"
        return base + ["path", toks[0], toks[1]], None

    if sub == "diagnose":
        toks = shlex.split(rest) if rest else []
        if not toks:
            toks = ["multigraph"]
        return base + ["diagnose", *toks], None

    return None, f"unknown graphify subcommand: {sub!r}"


async def _handle_mcp(ws: WebSocket, rt: "SessionRuntime", payload: str) -> None:
    """Handle the ``/mcp`` command: list or manage MCP servers.

    ``/mcp``                     — list servers with status + tool counts
    ``/mcp tools [server]``      — list tools (all servers or one)
    ``/mcp reconnect <server>``  — reconnect a failed / disconnected server
    ``/mcp enable  <server>``    — enable (reconnect) a server
    ``/mcp disable <server>``    — disable (disconnect) a server
    """
    bridge = rt.bridge
    if bridge is None or getattr(bridge, "client", None) is None:
        await send_to(ws, {"type": "system_msg", "subtype": "error",
                           "data": {"message": "MCP: SDK not connected yet."}})
        return

    parts = payload.split()
    sub = parts[0].lower() if parts else ""

    if sub in ("reconnect", "enable", "disable"):
        name = " ".join(parts[1:]).strip()
        if not name:
            await send_to(ws, {"type": "system_msg", "subtype": "error",
                               "data": {"message": f"usage: /mcp {sub} <server>"}})
            return
        try:
            if sub == "reconnect":
                await bridge.reconnect_mcp_server(name)
                verb = "reconnected"
            else:
                await bridge.toggle_mcp_server(name, enabled=(sub == "enable"))
                verb = "enabled" if sub == "enable" else "disabled"
            await send_to(ws, {"type": "system_msg", "subtype": "info",
                               "data": {"message": f"MCP server '{name}' {verb}."}})
        except Exception as exc:  # noqa: BLE001 — surface any control failure
            await send_to(ws, {"type": "system_msg", "subtype": "error",
                               "data": {"message": f"MCP {sub} failed for '{name}': {exc}"}})
        return

    # Default: list servers (optionally the full tool list via `/mcp tools`).
    tools_mode = sub == "tools"
    filter_name = " ".join(parts[1:]).strip() if tools_mode else ""
    try:
        servers = await bridge.get_mcp_status()
    except Exception as exc:  # noqa: BLE001 — surface connection/API failures
        await send_to(ws, {"type": "system_msg", "subtype": "error",
                           "data": {"message": f"MCP status unavailable: {exc}"}})
        return

    content = format_mcp_status(
        servers, tools_mode=tools_mode, filter_name=filter_name,
    )
    await send_to(ws, {"type": "modal", "title": "MCP Servers", "content": content})


async def _run_graphify_cli(ws: WebSocket, payload: str) -> None:
    """Run a read-only graphify subcommand and stream its output to ``ws``."""
    argv, err = _build_graphify_cli_argv(payload)
    if err:
        await send_to(ws, {"type": "system_msg", "subtype": "error",
                           "data": {"message": err}})
        return

    sub_label = payload.strip().split(None, 1)[0]
    await send_to(ws, {"type": "system_msg", "subtype": "info",
                       "data": {"message": f"/graphify {sub_label} — running..."}})

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=config.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        text = out.decode("utf-8", "replace").rstrip()
        rc = proc.returncode
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        await send_to(ws, {"type": "system_msg", "subtype": "error",
                           "data": {"message": "graphify timed out after 60s"}})
        return
    except Exception as e:  # noqa: BLE001 — surface any launch/IO failure
        await send_to(ws, {"type": "system_msg", "subtype": "error",
                           "data": {"message": f"graphify failed: {e}"}})
        return

    if not text:
        text = f"(no output, exit code {rc})"
    elif rc:
        text = f"{text}\n\n(exit code {rc})"
    await send_to(ws, {"type": "command_data", "data": text})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _try_shutdown_old_server(port: int) -> None:
    """Shut down an existing server on *port*.

    Tries the graceful ``/api/shutdown`` endpoint first.  If that fails
    (e.g. old server is running code without the endpoint), falls back
    to killing the process that owns the port via ``taskkill`` on
    Windows or ``fuser`` on Linux/macOS.
    """
    import urllib.request
    import urllib.error
    import time as _time

    # --- Attempt 1: HTTP graceful shutdown ---
    url = f"http://localhost:{port}/api/shutdown"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        urllib.request.urlopen(req, timeout=3)
        print(f"Shut down old server on port {port}.")
        _time.sleep(1.5)
        return
    except Exception:
        pass

    # --- Attempt 2: kill the process holding the port ---
    import subprocess
    if sys.platform == "win32":
        try:
            # netstat -ano finds PIDs listening on the port.
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                text=True, timeout=5,
            )
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and f":{port}" in parts[1] and parts[3] == "LISTENING":
                    pid = parts[4]
                    subprocess.call(
                        ["taskkill", "/F", "/PID", pid],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    print(f"Killed old process {pid} on port {port}.")
                    _time.sleep(1)
                    return
        except Exception:
            pass
    else:
        try:
            subprocess.call(
                ["fuser", "-k", f"{port}/tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            print(f"Killed old process on port {port}.")
            _time.sleep(1)
        except Exception:
            pass


def _probe_hub(port: int) -> dict | None:
    """Return a running orchestrator2 hub's ``/api/whoami`` on *port*, or None.

    Any orchestrator2 hub on the port is eligible — different accounts share
    the same hub, with each session spawning its own bridge subprocess using
    the launcher's ``CLAUDE_CONFIG_DIR``.

    Retries the HTTP request a few times on *timeout* (the hub is up but its
    event loop was momentarily busy).  Without this, a transient stall would
    make the probe fail, and the launcher would then spin up a whole *second*
    server on a different port — a slow full SDK startup that also litters the
    browser with duplicate/stray tabs.

    The retries are gated behind a fast TCP-listening check so a *missing* hub
    (first launch) stays cheap: the OS accepts a loopback connection to a
    listening socket in well under a millisecond even when the app's event loop
    is blocked, so if nothing is listening we bail immediately instead of
    burning three HTTP timeouts (a closed port can *time out* rather than refuse
    on some Windows firewall configurations).
    """
    import urllib.request

    # Is anything listening at all?  (Loopback connect is instant for a live
    # server regardless of how busy its event loop is.)
    probe_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    probe_sock.settimeout(1.0)
    try:
        probe_sock.connect(("127.0.0.1", port))
    except OSError:
        return None
    finally:
        probe_sock.close()

    last_exc: Exception | None = None
    for _attempt in range(3):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/whoami", timeout=2.0
            ) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if not isinstance(data, dict) or data.get("app") != "orchestrator2":
                return None
            return data
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        log.info("hub probe on port %d gave up after retries: %s", port, last_exc)
    return None


def _launch_into_hub(
    port: int, *, cwd: str, resume: str | None, no_continue: bool,
    model: str | None = None, effort: str | None = None,
    config_dir: str | None = None,
) -> str | None:
    """Ask a running hub to open a session; return its ``rid`` (or None)."""
    import urllib.request

    payload = json.dumps({
        "cwd": cwd,
        "resume": resume,
        "no_continue": no_continue,
        "model": model,
        "effort": effort,
        "config_dir": config_dir,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/launch",
            data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        log.warning("hub session launch failed: %s", exc)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        log.warning("hub session launch rejected: %s", data)
        return None
    return data.get("rid")


def _bind_port(host: str, port: int, wait_secs: float = 0.0) -> tuple[_socket.socket, int]:
    """Try to bind *port*; on failure, let the OS pick a free one.

    On Windows, ``SO_REUSEADDR`` silently allows multiple processes to
    bind the same port — the OS then routes connections unpredictably.
    We use ``SO_EXCLUSIVEADDRUSE`` instead so the bind properly fails
    when the port is already occupied.  On other platforms, the standard
    ``SO_REUSEADDR`` is used to allow reusing TIME_WAIT addresses.

    When *wait_secs* > 0 (a restart child taking over from an instance that
    is still exiting), retry binding the *requested* port for that many
    seconds before giving up and letting the OS pick a free one.  This keeps
    the restarted server on the same port so the browser can reload in place.
    """
    import time as _t
    deadline = _t.monotonic() + max(0.0, wait_secs)
    while True:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        if sys.platform == "win32":
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        else:
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            if _t.monotonic() < deadline:
                sock.close()
                _t.sleep(0.2)
                continue
            sock.bind((host, 0))
        actual = sock.getsockname()[1]
        sock.set_inheritable(True)
        return sock, actual


# Lightweight "starting…" page served by the stdlib pre-server (below) while
# fastapi/uvicorn load.  Its JS polls /api/ready and redirects to the real app
# the moment uvicorn takes over the socket.
_SPLASH_HTML = ("""<!doctype html>
<html><head><meta charset="utf-8"><title>orchestrator2 - starting\u2026</title>
<style>
  html,body{height:100%;margin:0}
  body{background:#0a0a14;color:#cdd6f4;font-family:system-ui,'Segoe UI',sans-serif;
       display:flex;align-items:center;justify-content:center;flex-direction:column;gap:22px}
  .spin{width:46px;height:46px;border-radius:50%;border:4px solid #23233a;
        border-top-color:#7aa2f7;animation:r .8s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  .t{font-size:15px;letter-spacing:.3px;color:#9aa2c0}
  .b{font-size:12px;color:#565b7a}
</style></head>
<body>
  <div class="spin"></div>
  <div class="t">starting orchestrator2\u2026</div>
  <div class="b" id="b">warming up the server</div>
<script>
  let n=0;
  async function poll(){
    n++;
    if(n===12){document.getElementById('b').textContent='still starting - hang tight';}
    try{
      const r=await fetch('/api/ready',{cache:'no-store'});
      if(r.ok){location.replace('/'+location.search);return;}
    }catch(e){}
    setTimeout(poll,400);
  }
  poll();
</script>
</body></html>""").encode("utf-8")


def _serve_splash_until(sock: _socket.socket, stop_event: "_threading.Event",
                        ready_event: "_threading.Event") -> None:
    """Serve the startup splash on *sock* until *stop_event* is set.

    Runs in a thread on the already-bound socket so the browser tab can open in
    ~0.3s while the heavy fastapi/uvicorn stack imports on the main thread.
    Answers every path with the splash HTML except ``/api/ready`` (503) \u2014 the
    real app answers 200 there once uvicorn is up, which is the splash's cue to
    redirect.  The socket is left *listening* the whole time and handed to
    uvicorn afterwards, so connections that arrive during the brief handoff sit
    in the kernel backlog and get served by uvicorn (never refused).
    """
    sock.listen(128)
    sock.settimeout(0.25)
    ready_event.set()  # socket is now listening; safe to open the browser
    while not stop_event.is_set():
        try:
            conn, _ = sock.accept()
        except OSError:
            continue  # accept timeout (settimeout) or transient error
        try:
            conn.settimeout(1.0)
            data = b""
            try:
                while b"\r\n" not in data and len(data) < 4096:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
            first = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = first.split(" ")
            path = parts[1] if len(parts) >= 2 else "/"
            if path.startswith("/api/"):
                # The real HTTP API isn't up yet.  Answer every /api/* path
                # (not just /api/ready) with a clean 503 JSON so a *concurrent*
                # launcher probing /api/whoami or POSTing /api/session/launch
                # during this splash window sees "not ready" and retries,
                # instead of parsing the splash HTML as JSON, concluding "no
                # hub here", and spinning up a second independent server.
                body = b'{"ready": false}'
                resp = (b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() +
                        b"\r\nConnection: close\r\n\r\n" + body)
            else:
                body = _SPLASH_HTML
                resp = (b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/html; charset=utf-8\r\n"
                        b"Content-Length: " + str(len(body)).encode() +
                        b"\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n" +
                        body)
            try:
                conn.sendall(resp)
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
    # Restore blocking mode and leave the socket listening for uvicorn.
    try:
        sock.settimeout(None)
    except OSError:
        pass


def _setup_file_logging(log_path: str, fmt: str) -> None:
    """Attach a file handler to the root logger and wire up crash logging.

    - Appends to *log_path* (created if missing).
    - Installs ``sys.excepthook`` so uncaught exceptions are logged to
      the file before the process dies.
    - Registers an ``atexit`` handler that logs a clean-shutdown line.

    The caller has already stamped ``%(process)d`` into the format
    string in ``main`` so records from concurrent instances writing
    to the same file can be disambiguated by PID.
    """
    path = Path(log_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(str(path), encoding="utf-8")
    handler.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(handler)

    _flog = logging.getLogger("orchestrator2")
    _flog.info("--- log file opened: %s ---", path)

    # Log uncaught exceptions so they survive a closed terminal.
    _original_excepthook = sys.excepthook

    def _excepthook(exc_type: type, exc_value: BaseException, exc_tb: Any) -> None:
        _flog.critical(
            "Uncaught exception — process terminating",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        _original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    atexit.register(lambda: _flog.info("--- server shut down (pid %d) ---", os.getpid()))


def _stdin_is_tty() -> bool:
    """True when stdin is an interactive terminal (needed for the TUI picker).

    Guarded because ``sys.stdin`` can be ``None`` (pythonw / detached child)
    or raise on ``isatty()`` for exotic stream replacements.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _console_is_visible() -> bool:
    """True when this process owns a *visible* console window (Windows).

    A process launched by tray_minimizer has a real console (so stdin is a
    tty) but its window is hidden with SW_HIDE — drawing a full-screen TUI
    there would be invisible.  This distinguishes "usable terminal" from
    "hidden console".
    """
    if sys.platform != "win32":
        return _stdin_is_tty()
    try:
        import ctypes
        k = ctypes.windll.kernel32
        u = ctypes.windll.user32
        k.GetConsoleWindow.restype = ctypes.c_void_p
        hwnd = k.GetConsoleWindow()
        if not hwnd:
            return False
        return bool(u.IsWindowVisible(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


def _run_launch_picker(mode: str, launch_cwd: str | None = None) -> dict | None:
    """Run the `--resume` / `--copy` TUI picker and return its result.

    Returns ``{"session_id": str | None, "cwd": str | None}`` (or ``None`` when
    no picker could run).  ``cwd`` is the picked session's working directory —
    the server adopts it because resume is cwd-scoped (the CLI only finds a
    session under its own project directory).

    The picker is ALWAYS run in a **child process** (`copy_session.py
    --pick`), never in-process — so the server process never hosts a
    full-screen Textual app whose terminal teardown could wedge the
    subsequent uvicorn startup.  On Windows:

    * if our own console is visible, the child shares it (the TUI draws in
      the current terminal);
    * if our console is hidden (tray_minimizer launch) or absent (IDE run /
      double-click), the child gets its own **fresh, visible** console via
      CREATE_NEW_CONSOLE — so the picker is always something the user can
      actually see, while the server itself stays hidden in the tray.

    The chosen id is passed back through a temp JSON file.  The child
    inherits our environment (notably CLAUDE_CONFIG_DIR) so it lists the
    same account.
    """
    import subprocess
    import tempfile
    import json as _json

    if sys.platform != "win32":
        # POSIX: only a real terminal can host the TUI; run it in-process.
        if _stdin_is_tty():
            try:
                from copy_session import pick_session_for_launch
                return pick_session_for_launch(mode, launch_cwd)
            except Exception:
                log.exception("launch picker failed")
            return None
        print(
            f"--{mode} needs an interactive terminal; run orchestrator2 from a "
            f"terminal to use the session picker.",
            file=sys.stderr,
        )
        return None

    def _read_result(path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        sid = data.get("session_id")
        cwd = data.get("cwd")
        return {
            "session_id": sid if isinstance(sid, str) and sid else None,
            "cwd": cwd if isinstance(cwd, str) and cwd else None,
        }

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8",
    )
    out_path = tmp.name
    tmp.close()

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copy_session.py")
    argv = [sys.executable, script, "--pick", mode, "--out", out_path]
    if launch_cwd:
        argv.extend(["--cwd", launch_cwd])

    # Share the current console when it's visible; otherwise pop a fresh,
    # visible one (the hidden/tray or no-console case).
    creationflags = 0 if _console_is_visible() else subprocess.CREATE_NEW_CONSOLE
    try:
        proc = subprocess.Popen(
            argv,
            creationflags=creationflags,
            cwd=os.path.dirname(script) or None,
        )
        proc.wait()
    except Exception:
        log.exception("failed to launch picker process")
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return None

    try:
        return _read_result(out_path)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def main() -> None:
    """Launch the server."""
    global config

    # Parse args early so we can get the port and store for startup().
    config = parse_args()

    # Set CLAUDE_CONFIG_DIR before any SDK or session code runs.  When
    # --config-dir is passed it wins; otherwise the SDK/CLI use whatever
    # CLAUDE_CONFIG_DIR is inherited from the environment (or ~/.claude when
    # unset).
    if config.config_dir:
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(config.config_dir).resolve())

    # Console: keep the short, PID-less format.  File: prefix every
    # record with the PID so concurrent orchestrator2 instances
    # writing to the same log file can be told apart.  Without the
    # PID, interleaved records from two processes are effectively
    # indistinguishable and bridge / dispatcher / turn diagnosis
    # becomes nearly impossible.
    console_fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    file_fmt = "%(asctime)s [pid %(process)d] [%(name)s] %(levelname)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=console_fmt)

    if config.log_file:
        _setup_file_logging(config.log_file, file_fmt)

    # --resume (no id) / --copy: show the full-screen terminal picker so the
    # user can choose a project + session to resume (or copy in from another
    # account) before the server starts.  The account is the one selected by
    # CLAUDE_CONFIG_DIR (set just above).  _run_launch_picker draws the TUI in
    # this terminal when we have one, or pops a fresh console window when we
    # don't (IDE run / hidden launcher).  In --detach the *parent* runs here
    # and passes the resolved id to the child via argv below.  The headless
    # --detach child skips this (its resume id is already concrete).
    if config.copy or config.resume == _PICKER_SENTINEL:
        picker_mode = "copy" if config.copy else "resume"
        result = _run_launch_picker(picker_mode, config.cwd)
        chosen = result.get("session_id") if isinstance(result, dict) else None
        chosen_cwd = result.get("cwd") if isinstance(result, dict) else None
        if chosen:
            config = dataclasses.replace(config, resume=chosen, copy=False)
            # Resume is cwd-scoped: adopt the picked session's own working
            # directory so the CLI finds it (a session picked from another
            # project — or a just-copied one — lives under that project's
            # slug, not the launch cwd's).  Only if the dir still exists.
            if chosen_cwd and os.path.isdir(chosen_cwd):
                config = dataclasses.replace(
                    config, cwd=str(Path(chosen_cwd).resolve())
                )
        elif config.copy:
            # Copy cancelled, declined ("open it now?" → No), or copied into a
            # different account (unresumable here).  In every case nothing was
            # chosen to open, so start a FRESH empty session rather than
            # silently auto-continuing the launch cwd's most-recent session
            # (which is unrelated to the copy the user was doing).
            config = dataclasses.replace(
                config, copy=False, resume=None, no_continue=True
            )
        # A cancelled resume picker leaves the sentinel in place, so the
        # in-browser picker still appears.

    # --- Central-hub reuse -------------------------------------------------
    # If an orchestrator2 hub is already serving on our port, join it — open
    # the launched session there and point a browser at it — instead of
    # starting a second, independent server.  Different accounts are fine:
    # each session spawns its own bridge subprocess with the launcher's
    # CLAUDE_CONFIG_DIR.  Skipped by --standalone and the in-browser resume
    # picker.  Runs before --detach so a hub short-circuits the spawn.
    #
    # Also skipped by --wait-port: that flag marks a *restart child* whose whole
    # job is to take over the same port from the outgoing server, which is still
    # briefly alive (it waits for us to come up before releasing the port).  If
    # we probed and joined it as a hub session here, we'd exit(0) instead of
    # binding the port — the parent's crash-check then sees the child gone and
    # aborts the restart, leaving the stale process in place.
    if (not config.standalone
            and not config.wait_port
            and config.resume != _PICKER_SENTINEL
            and _probe_hub(config.port) is not None):
        rid = _launch_into_hub(
            config.port,
            cwd=config.cwd,
            resume=config.resume,
            no_continue=config.no_continue,
            model=config.model,
            effort=config.effort,
            config_dir=config.config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"),
        )
        if rid:
            url = f"http://localhost:{config.port}/?rid={rid}"
            print(f"Joined running orchestrator2 hub on port {config.port} "
                  f"(session {rid}).")
            if config.open_browser:
                webbrowser.open(f"{url}&t={int(time.time())}")
            sys.exit(0)
        # Hub was up but couldn't open the session — fall through and start
        # our own server on a free port.
        print("Hub was running but couldn't open the session — starting a "
              "separate server.")

    # --detach: validate everything here (errors visible in terminal),
    # then respawn headless and exit.
    if config.detach:
        # Ensure we're signed in before spawning the headless child, blocking
        # in this visible terminal so the child starts authenticated and
        # doesn't pop its own login window.
        _ensure_logged_in(block=True)
        sock, actual_port = _bind_port("0.0.0.0", config.port)
        if actual_port != config.port:
            print(f"Port {config.port} in use — using {actual_port} instead.")
        sock.close()  # release for the child to rebind

        # Rebuild argv: drop --detach, --open, override --port with actual.
        # --open is stripped because the PARENT opens the browser (the child
        # runs with CREATE_NO_WINDOW and webbrowser.open() doesn't reliably
        # work from a hidden-console process on Windows).
        child_argv = [sys.executable, sys.argv[0]]
        i = 1
        while i < len(sys.argv):
            a = sys.argv[i]
            if a in ("--detach", "--open", "--auto-shutdown"):
                i += 1
                continue
            if a == "--port":
                i += 2  # skip --port and its value
                continue
            if a.startswith("--port="):
                i += 1
                continue
            # --copy / --resume were consumed by the launch picker above; the
            # resolved session id is re-added from config.resume below so the
            # headless child doesn't try to open a picker it has no terminal
            # for.
            if a == "--copy":
                i += 1
                continue
            if a == "--resume":
                i += 1
                if i < len(sys.argv) and not sys.argv[i].startswith("-"):
                    i += 1  # also skip its optional value
                continue
            if a.startswith("--resume="):
                i += 1
                continue
            # --cwd may have been overridden by the picker (adopting the picked
            # session's project dir); strip any inherited --cwd and re-add the
            # resolved config.cwd below.
            if a == "--cwd":
                i += 2
                continue
            if a.startswith("--cwd="):
                i += 1
                continue
            child_argv.append(a)
            i += 1
        child_argv.extend(["--port", str(actual_port), "--auto-shutdown",
                           "--skip-auto-login", "--cwd", config.cwd])
        # Re-add the (possibly picker-resolved) resume target for the child.
        if config.resume == _PICKER_SENTINEL:
            child_argv.append("--resume")  # bare → child shows browser picker
        elif config.resume:
            child_argv.extend(["--resume", config.resume])

        import subprocess
        import tempfile

        # Send child stderr to a temp file so we can check for
        # immediate crashes without broken-pipe issues.
        err_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False,
        )
        err_path = err_file.name

        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # CREATE_NO_WINDOW gives the child a hidden console that
            # the Claude CLI subprocess inherits (no blank terminal).
            # CREATE_NEW_PROCESS_GROUP prevents Ctrl+C from the parent
            # console propagating to the child.
            # Do NOT combine with DETACHED_PROCESS — they conflict and
            # cause the CLI to allocate a visible console then crash.
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            child_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err_file,
            close_fds=True,
            **kwargs,
        )
        err_file.close()

        # Poll until the child is actually serving HTTP, then open the browser
        # immediately — rather than sleeping a fixed 2s and hoping.  This opens
        # the tab the instant the port accepts a connection (usually well under
        # a second once imports finish) and still catches an early crash.  The
        # SDK connect happens *after* the child starts serving, so the page
        # loads right away and shows a "connecting…" status while it finishes.
        import time as _time
        url = f"http://localhost:{actual_port}"
        deadline = _time.monotonic() + 20.0
        serving = False
        while _time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                err = Path(err_path).read_text(errors="replace").strip()
                print(f"Failed to start (exit {rc}):", file=sys.stderr)
                if err:
                    for line in err.splitlines()[-20:]:
                        print(f"  {line}", file=sys.stderr)
                try:
                    os.unlink(err_path)
                except OSError:
                    pass
                sys.exit(rc or 1)
            probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            probe.settimeout(0.25)
            try:
                probe.connect(("127.0.0.1", actual_port))
                serving = True
            except OSError:
                serving = False
            finally:
                probe.close()
            if serving:
                break
            _time.sleep(0.1)

        # Serving (or timed out but process still alive) → open the browser from
        # the parent (which has a visible console).  The child's CREATE_NO_WINDOW
        # environment makes webbrowser.open() unreliable on Windows.
        print(f"orchestrator2 launched on {url}")
        if config.open_browser:
            # Attach straight to the just-launched primary session (bare URLs
            # now land in the lobby).  ``default`` resolves to the hub primary.
            webbrowser.open(f"{url}/?rid=default&t={int(time.time())}")

        sys.exit(0)

    # Auto-login (non-detach path).  The --detach child skips this because the
    # parent already ensured we're signed in.  Non-blocking: the login window
    # opens and startup proceeds; the worker's connect-retry succeeds once the
    # credentials appear.
    if not config.skip_auto_login:
        _ensure_logged_in(block=False)

    # Bind the socket ourselves so we know the actual port before
    # uvicorn starts (needed for --open and the startup message).
    # A restart child (--wait-port) retries the same port while the old
    # instance finishes exiting, so the browser can reload in place.
    sock, actual_port = _bind_port(
        "0.0.0.0", config.port, wait_secs=20.0 if config.wait_port else 0.0
    )

    if actual_port != config.port:
        # The requested port is occupied — definitive proof another server
        # already owns it.  For a normal launch that means our hub is (or is
        # coming) up there and we should JOIN it, not start a second, fully
        # independent server on a random port (which splits sessions across
        # two hubs and burns a whole extra SDK startup).  The earlier reuse
        # probe (before binding) can miss a hub that was still in its startup
        # splash or had a momentarily-blocked event loop, so retry the join
        # here now that we KNOW something holds the port — riding out the
        # splash/busy window before falling back.  --standalone and the
        # restart child (--wait-port) legitimately want their own port.
        if not config.standalone and not config.wait_port \
                and config.resume != _PICKER_SENTINEL:
            sock.close()  # release the useless fallback port
            join_cfg_dir = (config.config_dir
                            or os.environ.get("CLAUDE_CONFIG_DIR")
                            or str(Path.home() / ".claude"))
            import time as _time
            deadline = _time.monotonic() + 30.0
            rid = None
            while _time.monotonic() < deadline:
                if _probe_hub(config.port) is not None:
                    rid = _launch_into_hub(
                        config.port, cwd=config.cwd, resume=config.resume,
                        no_continue=config.no_continue, model=config.model,
                        effort=config.effort, config_dir=join_cfg_dir,
                    )
                    if rid:
                        break
                _time.sleep(0.5)
            if rid:
                url = f"http://localhost:{config.port}/?rid={rid}"
                print(f"Joined running orchestrator2 hub on port "
                      f"{config.port} (session {rid}).")
                if config.open_browser:
                    webbrowser.open(f"{url}&t={int(time.time())}")
                sys.exit(0)
            # Couldn't join after retrying — fall back to a standalone server
            # on a fresh port so the user at least gets a working instance.
            print(f"Port {config.port} in use but couldn't join the hub — "
                  f"starting a separate server.")
            sock, actual_port = _bind_port("0.0.0.0", 0)

        print(f"Port {config.port} in use — using {actual_port} instead.")
        config = dataclasses.replace(config, port=actual_port)

    url = f"http://localhost:{actual_port}"
    msg = f"orchestrator2 starting on {url}"
    if config.config_dir:
        msg += f"  (config-dir: {config.config_dir})"
    print(msg)

    # Instant-startup splash.  fastapi/uvicorn take ~1.5s+ to import and build
    # (occasionally much longer under Windows process-launch overhead); binding
    # a port is instant, but the browser can't be pointed at it until *something*
    # is listening.  So we spin up a stdlib pre-server on the already-bound
    # socket (listens in ~0.3s), open the browser at it immediately, and load
    # the heavy stack on this thread meanwhile.  The pre-server shows a
    # "starting…" splash whose JS polls /api/ready; once we hand the (still
    # listening) socket to uvicorn, that poll returns 200 and the page redirects
    # to the real UI.  This makes the tab appear near-instantly instead of after
    # the framework import, and eliminates the "can't connect" race entirely.
    _splash_stop = _threading.Event()
    _splash_ready = _threading.Event()
    _splash_thread = _threading.Thread(
        target=_serve_splash_until, args=(sock, _splash_stop, _splash_ready),
        name="splash-preserver", daemon=True,
    )
    _splash_thread.start()
    _splash_ready.wait(timeout=5)  # ensure the socket is listening first

    if config.open_browser:
        # Cache-buster forces a fresh navigation instead of re-focusing a stale
        # tab with the same URL.  Attach straight to the primary session
        # (``default`` sentinel) since bare URLs now land in the lobby.
        webbrowser.open(f"{url}/?rid=default&t={int(time.time())}")

    # Build the FastAPI app now (imports fastapi lazily) — only the serving
    # process pays this cost; the --detach parent returned long ago.  This is
    # the slow part the splash is covering for.
    import uvicorn
    fastapi_app = build_app()

    # Disable WebSocket keepalive pings.  The default (20s ping, 20s
    # timeout) is far too aggressive for a localhost server under heavy
    # CPU/IO load — the browser can't respond in time, the connection
    # drops, and auto-shutdown fires.  Keepalive pings exist to detect
    # dead connections through NATs/proxies; localhost doesn't need them.
    uvi_config = uvicorn.Config(
        fastapi_app, host="0.0.0.0", port=actual_port, log_level="info",
        ws_ping_interval=None, ws_ping_timeout=None,
    )
    server = uvicorn.Server(uvi_config)

    # Stop the splash pre-server and hand the still-listening socket to uvicorn.
    # Any connection that arrived during this handoff is queued in the kernel
    # backlog and gets served by uvicorn, so nothing is refused.
    _splash_stop.set()
    _splash_thread.join(timeout=5)

    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
