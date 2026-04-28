"""Tool execution tracking, history, and structured output for the frontend.

Manages active_tools, background_tasks, tool_history, thinking_history in
State, and produces the structured dicts that the WebSocket broadcasts.
"""

from __future__ import annotations

import time
from typing import Any

from config import SHORT_OUTPUT_TOOLS
from state import State, fmt_duration, humanize_size


# ---------------------------------------------------------------------------
# Tool-use formatting for the frontend
# ---------------------------------------------------------------------------

def format_tool_header(name: str, inp: dict[str, Any]) -> dict[str, Any]:
    """Produce a structured header dict for a tool call.

    The frontend uses this to render the one-liner header in collapsed
    mode.  Every tool type gets a customised summary.
    """
    header: dict[str, Any] = {"name": name}

    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        bg = bool(inp.get("run_in_background"))
        # First non-empty line as preview (up to 80 chars).
        lines = [ln for ln in (cmd or "").splitlines() if ln.strip()]
        preview = (lines[0][:80] + "…") if lines and len(lines[0]) > 80 else (lines[0] if lines else "")
        header.update(description=desc, command_preview=preview, is_background=bg)

    elif name == "Edit":
        path = inp.get("file_path", "?")
        old = inp.get("old_string", "") or ""
        new = inp.get("new_string", "") or ""
        removed = len(old.splitlines()) or (1 if old else 0)
        added = len(new.splitlines()) or (1 if new else 0)
        replace_all = bool(inp.get("replace_all"))
        header.update(
            file_path=path, lines_added=added, lines_removed=removed,
            replace_all=replace_all,
        )

    elif name == "Write":
        path = inp.get("file_path", "?")
        content = inp.get("content", "") or ""
        header.update(
            file_path=path,
            line_count=len(content.splitlines()),
            char_count=len(content),
        )

    elif name == "Read":
        path = inp.get("file_path", "?")
        header.update(
            file_path=path,
            offset=inp.get("offset"),
            limit=inp.get("limit"),
        )

    elif name == "Grep":
        header.update(
            pattern=inp.get("pattern", ""),
            path=inp.get("path", "."),
            output_mode=inp.get("output_mode"),
        )

    elif name == "Glob":
        header.update(
            pattern=inp.get("pattern", ""),
            path=inp.get("path", "."),
        )

    elif name == "Task":
        header.update(
            subagent_type=inp.get("subagent_type", "general-purpose"),
            description=(inp.get("description") or "")[:60],
        )

    elif name == "WebFetch":
        header.update(url=inp.get("url", "?"))

    elif name == "WebSearch":
        header.update(query=inp.get("query", "?"))

    elif name == "NotebookEdit":
        header.update(notebook_path=inp.get("notebook_path", "?"))

    elif name == "TodoWrite":
        header.update(todo_count=len(inp.get("todos", []) or []))

    elif name == "Agent":
        header.update(
            subagent_type=inp.get("subagent_type", "general-purpose"),
            description=(inp.get("description") or "")[:60],
        )

    return header


def format_tool_use_msg(
    name: str,
    inp: dict[str, Any],
    tool_use_id: str,
    seq: int,
    *,
    is_background: bool = False,
) -> dict[str, Any]:
    """Produce the full ``tool_use`` WebSocket message for a new tool call."""
    return {
        "type": "tool_use",
        "seq": seq,
        "tool_use_id": tool_use_id,
        "name": name,
        "input": inp,
        "status": "running",
        "is_background": is_background,
        "header": format_tool_header(name, inp),
    }


def format_tool_result_msg(
    tool_use_id: str,
    seq: int | None,
    name: str,
    text: str,
    is_error: bool,
) -> dict[str, Any]:
    """Produce the ``tool_result`` WebSocket message for a completed tool."""
    return {
        "type": "tool_result",
        "seq": seq,
        "tool_use_id": tool_use_id,
        "name": name,
        "is_error": is_error,
        "content": text,
        "size_hint": humanize_size(text),
        "status": "error" if is_error else "complete",
    }


# ---------------------------------------------------------------------------
# Tool registration / completion
# ---------------------------------------------------------------------------

