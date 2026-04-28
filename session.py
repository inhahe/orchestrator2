"""Session management — discovery, JSONL parsing, history, trim, export.

All session I/O goes through this module: listing projects/sessions,
reading/writing titles, rendering history for the frontend, rolling-
window trim, and markdown export.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid as uuid_mod
from datetime import datetime
from pathlib import Path
from typing import Any

from tool_manager import format_tool_header

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRIM_MARKER_TYPE = "orch-trim-metadata"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _humanize_size(text: str) -> str:
    """Return a human-readable size hint like ``42 lines, 1.2k chars``."""
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    if lines == 0 and text:
        lines = 1
    chars = len(text)
    char_str = f"{chars / 1000:.1f}k chars" if chars >= 1000 else f"{chars} chars"
    return f"{lines} line{'s' if lines != 1 else ''}, {char_str}"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def claude_projects_dir() -> Path:
    """Return the Claude Code projects directory (``~/.claude/projects/``)."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base if base else Path.home() / ".claude") / "projects"


def _sanitize_cwd(cwd: str) -> str:
    """Match Claude Code's ``sanitizePath``: any non-alphanumeric → ``-``."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _normalize_path_for_compare(p: str) -> str:
    """Normalise paths for comparison (backslash → slash, lowercase on Win)."""
    s = p.replace("\\", "/").rstrip("/")
    if sys.platform == "win32":
        s = s.lower()
    return s


# ---------------------------------------------------------------------------
# Project / session discovery
# ---------------------------------------------------------------------------

def project_dir_for_cwd(cwd: str) -> Path:
    """Return the on-disk project directory for *cwd*."""
    try:
        resolved = str(Path(cwd).resolve(strict=False))
    except OSError:
        resolved = cwd
    return claude_projects_dir() / _sanitize_cwd(resolved)


def find_project_for_cwd(cwd: str) -> Path | None:
    """Locate an existing project directory for *cwd*.

    Tries the direct sanitise-and-check path first; falls back to
    scanning every project dir for one whose first-record ``cwd``
    matches (case-insensitive on Windows).
    """
    direct = project_dir_for_cwd(cwd)
    if direct.exists():
        return direct
    try:
        target = str(Path(cwd).resolve(strict=False))
    except OSError:
        target = cwd
    target_norm = _normalize_path_for_compare(target)
    projects = claude_projects_dir()
    if not projects.exists():
        return None
    for project in projects.iterdir():
        if not project.is_dir():
            continue
        for jsonl in project.glob("*.jsonl"):
            stored = sniff_session_cwd(jsonl)
            if stored and _normalize_path_for_compare(stored) == target_norm:
                return project
            break  # only check one jsonl per project
    return None


def find_most_recent_session_for_cwd(cwd: str) -> Path | None:
    """Return the most-recently-modified ``.jsonl`` in the project for *cwd*."""
    project = project_dir_for_cwd(cwd)
    if not project.exists():
        return None
    candidates: list[tuple[Path, float]] = []
    for jsonl in project.glob("*.jsonl"):
        try:
            candidates.append((jsonl, jsonl.stat().st_mtime))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])[0]


def find_session_dir(session_id: str) -> Path | None:
    """Locate the project dir that contains ``<session_id>.jsonl``."""
    projects = claude_projects_dir()
    if not projects.exists():
        return None
    for project in projects.iterdir():
        if project.is_dir() and (project / f"{session_id}.jsonl").exists():
            return project
    return None


# ---------------------------------------------------------------------------
# Session CWD sniffing
# ---------------------------------------------------------------------------

def sniff_session_cwd(jsonl: Path) -> str | None:
    """Read the first few records and return the first ``cwd`` field."""
    try:
        with jsonl.open(encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
                    return rec["cwd"]
    except OSError:
        pass
    return None


def find_session_cwd(session_id: str) -> str | None:
    """Locate a session by id and return its recorded cwd."""
    project_dir = find_session_dir(session_id)
    if project_dir is None:
        return None
    info = _parse_session_info(
        project_dir / f"{session_id}.jsonl", project_dir.name
    )
    return info.get("cwd") if info else None


# ---------------------------------------------------------------------------
# Title read / write
# ---------------------------------------------------------------------------

def read_session_title(session_id: str) -> str | None:
    """Look up a session's display title from JSONL (custom-title or ai-title)."""
    project = find_session_dir(session_id)
    if project is None:
        return None
    jsonl = project / f"{session_id}.jsonl"
    if not jsonl.exists():
        return None
    custom_title: str | None = None
    ai_title: str | None = None
    try:
        with jsonl.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                t = rec.get("type")
                if t == "custom-title":
                    v = rec.get("customTitle")
                    if isinstance(v, str) and v.strip():
                        custom_title = v.strip()
                elif t == "ai-title":
                    v = rec.get("aiTitle")
                    if isinstance(v, str) and v.strip():
                        ai_title = v.strip()
    except OSError:
        return None
    return custom_title or ai_title


