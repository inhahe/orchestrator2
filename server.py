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
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import dataclasses
import webbrowser

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from config import (
    Config,
    DEFAULT_PORT,
    _PICKER_SENTINEL,
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
from sdk_bridge import SDKBridge

log = logging.getLogger("orchestrator2")

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
    if config is None:
        config = parse_args()
    state = init_state_from_config(config)
    theme = load_theme()

    bridge = SDKBridge(
        config=config,
        state=state,
        broadcaster=broadcast,
    )

    _ticker_task = asyncio.create_task(_status_ticker(), name="status-ticker")

    # Seed session_id early so the browser gets history on first connect
    # (before the SDK's init message arrives).  Same approach as the TUI
    # orchestrator's pre-connect seeding.
    if config.resume and config.resume != _PICKER_SENTINEL:
        state.session_id = config.resume
        state.session_title = read_session_title(config.resume)
    elif not config.no_continue:
        recent = find_most_recent_session_for_cwd(config.cwd)
        if recent is not None:
            state.session_id = recent.stem
            state.session_title = read_session_title(recent.stem)

    # If --resume was passed without an argument, defer SDK start until the
    # user picks a session from the graphical picker.
    if config.resume == _PICKER_SENTINEL:
        _picker_mode = True
        log.info("picker mode — waiting for session selection on port %s", config.port)
    else:
        if config.resume:
            bridge._initial_resume_id = config.resume
        await bridge.start()
        log.info("server started on port %s, cwd=%s", config.port, config.cwd)

    yield

    # --- Shutdown ---
    if bridge:
        await bridge.stop()
    if _ticker_task and not _ticker_task.done():
        _ticker_task.cancel()
        try:
            await _ticker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Orchestrator 2", docs_url=None, redoc_url=None, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Static files — serve the web UI
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_model=None)
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
    state = init_state_from_config(config)
    bridge = SDKBridge(config=config, state=state, broadcaster=broadcast)

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

@app.get("/static/{path:path}")
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

@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    """Return current status bar + panel data."""
    if state is None or config is None:
        return {"status": {}, "panels": {}}
    return {
        "status": state_to_status_dict(state, config),
        "panels": state_to_panels_dict(state),
    }


@app.get("/api/completions")
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


@app.get("/api/sessions")
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


@app.post("/api/resume")
async def api_resume(body: dict[str, Any]) -> dict[str, Any]:
    """Select a session from the picker and start the SDK bridge."""
    global _picker_mode

    if not _picker_mode:
        return {"ok": False, "error": "Not in picker mode"}

    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "No session_id provided"}

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


@app.post("/api/queue/delete")
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


@app.post("/api/queue/edit")
async def api_queue_edit(body: dict[str, Any]) -> dict[str, Any]:
    """Edit a pending prompt by index."""
    if state is None:
        return {"ok": False, "error": "not ready"}
    idx = body.get("index")
    text = (body.get("text") or "").strip()
    if not isinstance(idx, int) or idx < 0 or idx >= len(state.queued_prompts):
        return {"ok": False, "error": f"invalid index: {idx}"}
    if not text:
        return {"ok": False, "error": "text cannot be empty"}
    state.queued_prompts[idx] = text
    await _broadcast_queue_update()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Theme / settings API
# ---------------------------------------------------------------------------

@app.get("/api/theme")
async def api_theme() -> dict[str, Any]:
    """Return current theme tokens, grouped for the settings UI."""
    t = theme or load_theme()
    return {
        "groups": t.groups(),
        "saved_colors": load_saved_colors(),
        "presets": list(PRESET_THEMES.keys()),
    }


@app.post("/api/theme")
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


@app.post("/api/theme/preset")
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


@app.post("/api/theme/reset")
async def api_theme_reset() -> dict[str, Any]:
    """Reset all tokens to defaults."""
    global theme
    theme = Theme()
    save_theme(theme)
    return {"ok": True, "css": "", "groups": theme.groups()}


@app.post("/api/theme/saved-colors")
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


@app.get("/settings", response_model=None)
async def settings_page():
    """Serve the theme settings page."""
    page = STATIC_DIR / "settings.html"
    if page.exists():
        return FileResponse(page, media_type="text/html")
    return HTMLResponse("<h1>Settings</h1><p>settings.html not found.</p>")


# ---------------------------------------------------------------------------
# WebSocket endpoint — main real-time channel
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Handle one WebSocket client connection."""
    await ws.accept()

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

        # User messages while busy go to the pending queue (displayed
        # in the queue panel).  The bridge's _between_turns() drains
        # state.queued_prompts between turns.
        if kind == "message" and state.busy:
            state.queued_prompts.append(payload)
            await _broadcast_queue_update()
            return

        # Everything else (commands, messages while idle) → event queue.
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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Launch the server."""
    global config

    # Parse args early so we can get the port and store for startup().
    config = parse_args()
    port = config.port

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    url = f"http://localhost:{port}"
    print(f"orchestrator2 starting on {url}")

    # Open browser after a short delay (uvicorn.run blocks, so use a timer).
    if config.open_browser:
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