def register_tool_use(
    state: State,
    tool_use_id: str,
    name: str,
    inp: dict[str, Any],
    *,
    parent_id: str | None = None,
) -> int:
    """Record a new tool call in state.  Returns the assigned seq number."""
    seq = state.next_tool_seq
    state.next_tool_seq += 1
    started = time.monotonic()

    first_shown: float | None = None
    if state.panel_delay <= 0:
        first_shown = started

    state.active_tools[tool_use_id] = {
        "name": name,
        "input": inp,
        "started_at": started,
        "seq": seq,
        "parent_id": parent_id,
        "sub_trail": [] if name == "Task" else None,
        "current_sub_id": None,
        "first_shown_at": first_shown,
    }

    # Track non-Bash, non-sub-tool seqs for the /tasks panel.
    if name != "Bash" and not parent_id:
        state.current_turn_tool_seqs.append(seq)

    # If this is a sub-tool of an active Task, link it.
    if parent_id and parent_id in state.active_tools:
        parent = state.active_tools[parent_id]
        trail = parent.get("sub_trail")
        if trail is None:
            trail = []
            parent["sub_trail"] = trail
        trail.append({
            "name": name,
            "input": inp,
            "seq": seq,
            "tool_use_id": tool_use_id,
        })
        parent["current_sub_id"] = tool_use_id

    state.tool_history.append({
        "seq": seq,
        "tool_use_id": tool_use_id,
        "name": name,
        "input": inp,
        "started_at": started,
        "result_text": None,
        "is_error": None,
        "ended_at": None,
    })

    return seq


def complete_tool(
    state: State,
    tool_use_id: str,
    result_text: str,
    is_error: bool,
) -> tuple[int | None, str | None, dict[str, Any] | None]:
    """Mark a tool as completed.

    Returns ``(seq, tool_name, active_info)`` — the popped active entry.
    """
    active = state.active_tools.pop(tool_use_id, None)

    # Panel grace: keep completed tool visible with ✓ for a while.
    if (
        active
        and active.get("first_shown_at") is not None
        and not active.get("parent_id")
        and state.panel_grace > 0
    ):
        shown_for = time.monotonic() - active["first_shown_at"]
        if shown_for < state.panel_grace:
            grace_end = active["first_shown_at"] + state.panel_grace
            state.completed_panel_tools[tool_use_id] = {
                **active,
                "completed_at": time.monotonic(),
                "grace_end": grace_end,
                "is_error": is_error,
                "duration": time.monotonic() - active["started_at"],
            }

    # Clear parent Task's "currently running sub-tool" pointer.
    if active and active.get("parent_id"):
        parent = state.active_tools.get(active["parent_id"])
        if parent and parent.get("current_sub_id") == tool_use_id:
            parent["current_sub_id"] = None

    # If this was a Task tool result, clean up leaked bg entries.
    if active and active.get("name") == "Task":
        leaked = [
            tid for tid, bg in state.background_tasks.items()
            if bg.get("tool_use_id") == tool_use_id
        ]
        for tid in leaked:
            state.background_tasks.pop(tid, None)

    seq = active.get("seq") if active else None
    tool_name = active.get("name") if active else None

    # Update history ring buffer.
    for h in reversed(state.tool_history):
        if h["tool_use_id"] == tool_use_id:
            h["result_text"] = result_text
            h["is_error"] = is_error
            h["ended_at"] = time.monotonic()
            break

    return seq, tool_name, active


# ---------------------------------------------------------------------------
# Background task registration / completion
# ---------------------------------------------------------------------------

def register_bg_task(
    state: State,
    task_id: str,
    task_type: str,
    name: str,
    *,
    tool_use_id: str | None = None,
) -> int:
    """Record a new background task.  Returns the assigned bg seq."""
    seq = state.next_bg_seq
    state.next_bg_seq += 1
    started = time.monotonic()

    first_shown: float | None = None
    if state.panel_delay <= 0:
        first_shown = started

    entry = {
        "seq": seq,
        "name": name,
        "task_type": task_type,
        "started_at": started,
        "tool_use_id": tool_use_id,
        "first_shown_at": first_shown,
        "ended_at": None,
        "status": None,
        "summary": None,
    }
    state.background_tasks[task_id] = entry
    state.current_turn_bg[task_id] = dict(entry)

    return seq