def write_session_title(session_id: str, title: str) -> None:
    """Set a session's title by appending a ``custom-title`` record.

    Attempts to use ``claude_agent_sdk.rename_session`` first; falls back
    to hand-rolled JSONL append.
    """
    try:
        from claude_agent_sdk import rename_session as _sdk_rename  # type: ignore
        _sdk_rename(session_id, title)
        return
    except (ImportError, AttributeError):
        pass
    project = find_session_dir(session_id)
    if project is None:
        raise OSError(f"session {session_id} not found on disk")
    jsonl = project / f"{session_id}.jsonl"
    if not jsonl.exists():
        raise OSError(f"session jsonl missing: {jsonl}")
    record = {
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Session info parsing
# ---------------------------------------------------------------------------

def _extract_text(content: Any) -> str:
    """Pull plain text from a message-content field (str or list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _parse_session_info(jsonl: Path, project_slug: str) -> dict[str, Any] | None:
    """Read a session JSONL and extract id, cwd, messages, timestamp, title."""
    try:
        st = jsonl.stat()
    except OSError:
        return None
    info: dict[str, Any] = {
        "session_id": jsonl.stem,
        "project_slug": project_slug,
        "cwd": None,
        "first_user_msg": None,
        "last_user_msg": None,
        "last_timestamp": None,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "msg_count": 0,
        "title": None,
    }
    info["title"] = read_session_title(jsonl.stem)
    try:
        with jsonl.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                info["msg_count"] += 1
                if info["cwd"] is None and isinstance(rec.get("cwd"), str):
                    info["cwd"] = rec["cwd"]
                ts = rec.get("timestamp")
                if isinstance(ts, str):
                    info["last_timestamp"] = ts
                if rec.get("type") == "user":
                    msg = rec.get("message")
                    text = ""
                    if isinstance(msg, dict):
                        text = _extract_text(msg.get("content"))
                    elif isinstance(msg, str):
                        text = msg
                    text = text.strip()
                    if text and not text.startswith("<bash") and not text.startswith("<tool"):
                        if info["first_user_msg"] is None:
                            info["first_user_msg"] = text
                        info["last_user_msg"] = text
    except OSError:
        return None
    return info


# ---------------------------------------------------------------------------
# Trim marker helpers
# ---------------------------------------------------------------------------

def _is_trim_session_file(jsonl_path: Path) -> dict[str, Any] | None:
    """If this ``.jsonl`` starts with our trim marker, return the marker dict."""
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            first = f.readline().strip()
    except OSError:
        return None
    if not first:
        return None
    try:
        obj = json.loads(first)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and obj.get("type") == _TRIM_MARKER_TYPE:
        return obj
    return None


# ---------------------------------------------------------------------------
# Listing projects and sessions
# ---------------------------------------------------------------------------

def list_projects() -> list[dict[str, Any]]:
    """Light-weight project listing (for session picker / REST endpoint)."""
    projects_dir = claude_projects_dir()
    if not projects_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for project in projects_dir.iterdir():
        if not project.is_dir():
            continue
        jsonls = list(project.glob("*.jsonl"))
        if not jsonls:
            continue
        try:
            stats = [(j, j.stat().st_mtime) for j in jsonls]
        except OSError:
            continue
        newest_mtime = max(m for _, m in stats)
        cwd: str | None = None
        try:
            newest_jsonl = max(stats, key=lambda x: x[1])[0]
            cwd = sniff_session_cwd(newest_jsonl)
        except OSError:
            pass
        out.append({
            "project_dir": str(project),
            "project_slug": project.name,
            "cwd": cwd,
            "session_count": len(jsonls),
            "newest_mtime": newest_mtime,
        })
    out.sort(key=lambda p: p["newest_mtime"], reverse=True)
    return out


def list_sessions(filter_cwd: str | None = None) -> list[dict[str, Any]]:
    """Return all sessions on disk, newest first.

    If *filter_cwd* is given, only include sessions whose project dir matches.
    """
    projects = claude_projects_dir()
    if not projects.exists():
        return []
    sessions: list[dict[str, Any]] = []
    for project in projects.iterdir():
        if not project.is_dir():
            continue
        for jsonl in project.glob("*.jsonl"):
            if _is_trim_session_file(jsonl) is not None:
                continue
            info = _parse_session_info(jsonl, project.name)
            if info is None:
                continue
            if filter_cwd is not None and info.get("cwd") != filter_cwd:
                continue
            sessions.append(info)
    sessions.sort(key=lambda s: s.get("mtime", 0.0), reverse=True)
    return sessions


def list_sessions_for_project(project_dir: Path) -> list[dict[str, Any]]:
    """Parse all sessions in a single project dir, newest first."""
    sessions: list[dict[str, Any]] = []
    for jsonl in project_dir.glob("*.jsonl"):
        if _is_trim_session_file(jsonl) is not None:
            continue
        info = _parse_session_info(jsonl, project_dir.name)
        if info is not None:
            sessions.append(info)
    sessions.sort(key=lambda s: s.get("mtime", 0.0), reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# Session history — structured for the web frontend
# ---------------------------------------------------------------------------

def render_session_history(
    jsonl: Path,
    *,
    show_tool_output: bool = False,
) -> tuple[int, list[dict[str, Any]], list[str]]:
    """Build structured history for the frontend.

    Returns ``(message_count, messages, orphan_bg_tool_ids)``.

    Messages are dicts with a ``type`` key the frontend can route.
    Orphan bg tool ids are background-Bash that started but never got a
    matching completion notification.

    **Tool-block contract** (applies to both history and live messages):

    ``tool_use`` message fields:
        - ``tool_use_id``  — links to the matching ``tool_result``
        - ``name``         — tool name (``"Bash"``, ``"Read"``, etc.)
        - ``input``        — full input dict (``command``, ``file_path``, …)
        - ``status``       — ``"running"`` (live) | ``"complete"`` | ``"error"``
        - ``result``       — present only when status != ``"running"``;
                             holds ``{content, is_error, size_hint}``
        - ``is_history``   — ``True`` for replayed transcript records

    For live execution the backend sends ``tool_use`` with
    ``status="running"`` first, then a separate ``tool_result`` message
    when it finishes.  The frontend updates the existing block in-place.

    For history replay (this function) we do a **two-pass** approach:
    first collect all tool_results keyed by tool_use_id, then emit
    tool_use blocks with the result already attached and the correct
    status.  This lets the frontend render every historical tool block
    as already-complete with the right ✓/✗ indicator and expandable
    output.
    """
    rendered = 0
    bg_started: dict[str, str] = {}
    notif_re = re.compile(
        r"<task-notification>.*?<tool-use-id>([^<]+)</tool-use-id>",
        re.DOTALL,
    )

    # --- First pass: slurp records and index tool_results by tool_use_id ---
    records: list[dict[str, Any]] = []
    tool_results_by_id: dict[str, dict[str, Any]] = {}

    try:
        f = jsonl.open(encoding="utf-8", errors="replace")
    except OSError as e:
        return 0, [{
            "type": "system",
            "subtype": "error",
            "content": f"Failed to open {jsonl.name}: {e}",
            "is_history": True,
        }], []

    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            records.append(rec)

            # Index tool_result blocks from user messages so we can
            # pair them with their tool_use in the second pass.
            if rec.get("type") == "user":
                msg = rec.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "tool_result"
                                and block.get("tool_use_id")
                            ):
                                inner = block.get("content")
                                text = (
                                    inner if isinstance(inner, str)
                                    else _extract_text(inner)
                                )
                                text = (text or "").strip()
                                tool_results_by_id[block["tool_use_id"]] = {
                                    "content": text,
                                    "is_error": bool(block.get("is_error")),
                                    "size_hint": _humanize_size(text),
                                }

    # --- Second pass: emit structured messages with results attached ---
    messages: list[dict[str, Any]] = []

    for rec in records:
        t = rec.get("type")
        msg = rec.get("message")

        # --- User messages ---
        if t == "user" and isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                text = content.strip()
                has_perm_mode = "permissionMode" in rec
                looks_synthetic = (
                    text.startswith("<bash")
                    or text.startswith("<tool")
                    or text.startswith("<task-notification")
                    or text.startswith("<local-command")
                )
                if has_perm_mode and not looks_synthetic:
                    messages.append({
                        "type": "user",
                        "content": text,
                        "is_history": True,
                    })
                    rendered += 1
                if text.startswith("<task-notification"):
                    m = notif_re.search(text)
                    if m:
                        bg_started.pop(m.group(1), None)

            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    # tool_result blocks are NOT emitted separately —
                    # they're already attached to their tool_use above.
                    # Only emit standalone text blocks.
                    if bt == "text" and isinstance(block.get("text"), str):
                        text = block["text"].strip()
                        if text:
                            messages.append({
                                "type": "user",
                                "content": text,
                                "is_history": True,
                            })
                            rendered += 1

        # --- Assistant messages ---
        elif t == "assistant" and isinstance(msg, dict):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        messages.append({
                            "type": "assistant",
                            "content": text,
                            "is_history": True,
                        })
                elif bt == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input") or {}
                    tool_use_id = block.get("id")
                    # Pair with the indexed result (if any).
                    result_info = tool_results_by_id.get(tool_use_id)
                    if result_info:
                        status = "error" if result_info["is_error"] else "complete"
                    else:
                        status = "complete"  # transcript is finished
                    tool_msg: dict[str, Any] = {
                        "type": "tool_use",
                        "name": name,
                        "input": inp,
                        "tool_use_id": tool_use_id,
                        "status": status,
                        "header": format_tool_header(name, inp),
                        "is_history": True,
                    }
                    if result_info:
                        tool_msg["result"] = result_info
                    messages.append(tool_msg)
                    # Track bg Bash starts for orphan detection.
                    if (
                        name == "Bash"
                        and isinstance(inp, dict)
                        and inp.get("run_in_background")
                    ):
                        tid = block.get("id")
                        if tid:
                            cmd = (inp.get("command") or "").splitlines()
                            head = (cmd[0] if cmd else "")[:60]
                            bg_started[tid] = head
                elif bt == "thinking":
                    thinking_text = (block.get("thinking") or "").strip()
                    if thinking_text:
                        messages.append({
                            "type": "thinking",
                            "content": thinking_text,
                            "is_history": True,
                        })
            rendered += 1

    orphan_cmds = [f"{tid[:8]}: {cmd}" for tid, cmd in bg_started.items()]
    return rendered, messages, orphan_cmds


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def render_session_markdown(jsonl: Path) -> str:
    """Convert a session JSONL transcript into readable markdown."""
    metadata: dict[str, Any] = {
        "session_id": jsonl.stem,
        "cwd": None,
        "first_ts": None,
        "last_ts": None,
        "title": read_session_title(jsonl.stem),
    }
    body: list[str] = []

    def _ts(rec: dict[str, Any]) -> str:
        ts = rec.get("timestamp")
        if isinstance(ts, str):
            if metadata["first_ts"] is None:
                metadata["first_ts"] = ts
            metadata["last_ts"] = ts
            return f" — _{ts}_"
        return ""

    with jsonl.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if metadata["cwd"] is None and isinstance(rec.get("cwd"), str):
                metadata["cwd"] = rec["cwd"]
            t = rec.get("type")
            msg = rec.get("message")
            ts_suffix = _ts(rec)

            if t == "user" and isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    body.append(f"\n## You{ts_suffix}\n\n{content.strip()}\n")
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type")
                        if bt == "tool_result":
                            inner = block.get("content")
                            text = (
                                inner if isinstance(inner, str)
                                else _extract_text(inner)
                            )
                            text = (text or "").strip() or "(empty)"
                            body.append(
                                f"\n**Result:**\n\n```\n{text}\n```\n"
                            )
                        elif bt == "text" and isinstance(block.get("text"), str):
                            body.append(
                                f"\n## You{ts_suffix}\n\n{block['text'].strip()}\n"
                            )

            elif t == "assistant" and isinstance(msg, dict):
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                section_started = False
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text":
                        text = (block.get("text") or "").strip()
                        if not text:
                            continue
                        if not section_started:
                            body.append(f"\n## Claude{ts_suffix}\n")
                            section_started = True
                        body.append(f"\n{text}\n")
                    elif bt == "tool_use":
                        name = block.get("name", "?")
                        inp = block.get("input", {})
                        if not section_started:
                            body.append(f"\n## Claude{ts_suffix}\n")
                            section_started = True
                        try:
                            inp_text = json.dumps(inp, indent=2, default=str)
                        except (TypeError, ValueError):
                            inp_text = str(inp)
                        body.append(
                            f"\n**Tool: `{name}`**\n\n```json\n{inp_text}\n```\n"
                        )

    header = ["# Claude Conversation\n"]
    if metadata["title"]:
        header.append(f"- **Title:** {metadata['title']}")
    header.append(f"- **Session:** `{metadata['session_id']}`")
    if metadata["cwd"]:
        header.append(f"- **Project:** `{metadata['cwd']}`")
    if metadata["first_ts"]:
        header.append(f"- **Started:** {metadata['first_ts']}")
    if metadata["last_ts"]:
        header.append(f"- **Last activity:** {metadata['last_ts']}")
    return "\n".join(header) + "\n\n---\n" + "".join(body)


# ---------------------------------------------------------------------------
# Rolling-window trim
# ---------------------------------------------------------------------------

def _is_user_turn_start(record: dict[str, Any]) -> bool:
    """True iff this record marks the beginning of a new user turn.

    These are the only safe cut points for rolling-window trim: slicing
    mid-turn would leave tool_use blocks orphaned.
    """
    if record.get("type") != "user" or record.get("isSidechain"):
        return False
    msg = record.get("message") or {}
    content = msg.get("content", "")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(c, dict) and c.get("type") == "tool_result"
            for c in content
        )
    return False


def _rough_tokens(record: dict[str, Any]) -> int:
    """Cheap upper-bound token estimate (chars/4)."""
    msg = record.get("message")
    if not isinstance(msg, dict):
        return 0
    content = msg.get("content", "")
    if isinstance(content, str):
        return max(1, len(content) // 4)
    if isinstance(content, list):
        total = 0
        for c in content:
            try:
                total += len(json.dumps(c, default=str)) // 4
            except (TypeError, ValueError):
                total += 10
        return max(1, total)
    return 0


def trim_session(
    src_session_id: str,
    project_dir: Path,
    target_tokens: int,
) -> str | None:
    """Fork the tail of a session into a new JSONL at or below *target_tokens*.

    Returns the new session UUID, or ``None`` if no trim was needed.
    """
    src_path = project_dir / f"{src_session_id}.jsonl"
    if not src_path.exists():
        return None
    src_marker = _is_trim_session_file(src_path)
    root_session_id = (
        src_marker.get("rootSessionId") if src_marker else src_session_id
    )

    records: list[dict[str, Any]] = []
    try:
        with open(src_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == _TRIM_MARKER_TYPE:
                    continue
                records.append(obj)
    except OSError:
        return None
    if not records:
        return None

    transcript = [r for r in records if not r.get("isSidechain")]
    turn_starts = [i for i, r in enumerate(transcript) if _is_user_turn_start(r)]
    if len(turn_starts) < 2:
        return None

    total_tokens = sum(_rough_tokens(r) for r in transcript)
    if total_tokens <= target_tokens:
        return None

    cut_idx: int | None = None
    for ts in reversed(turn_starts):
        tail = sum(_rough_tokens(transcript[i]) for i in range(ts, len(transcript)))
        if tail <= target_tokens:
            cut_idx = ts
        else:
            break
    if cut_idx is None:
        cut_idx = turn_starts[-1]

    kept = transcript[cut_idx:]
    if not kept:
        return None

    new_session_id = str(uuid_mod.uuid4())
    uuid_map: dict[str, str] = {}
    for r in kept:
        if "uuid" in r:
            uuid_map[r["uuid"]] = str(uuid_mod.uuid4())

    output_lines: list[str] = []
    output_lines.append(
        json.dumps(
            {
                "type": _TRIM_MARKER_TYPE,
                "sessionId": new_session_id,
                "previousSessionId": src_session_id,
                "rootSessionId": root_session_id,
                "createdAt": time.time(),
            },
            separators=(",", ":"),
        )
    )
    for meta in records:
        if meta.get("type") == "permission-mode":
            m = dict(meta)
            m["sessionId"] = new_session_id
            output_lines.append(json.dumps(m, separators=(",", ":")))
            break

    prev_new_uuid: str | None = None
    for i, r in enumerate(kept):
        new = dict(r)
        orig_uuid = r.get("uuid")
        if orig_uuid and orig_uuid in uuid_map:
            new["uuid"] = uuid_map[orig_uuid]
        orig_parent = r.get("parentUuid")
        if i == 0:
            new["parentUuid"] = None
        elif orig_parent and orig_parent in uuid_map:
            new["parentUuid"] = uuid_map[orig_parent]
        else:
            new["parentUuid"] = prev_new_uuid
        new["sessionId"] = new_session_id
        new["isSidechain"] = False
        for key in ("forkedFrom", "logicalParentUuid"):
            new.pop(key, None)
        output_lines.append(json.dumps(new, separators=(",", ":")))
        prev_new_uuid = new.get("uuid") or prev_new_uuid

    dest = project_dir / f"{new_session_id}.jsonl"
    try:
        with open(dest, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines) + "\n")
    except OSError:
        return None

    if src_marker is not None:
        try:
            src_path.unlink()
        except OSError:
            pass
    return new_session_id
