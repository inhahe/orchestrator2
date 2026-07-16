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
import json
import logging
import logging.handlers
import os
import socket as _socket
import sys
import time
from pathlib import Path
from typing import Any

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
    DEFAULT_PORT,
    _PICKER_SENTINEL,
    fetch_available_models,
    parse_args,
)
from state import (
    State,
    init_state_from_config,
    state_to_panels_dict,
    state_to_status_dict,
)
from session import (
    find_most_recent_session_for_cwd,
    find_session_cwd,
    find_session_dir,
    list_projects,
    list_sessions_for_project,
    read_session_title,
    render_session_history,
)
from commands import (
    classify,
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
_ws_clients: set[WebSocket] = set()
_ws_lock = asyncio.Lock()

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
    """Send *msg* as JSON to every connected WebSocket client."""
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
    """Push status_update + panel_update to clients every ~2s."""
    while True:
        await asyncio.sleep(2.0)
        if state is None or config is None:
            continue
        if not _ws_clients:
            continue
        try:
            # Expire panel grace items.
            now = time.monotonic()
            expired = [k for k, v in state.completed_panel_tools.items()
                       if v.get("grace_end", 0) < now]
            for k in expired:
                state.completed_panel_tools.pop(k, None)
            expired_bg = [k for k, v in state.completed_panel_bg.items()
                          if v.get("grace_end", 0) < now]
            for k in expired_bg:
                state.completed_panel_bg.pop(k, None)

            panels = _enrich_panels(state_to_panels_dict(state))
            await broadcast({
                "type": "status_update",
                "status": state_to_status_dict(state, config),
                "panels": panels,
            })

            # Catch-all bell flush: bells rung from sync contexts (e.g.
            # rate-limit hits) set state.pending_bell but have no async
            # path to broadcast it.  Flush here so they reach the frontend
            # within one tick instead of waiting for the next turn to end.
            if state.pending_bell:
                event = state.pending_bell
                state.pending_bell = None
                await broadcast({"type": "bell", "event": event})
        except Exception:
            log.exception("ticker error")


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background tasks on startup, clean up on shutdown."""
    global config, state, bridge, theme, _ticker_task, _picker_mode

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
    theme = load_theme()

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
        if not find_session_dir(config.resume):
            _match = _find_session_by_name(config.resume)
            if _match:
                _resolved_resume = _match
                config = dataclasses.replace(config, resume=_match)
        state.session_id = _resolved_resume
        state.session_title = read_session_title(_resolved_resume)
        _pending_resume_id = _resolved_resume
    elif not config.no_continue:
        recent = find_most_recent_session_for_cwd(config.cwd)
        if recent is not None:
            state.session_id = recent.stem
            state.session_title = read_session_title(recent.stem)
            _pending_resume_id = recent.stem

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
        bridge = SDKBridge(config=config, state=state, broadcaster=broadcast)
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
    return app


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

    if resume_param is not None:
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

    if cwd_param is not None:
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
        session_dir = find_session_dir(resume)
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
    bridge = SDKBridge(config=config, state=state, broadcaster=broadcast)
    _bridge_ready.set()

    if new_resume:
        state.session_id = new_resume
        title = read_session_title(new_resume)
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
                state.session_title = read_session_title(recent.stem)
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
    """Gracefully shut down the server.

    Used by ``--detach`` when a new server needs to take over the port
    occupied by an old instance, and for manual cleanup.
    """
    log.info("shutdown requested via /api/shutdown")
    if bridge:
        try:
            await bridge.stop()
        except Exception:
            pass
    # Schedule a hard exit after a short delay so the response gets sent.
    asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
    return {"ok": True, "message": "shutting down"}


@_route("get", "/api/status")
async def api_status() -> dict[str, Any]:
    """Return current status bar + panel data."""
    if state is None or config is None:
        return {"status": {}, "panels": {}}
    return {
        "status": state_to_status_dict(state, config),
        "panels": state_to_panels_dict(state),
    }


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

    # Verify the session exists on disk.
    session_dir = find_session_dir(session_id)
    if session_dir is None:
        return {"ok": False, "error": f"Session {session_id} not found"}

    # Update state with session info.
    state.session_id = session_id
    title = read_session_title(session_id)
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

async def _broadcast_queue_update() -> None:
    """Push the current queue state to all connected clients."""
    assert state is not None
    items = [
        {"index": i, "text": text}
        for i, text in enumerate(state.queued_prompts)
    ]
    await broadcast({"type": "queue_update", "queue": items})


@_route("post", "/api/queue/delete")
async def api_queue_delete(body: dict[str, Any]) -> dict[str, Any]:
    """Delete a pending prompt by index."""
    if state is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")
    if not isinstance(idx, int) or idx < 0 or idx >= len(state.queued_prompts):
        return {"ok": False, "error": f"invalid index: {idx}"}
    removed = state.queued_prompts[idx]
    del state.queued_prompts[idx]
    await _broadcast_queue_update()
    return {"ok": True, "removed": _truncate(removed, 80)}


@_route("post", "/api/queue/send")
async def api_queue_send(body: dict[str, Any]) -> dict[str, Any]:
    """Send a queued prompt by index.

    If idle, pops the prompt and puts it on the event queue so the bridge
    picks it up immediately.  If busy, moves it to the front of the queue
    so it executes next when the current turn ends.
    """
    if state is None or bridge is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")
    if not isinstance(idx, int) or idx < 0 or idx >= len(state.queued_prompts):
        return {"ok": False, "error": f"invalid index: {idx}"}
    prompt = state.queued_prompts[idx]
    if state.busy:
        if idx == 0:
            # Already first in line — nothing to do.
            return {"ok": True, "already_next": True}
        # Move to front so it's the next prompt after the current turn.
        del state.queued_prompts[idx]
        state.queued_prompts.appendleft(prompt)
        await _broadcast_queue_update()
        return {"ok": True, "moved_to_front": True}
    del state.queued_prompts[idx]
    bridge.event_queue.put_nowait(("message", prompt))
    await _broadcast_queue_update()
    return {"ok": True, "sent": _truncate(prompt, 80)}


@_route("post", "/api/queue/edit")
async def api_queue_edit(body: dict[str, Any]) -> dict[str, Any]:
    """Edit a pending prompt by index.

    Also clears the editing lock atomically so the bridge doesn't send
    the old text before the update arrives.
    """
    if state is None or bridge is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")
    text = (body.get("text") or "").strip()
    if not isinstance(idx, int) or idx < 0 or idx >= len(state.queued_prompts):
        return {"ok": False, "error": f"invalid index: {idx}"}
    if not text:
        return {"ok": False, "error": "text cannot be empty"}
    # Update text and clear editing lock in one step.
    state.queued_prompts[idx] = text
    state.queue_editing_index = None
    await _broadcast_queue_update()
    # Wake the bridge if idle so it can send the (now updated) prompt.
    if state.queued_prompts and not state.busy:
        bridge.event_queue.put_nowait(("wakeup", "queue-edit-done"))
    return {"ok": True}


@_route("post", "/api/queue/editing")
async def api_queue_editing(body: dict[str, Any]) -> dict[str, Any]:
    """Notify the server that the user is editing (or finished editing) a queue item.

    While a queue item is being edited, the bridge will hold off on
    sending it so the user can finish their edit first.
    """
    if state is None or bridge is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")  # int to start editing, None to stop
    if idx is not None and not isinstance(idx, int):
        return {"ok": False, "error": "index must be int or null"}
    state.queue_editing_index = idx
    if idx is None and state.queued_prompts and not state.busy:
        # Edit finished and bridge is idle — wake it up to send the prompt.
        bridge.event_queue.put_nowait(("wakeup", "queue-edit-done"))
    return {"ok": True}


@_route("post", "/api/queue/merge")
async def api_queue_merge() -> dict[str, Any]:
    """Merge all queued prompts into one, separated by newlines."""
    if state is None:
        return {"ok": False, "error": "not ready"}
    if len(state.queued_prompts) < 2:
        return {"ok": False, "error": "need at least 2 prompts to merge"}
    merged = "\n".join(state.queued_prompts)
    state.queued_prompts.clear()
    state.queued_prompts.append(merged)
    await _broadcast_queue_update()
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
    await broadcast({
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
        # Let the bridge clean up, then hard-exit.  os._exit avoids
        # blocking on uvicorn's graceful-shutdown timeout.
        if bridge:
            try:
                await bridge.stop()
            except Exception:
                pass
        os._exit(0)


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
        # --- Send initial state ---
        await _send_initial_state(ws)

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
        _maybe_start_shutdown_timer()


async def _send_initial_state(ws: WebSocket) -> None:
    """Send session history, status, panels, and completions on connect."""
    assert state is not None and config is not None

    # In picker mode the main chat UI isn't active yet.
    if _picker_mode:
        await send_to(ws, {
            "type": "system_msg",
            "subtype": "info",
            "data": {"message": "Select a session to resume."},
        })
        return

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

    # Clear any stale chat content from a previous session (e.g. browser
    # tab reconnecting after the server restarted with a different cwd).
    await send_to(ws, {"type": "clear_screen"})

    # Session history (if continuing a session).
    if state.session_id:
        try:
            session_dir = find_session_dir(state.session_id)
            if session_dir:
                jsonl = session_dir / f"{state.session_id}.jsonl"
                _count, history_msgs, _orphans = render_session_history(jsonl)
                if history_msgs:
                    await send_to(ws, {
                        "type": "history",
                        "messages": history_msgs,
                    })
        except Exception as exc:
            log.warning("failed to load history for %s: %s", state.session_id, exc)


async def _handle_ws_message(ws: WebSocket, msg: dict[str, Any]) -> None:
    """Dispatch an incoming WebSocket message."""
    # The bridge is constructed off the hot startup path, so a very fast user
    # action can arrive before it exists.  Wait for it (the page already shows
    # "connecting…"); it's ready within a fraction of a second.
    if bridge is None:
        await _bridge_ready.wait()
    assert state is not None and config is not None and bridge is not None
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
                await _broadcast_queue_update()
            else:
                bridge.event_queue.put_nowait(("message", prompt))
            return

        # User messages while busy or connecting go to the pending queue
        # (displayed in the queue panel).  The bridge's _between_turns()
        # drains state.queued_prompts between turns; worker_loop() also
        # checks queued_prompts after the initial connect completes.
        if kind == "message" and (state.busy or state.connecting):
            state.queued_prompts.append(payload)
            await _broadcast_queue_update()
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


def _bind_port(host: str, port: int) -> tuple[_socket.socket, int]:
    """Try to bind *port*; on failure, let the OS pick a free one.

    On Windows, ``SO_REUSEADDR`` silently allows multiple processes to
    bind the same port — the OS then routes connections unpredictably.
    We use ``SO_EXCLUSIVEADDRUSE`` instead so the bind properly fails
    when the port is already occupied.  On other platforms, the standard
    ``SO_REUSEADDR`` is used to allow reusing TIME_WAIT addresses.
    """
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    if sys.platform == "win32":
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
    else:
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.bind((host, 0))
    actual = sock.getsockname()[1]
    sock.set_inheritable(True)
    return sock, actual


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
            child_argv.append(a)
            i += 1
        child_argv.extend(["--port", str(actual_port), "--auto-shutdown",
                           "--skip-auto-login"])

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
            webbrowser.open(f"{url}?t={int(time.time())}")

        sys.exit(0)

    # Auto-login (non-detach path).  The --detach child skips this because the
    # parent already ensured we're signed in.  Non-blocking: the login window
    # opens and startup proceeds; the worker's connect-retry succeeds once the
    # credentials appear.
    if not config.skip_auto_login:
        _ensure_logged_in(block=False)

    # Bind the socket ourselves so we know the actual port before
    # uvicorn starts (needed for --open and the startup message).
    sock, actual_port = _bind_port("0.0.0.0", config.port)

    if actual_port != config.port:
        print(f"Port {config.port} in use — using {actual_port} instead.")
        config = dataclasses.replace(config, port=actual_port)

    url = f"http://localhost:{actual_port}"
    msg = f"orchestrator2 starting on {url}"
    if config.config_dir:
        msg += f"  (config-dir: {config.config_dir})"
    print(msg)

    # Open browser once the server is fully initialised (lifespan done).
    # The cache-buster query param forces the browser to do a fresh
    # navigation instead of just focusing an existing tab with the same
    # URL (which would show stale content from a previous session).
    if config.open_browser:
        open_url = f"{url}?t={int(time.time())}"
        def _open_when_ready():
            # `_server_ready` is set at the END of lifespan STARTUP, but uvicorn
            # calls listen() only AFTER lifespan startup returns.  So the port is
            # bound-but-not-listening when the event fires — opening the browser
            # here directly races and lands on a dead port ("can't connect").
            # After the event, poll a real TCP connect (matching the --detach
            # parent's probe loop) until the port genuinely accepts connections.
            _server_ready.wait(timeout=30)
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                probe.settimeout(0.25)
                try:
                    probe.connect(("127.0.0.1", actual_port))
                    break
                except OSError:
                    time.sleep(0.05)
                finally:
                    probe.close()
            webbrowser.open(open_url)
        _threading.Thread(target=_open_when_ready, daemon=True).start()

    # Build the FastAPI app now (imports fastapi lazily) — only the serving
    # process pays this cost; the --detach parent returned long ago.
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
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