def complete_bg_task(
    state: State,
    task_id: str,
    status: str,
    *,
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Mark a background task as completed.

    Returns the task info dict if this is the first completion (not a
    duplicate), or ``None`` if already handled.
    """
    entry = state.background_tasks.get(task_id)
    if entry is None:
        # Check current_turn_bg for tasks that were already popped.
        entry = state.current_turn_bg.get(task_id)
        if entry is None:
            return None
        # Already completed — deduplicate.
        if entry.get("ended_at") is not None:
            return None

    now = time.monotonic()

    # Deduplicate.
    if entry.get("ended_at") is not None:
        return None

    entry["ended_at"] = now
    entry["status"] = status
    entry["summary"] = summary

    # Pop from running; optionally keep in grace.
    state.background_tasks.pop(task_id, None)
    state.current_turn_bg[task_id] = entry

    if (
        entry.get("first_shown_at") is not None
        and state.panel_grace > 0
    ):
        grace_end = entry["first_shown_at"] + state.panel_grace
        state.completed_panel_bg[task_id] = {
            **entry,
            "grace_end": grace_end,
            "duration": now - entry["started_at"],
        }

    return entry


# ---------------------------------------------------------------------------
# Thinking block tracking
# ---------------------------------------------------------------------------

def register_thinking(
    state: State,
    content: str,
) -> int:
    """Record a thinking block.  Returns the assigned thinking seq."""
    seq = state.next_thinking_seq
    state.next_thinking_seq += 1
    state.thinking_history.append({
        "seq": seq,
        "content": content,
        "timestamp": time.monotonic(),
    })
    return seq


# ---------------------------------------------------------------------------
# Query helpers (for /tools, /tasks, /bg, /show, /todos)
# ---------------------------------------------------------------------------

def get_active_tools_summary(state: State) -> dict[str, Any]:
    """Structured data for ``/tools`` command."""
    now = time.monotonic()
    tools = []
    for tu_id, info in state.active_tools.items():
        elapsed = now - info.get("started_at", now)
        tools.append({
            "tool_use_id": tu_id[:8],
            "seq": info.get("seq"),
            "name": info.get("name", "?"),
            "input": info.get("input", {}),
            "elapsed": fmt_duration(elapsed),
            "is_background": bool((info.get("input") or {}).get("run_in_background")),
        })

    bg_tasks = []
    for tid, info in state.background_tasks.items():
        elapsed = now - info.get("started_at", now)
        bg_tasks.append({
            "task_id": tid[:8],
            "seq": info.get("seq"),
            "task_type": info.get("task_type", "?"),
            "name": info.get("name") or "(unnamed)",
            "elapsed": fmt_duration(elapsed),
        })

    return {"active_tools": tools, "background_tasks": bg_tasks}


def get_tasks_summary(state: State) -> list[dict[str, Any]]:
    """Structured data for ``/tasks`` — non-Bash tools this turn."""
    by_seq = {h["seq"]: h for h in state.tool_history}
    now = time.monotonic()
    result = []
    for seq in state.current_turn_tool_seqs:
        h = by_seq.get(seq)
        if h is None:
            result.append({"seq": seq, "evicted": True})
            continue
        ended = h.get("ended_at")
        is_err = h.get("is_error")
        if ended is None:
            elapsed = now - h.get("started_at", now)
            status = "running"
            duration = fmt_duration(elapsed)
        elif is_err:
            status = "error"
            duration = fmt_duration(ended - h.get("started_at", ended))
        else:
            status = "complete"
            duration = fmt_duration(ended - h.get("started_at", ended))
        result.append({
            "seq": seq,
            "name": h.get("name", "?"),
            "input": h.get("input", {}),
            "status": status,
            "duration": duration,
            "is_error": is_err or False,
            "header": format_tool_header(h.get("name", "?"), h.get("input", {})),
        })
    return result


def get_bg_tasks_summary(state: State) -> list[dict[str, Any]]:
    """Structured data for ``/bg`` command."""
    now = time.monotonic()
    # Merge running tasks + completed-this-turn tasks.
    all_entries: dict[str, dict[str, Any]] = {}
    all_entries.update(state.current_turn_bg)
    all_entries.update(state.background_tasks)  # running overrides completed

    result = []
    for tid, info in sorted(all_entries.items(), key=lambda x: x[1].get("seq", 0)):
        ended = info.get("ended_at")
        if ended is not None:
            status = info.get("status", "completed")
            duration = fmt_duration(ended - info.get("started_at", ended))
        else:
            status = "running"
            duration = fmt_duration(now - info.get("started_at", now))
        result.append({
            "task_id": tid[:8],
            "task_id_full": tid,
            "seq": info.get("seq"),
            "name": info.get("name") or "(unnamed)",
            "task_type": info.get("task_type", "?"),
            "status": status,
            "duration": duration,
            "summary": info.get("summary"),
        })
    return result


def get_todos(state: State) -> list[dict[str, Any]]:
    """Structured data for ``/todos`` / ``/plan`` command."""
    if not state.current_todos:
        return []
    todos = []
    for t in state.current_todos:
        todos.append({
            "content": t.get("content", ""),
            "status": t.get("status", "pending"),
            "activeForm": t.get("activeForm"),
        })
    return todos


# ---------------------------------------------------------------------------
# /show detail — tool, bg, thinking lookups
# ---------------------------------------------------------------------------

def _parse_show_tokens(tokens: list[str]) -> tuple[list[tuple[str, int, int | None]], str | None]:
    """Parse ``/show`` arguments into ``[(letter, seq, tail), ...]``.

    Returns ``(entries, error_msg)``.  ``error_msg`` is ``None`` on success.
    """
    entries: list[tuple[str, int, int | None]] = []
    i = 0
    pending_tail: int | None = None
    while i < len(tokens):
        tok = tokens[i]
        # -tail K or legacy -K
        if tok == "-tail" and i + 1 < len(tokens):
            try:
                pending_tail = int(tokens[i + 1])
            except ValueError:
                return [], f"-tail must be followed by a number, got '{tokens[i + 1]}'"
            i += 2
            # Attach tail to last entry if present.
            if entries:
                letter, seq, _ = entries[-1]
                entries[-1] = (letter, seq, pending_tail)
                pending_tail = None
            continue
        if tok.startswith("-") and tok[1:].isdigit():
            pending_tail = int(tok[1:])
            if entries:
                letter, seq, _ = entries[-1]
                entries[-1] = (letter, seq, pending_tail)
                pending_tail = None
            i += 1
            continue
        # tN, bN, kN, or bare N (defaults to t)
        letter = "t"
        num_part = tok
        if tok and tok[0] in ("t", "b", "k") and tok[1:].isdigit():
            letter = tok[0]
            num_part = tok[1:]
        elif tok.isdigit():
            pass
        else:
            return [], f"unrecognised token '{tok}' (expected tN, bN, kN, or N)"
        try:
            seq = int(num_part)
        except ValueError:
            return [], f"unrecognised token '{tok}'"
        entries.append((letter, seq, None))
        i += 1
    return entries, None


def get_tool_detail(state: State, payload: str) -> dict[str, Any]:
    """Produce structured data for ``/show`` command.

    Returns a dict with ``type: "show_result"`` containing the requested
    detail entries.
    """
    payload = payload.strip()
    if not payload:
        return _get_recent_each_type(state)

    entries, err = _parse_show_tokens(payload.split())
    if err is not None:
        return {"type": "show_result", "error": err}

    tool_by_seq = {e["seq"]: e for e in state.tool_history}
    think_by_seq = {e["seq"]: e for e in state.thinking_history}
    bg_index = _bg_entry_index(state)

    results: list[dict[str, Any]] = []
    for letter, seq, tail in entries:
        if letter == "t":
            entry = tool_by_seq.get(seq)
            if entry is None:
                results.append({
                    "letter": "t", "seq": seq,
                    "error": f"#t{seq} not found (history keeps last {state.tool_history.maxlen})",
                })
                continue
            results.append(_tool_detail(entry, tail))
        elif letter == "b":
            match = bg_index.get(seq)
            if match is None:
                results.append({"letter": "b", "seq": seq, "error": f"#b{seq} not found"})
                continue
            results.append(_bg_detail(match, tail))
        elif letter == "k":
            entry = think_by_seq.get(seq)
            if entry is None:
                results.append({
                    "letter": "k", "seq": seq,
                    "error": f"#k{seq} not found (history keeps last {state.thinking_history.maxlen})",
                })
                continue
            results.append(_thinking_detail(entry))

    return {"type": "show_result", "entries": results}


def _get_recent_each_type(state: State) -> dict[str, Any]:
    """Show the most recent entries of each type (tool, bg, thinking)."""
    recent_tools = []
    for h in list(state.tool_history)[-5:]:
        recent_tools.append(_tool_detail(h, tail=None))

    recent_bg = []
    bg_index = _bg_entry_index(state)
    for seq in sorted(bg_index.keys())[-5:]:
        recent_bg.append(_bg_detail(bg_index[seq], tail=None))

    recent_thinking = []
    for h in list(state.thinking_history)[-3:]:
        recent_thinking.append(_thinking_detail(h))

    return {
        "type": "show_result",
        "recent": True,
        "tools": recent_tools,
        "bg_tasks": recent_bg,
        "thinking": recent_thinking,
    }


def _tool_detail(entry: dict[str, Any], tail: int | None) -> dict[str, Any]:
    """Full detail for one tool history entry."""
    result_text = entry.get("result_text") or ""
    if tail is not None and tail > 0:
        lines = result_text.splitlines()
        result_text = "\n".join(lines[-tail:])

    started = entry.get("started_at")
    ended = entry.get("ended_at")
    duration = fmt_duration(ended - started) if started and ended else None

    return {
        "letter": "t",
        "seq": entry["seq"],
        "name": entry.get("name", "?"),
        "input": entry.get("input", {}),
        "header": format_tool_header(entry.get("name", "?"), entry.get("input", {})),
        "status": (
            "error" if entry.get("is_error")
            else "complete" if ended
            else "running"
        ),
        "result_text": result_text,
        "result_size": humanize_size(entry.get("result_text") or ""),
        "is_error": entry.get("is_error") or False,
        "duration": duration,
        "tail": tail,
    }


def _bg_detail(match: tuple[str, dict[str, Any]], tail: int | None) -> dict[str, Any]:
    """Full detail for one background task."""
    task_id, info = match
    started = info.get("started_at")
    ended = info.get("ended_at")
    duration = fmt_duration(ended - started) if started and ended else None
    now = time.monotonic()

    return {
        "letter": "b",
        "seq": info.get("seq"),
        "task_id": task_id[:8],
        "task_id_full": task_id,
        "name": info.get("name") or "(unnamed)",
        "task_type": info.get("task_type", "?"),
        "tool_use_id": info.get("tool_use_id"),
        "status": info.get("status") or ("running" if ended is None else "completed"),
        "duration": duration or fmt_duration(now - (started or now)),
        "summary": info.get("summary"),
    }


def _thinking_detail(entry: dict[str, Any]) -> dict[str, Any]:
    """Full detail for one thinking block."""
    return {
        "letter": "k",
        "seq": entry["seq"],
        "content": entry.get("content", ""),
    }


def _bg_entry_index(state: State) -> dict[int, tuple[str, dict[str, Any]]]:
    """Build ``{seq: (task_id, info)}`` from running + this-turn bg tasks."""
    index: dict[int, tuple[str, dict[str, Any]]] = {}
    for tid, info in state.current_turn_bg.items():
        seq = info.get("seq")
        if seq is not None:
            index[seq] = (tid, info)
    for tid, info in state.background_tasks.items():
        seq = info.get("seq")
        if seq is not None:
            index[seq] = (tid, info)
    return index


# ---------------------------------------------------------------------------
# Summarise tool result text (for history storage)
# ---------------------------------------------------------------------------

def summarize_tool_result(result_content: Any) -> str:
    """Extract and truncate tool result text from an SDK ToolResultBlock.

    Accepts either an SDK block (with ``.content`` / ``.text``) or a
    plain string.
    """
    if isinstance(result_content, str):
        text = result_content
    elif hasattr(result_content, "content"):
        content = result_content.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, str):
                    parts.append(c)
                elif hasattr(c, "text"):
                    parts.append(c.text)
            text = "\n".join(parts)
        else:
            text = str(content)
    else:
        text = str(result_content)
    # Strip trailing whitespace but preserve leading (may be code).
    return text.rstrip()
