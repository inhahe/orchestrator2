"""Slash command parser and dispatcher.

Ports ``classify()`` and the immediate-command dispatch table from the
original orchestrator, adapted for the web: commands return structured
``CommandResult`` dicts instead of printing ANSI to stdout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    BELL_EVENT_NAMES,
    EFFORT_CHOICES,
    KNOWN_MODELS,
    SLASH_COMMANDS,
    Config,
    _parse_bell_spec,
    parse_bell_events,
)
from state import (
    State,
    fmt_duration,
    fmt_tok,
    state_to_status_dict,
)
from session import (
    find_session_dir,
    read_session_title,
    render_session_history,
    render_session_markdown,
    write_session_title,
)


# ---------------------------------------------------------------------------
# Command result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    """Structured response from executing a slash command."""

    # What to render in the frontend.
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Partial state update to send as ``status_update`` WS message.
    state_updates: dict[str, Any] | None = None

    # If True, ``forward_payload`` should be queued as a turn prompt.
    forward_to_sdk: bool = False
    forward_payload: str | None = None


def _msg(text: str, *, level: str = "info") -> dict[str, Any]:
    """Helper: build a simple system message dict."""
    return {"type": "system_msg", "subtype": level, "data": {"message": text}}


def _data_msg(data: Any, *, label: str = "data") -> dict[str, Any]:
    """Helper: build a data-carrying system message."""
    return {"type": "command_data", "label": label, "data": data}


# ---------------------------------------------------------------------------
# classify() — command parser
# ---------------------------------------------------------------------------

def classify(line: str) -> tuple[str, str]:
    """Parse a line of user input into ``(kind, payload)``.

    Returns one of:
    - ``("message", text)`` — plain user message for Claude
    - ``("empty", "")`` — blank input
    - ``("error", reason)`` — parse error
    - ``(command_kind, payload)`` — a slash command
    """
    s = line.strip()
    if not s:
        return "empty", ""
    if not s.startswith("/"):
        return "message", s

    parts = s[1:].split(None, 1)
    if not parts:
        return "error", "empty slash command (try /help)"
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("i", "interrupt"):
        return "interrupt", ""
    if cmd in ("q", "quit", "exit"):
        return "quit", ""
    if cmd in ("quit!", "exit!", "q!"):
        return "force-quit", ""
    if cmd == "compact":
        return "compact", ""
    if cmd == "status":
        return "status", ""
    if cmd == "debug":
        return "debug", ""
    if cmd == "help":
        return "help", ""

    if cmd == "effort":
        if not arg:
            return "effort-show", ""
        val = arg.lower()
        if val in EFFORT_CHOICES:
            return "effort", val
        return "error", f"effort must be one of {', '.join(EFFORT_CHOICES)}"

    if cmd in ("model", "models"):
        return ("model", arg) if arg else ("model-show", "")

    if cmd == "rename":
        return "rename", arg

    if cmd == "thinking":
        val = arg.lower()
        if val in ("on", "true", "1", "yes", "enable"):
            return "thinking", "on"
        if val in ("off", "false", "0", "no", "disable"):
            return "thinking", "off"
        if val in ("", "toggle"):
            return "thinking", ""
        return "error", "usage: /thinking [on|off|toggle]"

    if cmd == "export":
        return "export", arg
    if cmd == "btw":
        if not arg:
            return "error", "usage: /btw <question>"
        return "btw", arg
    if cmd in ("collapse", "collapse-tools"):
        return "collapse", arg
    if cmd in ("collapse-threshold",):
        return "collapse-threshold", arg
    if cmd in ("autocompact", "auto-compact"):
        return "autocompact", arg
    if cmd in ("max-context", "maxcontext", "max-ctx"):
        return "max-context", arg
    if cmd == "bell":
        return "bell", arg
    if cmd == "queue":
        return "queue", arg
    if cmd == "graphify":
        return "graphify", arg
    if cmd == "history":
        return "history", arg
    if cmd == "clear":
        return "clear-context", ""
    if cmd == "cls":
        return "clear-screen", ""
    if cmd == "cost":
        return "status", ""
    if cmd == "cwd":
        if arg:
            return "switch-cwd", arg
        return "status", ""
    if cmd in ("connect", "reconnect"):
        return "connect", ""
    if cmd == "resume":
        return ("resume", arg) if arg else ("resume-pick", "")

    return "error", f"unknown command /{cmd} (try /help)"


# ---------------------------------------------------------------------------
# Immediate commands — execute synchronously, return result
# ---------------------------------------------------------------------------

# Map of kind → handler function name (on this module).
_IMMEDIATE_HANDLERS: dict[str, str] = {
    "status":          "_cmd_status",
    "help":            "_cmd_help",
    "debug":           "_cmd_debug",
    "clear-screen":    "_cmd_clear_screen",
    "rename":          "_cmd_rename",
    "export":          "_cmd_export",
    "collapse":        "_cmd_collapse",
    "collapse-threshold": "_cmd_collapse_threshold",
    "autocompact":     "_cmd_autocompact",
    "max-context":     "_cmd_max_context",
    "bell":            "_cmd_bell",
    "queue":           "_cmd_queue",
    "history":         "_cmd_history",
    "effort-show":     "_cmd_effort_show",
    "model-show":      "_cmd_model_show",
}


def try_immediate_command(
    kind: str,
    payload: str,
    state: State,
    config: Config,
) -> CommandResult | None:
    """Execute an immediate command if ``kind`` matches.

    Returns ``None`` if this is not an immediate command (should be
    queued for the SDK worker instead).
    """
    handler_name = _IMMEDIATE_HANDLERS.get(kind)
    if handler_name is None:
        return None
    handler = globals()[handler_name]
    return handler(payload, state, config)


def get_command_completions() -> list[str]:
    """Return all slash command names for tab-complete."""
    return sorted(SLASH_COMMANDS)


# ---------------------------------------------------------------------------
# Individual command handlers
# ---------------------------------------------------------------------------

def _cmd_help(_payload: str, _state: State, _config: Config) -> CommandResult:
    lines = [
        "Commands:",
        "  /help                           this help",
        "  /status  /cost                  session info, cost, usage",
        "  /cwd [path]                     show or switch working directory",
        "  /clear                          start a fresh session (wipes context)",
        "  /cls                            clear the output area",
        "  /history [N]                    replay session history (last N records)",
        "  /interrupt  /i                  stop the current turn",
        f"  /effort <level>                 one of {', '.join(EFFORT_CHOICES)}",
        "  /thinking [on|off|toggle]       enable/disable extended thinking",
        "  /model [name]                   show/set model; no arg lists available",
        "  /connect                        reconnect to the SDK",
        "  /resume [id|title]              resume a session (or open picker)",
        "  /rename <name>                  set a custom session title",
        "  /export [path]                  save conversation as markdown",
        "  /btw <question>                 side question (separate context)",
        "  /graphify [path] [flags]        build a knowledge graph (graphify)",
        "  /collapse [on|off]              toggle collapsing of repeated blocks",
        "  /collapse-threshold N           blocks shown before collapsing",
        "  /autocompact [on|off|N]         auto-compact threshold",
        "  /max-context [off|N]            cap context tokens",
        "  /bell [all|none|EVENTS]         view/change bell events",
        "  /queue [N|send|drop N|clear]    manage queued prompts",
        "  /quit  /exit                    graceful exit",
        "  /quit! /exit!                   force exit",
        "",
        "Input: Enter submits.  Shift+Enter for newline.  Ctrl+C: interrupt/clear.",
        "       Ctrl+Up/Down: prompt history.",
    ]
    return CommandResult(messages=[_msg("\n".join(lines))])


def _cmd_status(_payload: str, state: State, config: Config) -> CommandResult:
    status = state_to_status_dict(state, config)
    info = {
        "session_id": state.session_id,
        "session_title": state.session_title or "(unnamed)",
        "cwd": str(Path(config.cwd).resolve()),
        "turns": state.turns,
        "context_tokens": state.context_tokens,
        "cost": f"${state.total_cost_usd:.4f}",
        "effort": state.effort or "auto",
        "model": state.model or state.active_model or "(auto)",
        "thinking": "on" if state.thinking_enabled else "off",
        "last_result": state.last_result_subtype,
        "last_usage": state.last_usage,
        "status": status,
    }
    return CommandResult(messages=[_data_msg(info, label="status")])


def _cmd_debug(_payload: str, state: State, _config: Config) -> CommandResult:
    info = {
        "busy": state.busy,
        "connecting": state.connecting,
        "needs_user_attention": state.needs_user_attention,
        "active_tools": len(state.active_tools),
        "background_tasks": len(state.background_tasks),
        "queued_prompts": len(state.queued_prompts),
        "pending_bell": state.pending_bell,
        "rate_limit_status": state.rate_limit_status,
        "inline_all_tools": state.inline_all_tools,
        "panel_delay": state.panel_delay,
    }
    return CommandResult(messages=[_data_msg(info, label="debug")])


def _cmd_clear_screen(_payload: str, _state: State, _config: Config) -> CommandResult:
    return CommandResult(messages=[{"type": "clear_screen"}])


def _cmd_history(payload: str, state: State, _config: Config) -> CommandResult:
    sid = state.session_id
    if not sid:
        return CommandResult(messages=[_msg("No active session.", level="warning")])
    session_dir = find_session_dir(sid)
    if session_dir is None:
        return CommandResult(messages=[_msg(f"Session {sid[:8]} not found on disk.", level="error")])
    jsonl = session_dir / f"{sid}.jsonl"
    if not jsonl.exists():
        return CommandResult(messages=[_msg(f"Session file not found.", level="error")])

    # Parse optional record limit.
    limit = 2000
    p = payload.strip()
    if p:
        try:
            limit = int(p)
        except ValueError:
            return CommandResult(messages=[_msg("Usage: /history [N]", level="error")])
        if limit <= 0:
            return CommandResult(messages=[_msg("N must be positive.", level="error")])

    try:
        _count, history_msgs, _orphans = render_session_history(jsonl, max_history=limit)
    except Exception as e:
        return CommandResult(messages=[_msg(f"History load failed: {e}", level="error")])

    msgs: list[dict[str, Any]] = [{"type": "clear_screen"}]
    if history_msgs:
        msgs.append({"type": "history", "messages": history_msgs})
    else:
        msgs.append(_msg("No history to display."))
    return CommandResult(messages=msgs)


def _cmd_rename(payload: str, state: State, _config: Config) -> CommandResult:
    sid = state.session_id
    if not sid:
        return CommandResult(messages=[_msg(
            "No active session yet — run /rename after the first turn.",
            level="warning",
        )])
    new_title = payload.strip()
    if not new_title:
        current = read_session_title(sid)
        if current:
            return CommandResult(messages=[_msg(f"Current title: {current}")])
        return CommandResult(messages=[_msg("No title set. Usage: /rename <name>", level="warning")])
    try:
        write_session_title(sid, new_title)
    except (OSError, ValueError) as e:
        return CommandResult(messages=[_msg(f"Rename failed: {e}", level="error")])
    state.session_title = new_title
    return CommandResult(
        messages=[_msg(f"Renamed session {sid[:8]} → '{new_title}'")],
        state_updates={"session_title": new_title},
    )


def _cmd_export(payload: str, state: State, config: Config) -> CommandResult:
    sid = state.session_id
    if not sid:
        return CommandResult(messages=[_msg("No active session yet.", level="warning")])
    project_dir = find_session_dir(sid)
    if project_dir is None:
        return CommandResult(messages=[_msg(f"Session {sid[:8]} not found on disk.", level="error")])
    jsonl = project_dir / f"{sid}.jsonl"
    if not jsonl.exists():
        return CommandResult(messages=[_msg(f"Session file not found: {jsonl}", level="error")])

    path_arg = payload.strip()
    if path_arg:
        out_path = Path(path_arg).expanduser()
        if out_path.is_dir():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out_path = out_path / f"claude-{sid[:8]}-{stamp}.md"
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = Path(config.cwd) / f"claude-{sid[:8]}-{stamp}.md"

    try:
        markdown = render_session_markdown(jsonl)
    except Exception as e:
        return CommandResult(messages=[_msg(f"Export failed: {e}", level="error")])
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
    except OSError as e:
        return CommandResult(messages=[_msg(f"Export write failed: {e}", level="error")])
    size = out_path.stat().st_size
    return CommandResult(messages=[_msg(f"Exported → {out_path.resolve()} ({size} bytes)")])


def _cmd_collapse(payload: str, state: State, _config: Config) -> CommandResult:
    p = payload.strip().lower()
    if not p:
        label = "ON" if state.collapse_tools else "OFF"
        return CommandResult(messages=[_msg(f"collapse repeats: {label}")])
    if p in ("on", "true", "enable", "1"):
        state.collapse_tools = True
        return CommandResult(
            messages=[_msg("collapse repeats ON")],
            state_updates={"collapse_tools": True},
        )
    if p in ("off", "false", "disable", "0"):
        state.collapse_tools = False
        return CommandResult(
            messages=[_msg("collapse repeats OFF")],
            state_updates={"collapse_tools": False},
        )
    return CommandResult(messages=[_msg("Usage: /collapse [on|off]", level="error")])


def _cmd_collapse_threshold(payload: str, state: State, _config: Config) -> CommandResult:
    p = payload.strip()
    if not p:
        return CommandResult(messages=[_msg(f"collapse-threshold: {state.collapse_threshold}")])
    try:
        n = int(p)
    except ValueError:
        return CommandResult(messages=[_msg("Usage: /collapse-threshold <N>", level="error")])
    if n < 1:
        return CommandResult(messages=[_msg("Threshold must be at least 1.", level="error")])
    state.collapse_threshold = n
    return CommandResult(
        messages=[_msg(f"collapse-threshold → {n}")],
        state_updates={"collapse_threshold": n},
    )


def _cmd_autocompact(payload: str, state: State, config: Config) -> CommandResult:
    p = payload.strip().lower()
    if not p:
        status = "OFF" if config.no_compact else f"ON (at ~{config.compact_at} tok)"
        return CommandResult(messages=[_msg(f"auto-compact: {status}")])
    if p in ("on", "true", "enable", "1"):
        state._auto_compact = True  # type: ignore[attr-defined]
        return CommandResult(
            messages=[_msg(f"auto-compact ON (at ~{config.compact_at} tok)")],
            state_updates={"auto_compact": True},
        )
    if p in ("off", "false", "disable", "0"):
        state._auto_compact = False  # type: ignore[attr-defined]
        return CommandResult(
            messages=[_msg("auto-compact OFF")],
            state_updates={"auto_compact": False},
        )
    try:
        n = int(p.replace(",", "").replace("_", ""))
    except ValueError:
        return CommandResult(messages=[_msg("Usage: /autocompact [on|off|N]", level="error")])
    if n <= 0:
        return CommandResult(messages=[_msg("/autocompact N must be positive", level="error")])
    state._compact_at = n  # type: ignore[attr-defined]
    state._auto_compact = True  # type: ignore[attr-defined]
    return CommandResult(
        messages=[_msg(f"auto-compact threshold → ~{n} tok (ON)")],
        state_updates={"auto_compact": True, "compact_at": n},
    )


def _cmd_max_context(payload: str, state: State, config: Config) -> CommandResult:
    p = payload.strip().lower()
    if not p:
        cur = config.max_context_tokens
        status = "unlimited" if not cur else f"~{cur} tok"
        return CommandResult(messages=[_msg(f"max context: {status}")])
    if p in ("off", "none", "unlimited", "0"):
        state._max_context_tokens = 0  # type: ignore[attr-defined]
        return CommandResult(
            messages=[_msg("max context: unlimited")],
            state_updates={"max_context_tokens": 0},
        )
    try:
        n = int(p.replace(",", "").replace("_", ""))
    except ValueError:
        return CommandResult(messages=[_msg("Usage: /max-context [off|N]", level="error")])
    if n <= 0:
        return CommandResult(messages=[_msg("/max-context N must be positive", level="error")])
    state._max_context_tokens = n  # type: ignore[attr-defined]
    return CommandResult(
        messages=[_msg(f"max context → ~{n} tok (rolling-window trim)")],
        state_updates={"max_context_tokens": n},
    )


def _cmd_bell(payload: str, state: State, _config: Config) -> CommandResult:
    payload = (payload or "").strip()
    if not payload:
        if not state.bell_events:
            events_str = "disabled (no events)"
        else:
            events_str = ",".join(sorted(state.bell_events))
        avail = ", ".join(sorted(BELL_EVENT_NAMES))
        return CommandResult(messages=[_msg(
            f"bell: {events_str}\n  available: {avail}"
        )])
    result = _parse_bell_spec(payload)
    if result == "all":
        state.bell_events = set(BELL_EVENT_NAMES)
    elif result == "none":
        state.bell_events = set()
    else:
        assert isinstance(result, dict)
        if not result:
            return CommandResult(messages=[_msg(
                f"No valid events in '{payload}'", level="error",
            )])
        for name, enable in result.items():
            if enable:
                state.bell_events.add(name)
            else:
                state.bell_events.discard(name)
    events_str = ",".join(sorted(state.bell_events)) or "(none)"
    return CommandResult(messages=[_msg(f"bell: {events_str}")])


def _cmd_queue(payload: str, state: State, _config: Config) -> CommandResult:
    payload = (payload or "").strip()
    q = state.queued_prompts
    if not payload:
        if not q:
            return CommandResult(messages=[_msg("Queued prompts: none")])
        items = []
        for i, prompt in enumerate(q, start=1):
            first = prompt.splitlines()[0] if prompt else ""
            n_lines = len(prompt.splitlines())
            more = f" (+{n_lines - 1} lines)" if n_lines > 1 else ""
            items.append({"index": i, "preview": first, "extra": more})
        return CommandResult(messages=[_data_msg(items, label="queue")])

    parts = payload.split()
    if parts[0].lower() in ("send", "next"):
        if not q:
            return CommandResult(messages=[_msg("Queue is empty", level="error")])
        if state.busy:
            return CommandResult(messages=[_msg(
                "Claude is working — queued prompts will send after the current turn",
                level="warning",
            )])
        prompt = q.popleft()
        first = prompt.splitlines()[0][:80] if prompt else ""
        return CommandResult(
            messages=[_msg(f"Sending: {first}")],
            forward_to_sdk=True,
            forward_payload=prompt,
        )
    if parts[0].lower() == "clear":
        n = len(q)
        q.clear()
        return CommandResult(messages=[_msg(f"Queue cleared ({n} removed)")])
    if parts[0].lower() == "drop" and len(parts) >= 2:
        try:
            idx = int(parts[1]) - 1
        except ValueError:
            return CommandResult(messages=[_msg("/queue drop N — N must be integer", level="error")])
        if idx < 0 or idx >= len(q):
            return CommandResult(messages=[_msg(
                f"/queue drop {idx + 1} — out of range (queue has {len(q)})",
                level="error",
            )])
        removed = q[idx]
        del q[idx]
        first = removed.splitlines()[0][:60] if removed else ""
        return CommandResult(messages=[_msg(f"Dropped #{idx + 1}: {first}")])
    try:
        idx = int(parts[0]) - 1
    except ValueError:
        return CommandResult(messages=[_msg("Usage: /queue [N|send|drop N|clear]", level="error")])
    if idx < 0 or idx >= len(q):
        return CommandResult(messages=[_msg(
            f"/queue {idx + 1} — out of range (queue has {len(q)})",
            level="error",
        )])
    return CommandResult(messages=[_data_msg({"index": idx + 1, "text": q[idx]}, label="queue_detail")])


def _cmd_effort_show(_payload: str, state: State, _config: Config) -> CommandResult:
    current = state.effort or "auto"
    descs = {
        "auto": "no override — model picks its default",
        "low": "minimal thinking budget",
        "medium": "moderate thinking budget",
        "high": "generous thinking budget",
        "max": "maximum thinking budget",
    }
    levels = [{"name": lv, "description": descs.get(lv, ""), "current": lv == current}
              for lv in EFFORT_CHOICES]
    data = {"current": current, "levels": levels}
    return CommandResult(messages=[_data_msg(data, label="effort")])


def _cmd_model_show(_payload: str, state: State, _config: Config) -> CommandResult:
    pinned = state.model
    active = state.active_model
    if pinned:
        current = f"{pinned} (pinned)"
    elif active:
        current = f"{active} (CLI-picked)"
    else:
        current = "(auto — no AssistantMessage received yet)"
    models = [{"id": m, "description": d} for m, d in KNOWN_MODELS]
    data = {"current": current, "models": models}
    return CommandResult(messages=[_data_msg(data, label="model")])
