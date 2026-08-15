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
import threading
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

def claude_projects_dir(config_dir: str | None = None) -> Path:
    """Return the Claude Code projects directory (``~/.claude/projects/``).

    *config_dir* explicitly overrides the account (thread-safe, no env
    mutation) — used when loading a cross-account hub session's history.
    Falls back to ``CLAUDE_CONFIG_DIR`` then ``~/.claude``.
    """
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base if base else Path.home() / ".claude") / "projects"


def _sanitize_cwd(cwd: str) -> str:
    """Match Claude Code's ``sanitizePath``: any non-alphanumeric → ``-``."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


# ---------------------------------------------------------------------------
# Queued-prompt persistence
#
# The pending-prompt queue lives in memory (State.queued_prompts); we mirror
# it to a small JSON file so it survives a server restart.  It's stored under
# the active config dir (account-scoped, like sessions) but *outside* the
# ``projects/`` tree so it can never be mistaken for Claude session data.
# ---------------------------------------------------------------------------

def _orch2_state_dir() -> Path:
    """Return ``<config-dir>/orchestrator2`` (config-dir = CLAUDE_CONFIG_DIR or ~/.claude)."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else Path.home() / ".claude"
    return root / "orchestrator2"


def queue_file_for_cwd(cwd: str) -> Path:
    """Path to the persisted queue file for *cwd* (account + cwd scoped)."""
    try:
        resolved = str(Path(cwd).resolve(strict=False))
    except OSError:
        resolved = cwd
    return _orch2_state_dir() / "queues" / f"{_sanitize_cwd(resolved)}.json"


def load_persisted_queue(cwd: str) -> list[str]:
    """Load the persisted pending-prompt queue for *cwd* (empty on any error)."""
    path = queue_file_for_cwd(cwd)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    items = data.get("queue") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, str)]


def save_persisted_queue(cwd: str, items: Any) -> None:
    """Atomically write the pending-prompt queue for *cwd* to disk.

    When *items* is empty the file is removed so stale empties don't linger.
    Failures are swallowed — persistence must never break queue operations.
    """
    path = queue_file_for_cwd(cwd)
    lst = [str(x) for x in items]
    try:
        if not lst:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cwd": cwd, "queue": lst, "saved_at": time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


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


def find_session_dir(session_id: str, config_dir: str | None = None) -> Path | None:
    """Locate the project dir that contains ``<session_id>.jsonl``.

    *config_dir* scopes the search to a specific account's projects tree
    (for cross-account hub sessions); defaults to the active env account.
    """
    projects = claude_projects_dir(config_dir)
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


def find_session_cwd(session_id: str, config_dir: str | None = None) -> str | None:
    """Locate a session by id and return its recorded cwd."""
    project_dir = find_session_dir(session_id, config_dir)
    if project_dir is None:
        return None
    info = _parse_session_info(
        project_dir / f"{session_id}.jsonl", project_dir.name
    )
    return info.get("cwd") if info else None


# ---------------------------------------------------------------------------
# Title read / write
# ---------------------------------------------------------------------------

# --- Persistent, incremental title index ----------------------------------
# Reading a session's title means finding the last ``custom-title`` (user
# rename) / ``ai-title`` (auto summary) record.  A rename done early then
# followed by heavy use buries the custom-title deep in a file that can be
# hundreds of MB, so we can't just peek at the head/tail — correctness needs a
# full scan.  Doing that for every session on every lobby refresh froze the UI
# for many seconds.
#
# Session JSONLs are append-only, so the fix is to scan each file's titles once,
# remember (title, scanned-size) in a small on-disk index, and thereafter scan
# only the bytes appended since the last look — which is exactly where a *new*
# rename would land.  The first run after a fresh install pays the full scan
# once; every run after that is near-instant.
_TITLE_INDEX_PATH = Path.home() / ".orchestrator2_title_index.json"
_title_index: dict[str, dict[str, Any]] | None = None
_title_index_lock = threading.Lock()
_title_index_dirty = False


def _load_title_index() -> dict[str, dict[str, Any]]:
    """Lazily load the on-disk title index (path → {size, mtime, custom, ai})."""
    global _title_index
    if _title_index is not None:
        return _title_index
    try:
        with _TITLE_INDEX_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        _title_index = (
            {k: v for k, v in data.items() if isinstance(v, dict)}
            if isinstance(data, dict) else {}
        )
    except (OSError, ValueError):
        _title_index = {}
    return _title_index


def flush_title_index() -> None:
    """Persist the title index if it changed (atomic replace)."""
    global _title_index_dirty
    with _title_index_lock:
        if not _title_index_dirty or _title_index is None:
            return
        snapshot = dict(_title_index)
        _title_index_dirty = False
    try:
        tmp = _TITLE_INDEX_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, _TITLE_INDEX_PATH)
    except OSError:
        # Couldn't persist — keep the in-memory index; retry on next flush.
        with _title_index_lock:
            _title_index_dirty = True


def _scan_titles(jsonl: Path, start: int) -> tuple[str | None, str | None]:
    """Scan records from byte *start* to EOF; return the last (custom, ai) found.

    *start* > 0 does an incremental scan of only the appended region.  We seek to
    the exact previous size (a clean line boundary for append-only writes); any
    partial straddling record simply fails to parse and is skipped.
    """
    custom: str | None = None
    ai: str | None = None
    try:
        with jsonl.open("rb") as fb:
            if start > 0:
                fb.seek(start)
            for raw in fb:
                s = raw.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                t = rec.get("type")
                if t == "custom-title":
                    v = rec.get("customTitle")
                    if isinstance(v, str) and v.strip():
                        custom = v.strip()
                elif t == "ai-title":
                    v = rec.get("aiTitle")
                    if isinstance(v, str) and v.strip():
                        ai = v.strip()
    except OSError:
        pass
    return custom, ai


# --- Sticky user renames ---------------------------------------------------
# A rename is a statement of user intent, but the session JSONL is not a
# reliable place to keep it, because we are not its only writer.
#
# The Claude Code CLI holds the title in memory (``currentSessionTitle``) and
# re-appends it at EOF from ``reAppendSessionMetadata()`` — on *every*
# compaction and again when a session is resumed.  It notices a rename written
# by anyone else only if that record is still inside the last **64 KiB** of the
# file when it next looks (``readFileTailSync`` / ``LITE_READ_BUF_SIZE``, and
# the resume path stamps with ``skipTitleRefresh`` so it doesn't look at all).
# On a busy session those 64 KiB are seconds wide, so a ``/rename`` usually
# scrolls out of the window before the CLI ever reads it — after which the CLI
# goes on re-stamping the *old* title at EOF forever, and since "last record
# wins" it out-votes us by orders of magnitude.
#
# Measured on the reporter's own 845 MB session: 933 ``custom-title 'OS'``
# records against 46 ``'OSc'``.  The rename at offset 794,639,953 was undone by
# a compaction stamp 94 KB later (i.e. just outside the 64 KiB window), and a
# second rename survived 45 compactions only to be reverted by the resume stamp.
#
# So the user's intent is kept on our side instead.  A rename records a *pin*:
# the title we set, plus every title we have previously seen or set for that
# file.  When resolving, a JSONL title matching one of those stale values means
# the CLI is re-stamping something the user already replaced, so the pin wins.
# A title we have never seen is a genuine third-party rename (e.g. ``/rename``
# typed inside the CLI itself) and it drops the pin, so we never fight a real
# newer intent.
#
# Known limitation: renaming *back* to a previously-used title from outside
# orchestrator2 looks identical to a stale re-stamp and will be overridden.
# The escape hatch is to rename from orchestrator2.
_RENAME_PIN_PATH = Path.home() / ".orchestrator2_renames.json"
_rename_pins: dict[str, dict[str, Any]] | None = None
_rename_pin_lock = threading.Lock()


def _load_rename_pins() -> dict[str, dict[str, Any]]:
    """Lazily load the pin table (jsonl path → {title, stale, at})."""
    global _rename_pins
    if _rename_pins is not None:
        return _rename_pins
    try:
        with _RENAME_PIN_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        _rename_pins = {
            k: v for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("title"), str)
        } if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _rename_pins = {}
    return _rename_pins


def _save_rename_pins(pins: dict[str, dict[str, Any]]) -> None:
    """Persist the pin table (atomic replace).

    Unlike the title *index* — a derived cache that can be rebuilt by rescanning
    — this file is the only record of a user's rename, so it is written through
    immediately rather than batched behind a dirty flag.
    """
    try:
        tmp = _RENAME_PIN_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(pins, f, indent=1)
        os.replace(tmp, _RENAME_PIN_PATH)
    except OSError:
        pass


def _pin_rename(jsonl: Path, title: str, prev: str | None) -> None:
    """Record that the user set *jsonl*'s title to *title*.

    *prev* is whatever the file said immediately before the rename — the value
    the CLI has cached and will keep re-stamping.  It joins the pin's ``stale``
    set along with any title an earlier pin had set, so a chain of renames
    (A → B → C) still recognises a stamp of A or B as obsolete.
    """
    key = str(jsonl)
    with _rename_pin_lock:
        pins = _load_rename_pins()
        old = pins.get(key) or {}
        stale = {s for s in old.get("stale", []) if isinstance(s, str)}
        if isinstance(old.get("title"), str):
            stale.add(old["title"])
        if prev:
            stale.add(prev)
        stale.discard(title)
        pins[key] = {"title": title, "stale": sorted(stale), "at": time.time()}
        # Sessions get deleted; without this the table would only ever grow.
        for dead in [k for k in pins if not os.path.exists(k)]:
            pins.pop(dead, None)
        _save_rename_pins(pins)


def _apply_rename_pin(key: str, custom: str | None) -> str | None:
    """Overlay the rename pin for *key* on the raw ``custom-title`` value."""
    pins = _load_rename_pins()
    pin = pins.get(key)
    if pin is None:
        return custom
    title = pin.get("title")
    if not isinstance(title, str):
        return custom
    if custom is None or custom == title or custom in set(pin.get("stale", [])):
        return title
    # A title we have neither written nor seen — someone renamed the session
    # for real (``/rename`` inside the CLI, another tool).  Yield to it and
    # forget the pin, so we don't keep overriding a fresher intent.
    with _rename_pin_lock:
        pins.pop(key, None)
        _save_rename_pins(pins)
    return custom


def _resolve_title_raw(jsonl: Path) -> tuple[str | None, str | None]:
    """The (custom, ai) titles actually recorded in *jsonl*, via the index.

    Full-scans a file the first time it's seen, then only re-scans the bytes
    appended since (which is where a new rename lands).  Account-correct by
    construction — it reads the exact file it's handed.

    This is the *raw* view: it deliberately ignores the rename pins applied by
    ``_resolve_title``, because pinning needs to know what the file itself says.
    """
    try:
        st = jsonl.stat()
    except OSError:
        return None, None
    size = st.st_size
    key = str(jsonl)
    idx = _load_title_index()
    hit = False
    with _title_index_lock:
        entry = idx.get(key)
        prev_size = 0
        prev_custom: str | None = None
        prev_ai: str | None = None
        if entry is not None and entry.get("size") == size:
            hit = True
            prev_custom = entry.get("custom")
            prev_ai = entry.get("ai")
        elif entry is not None and isinstance(entry.get("size"), int) \
                and 0 < entry["size"] <= size:
            prev_size = entry["size"]
            prev_custom = entry.get("custom")
            prev_ai = entry.get("ai")
    if hit:
        return prev_custom, prev_ai
    # Scan (I/O) outside the lock.
    new_custom, new_ai = _scan_titles(jsonl, prev_size)
    custom = new_custom or prev_custom
    ai = new_ai or prev_ai
    global _title_index_dirty
    with _title_index_lock:
        idx[key] = {"size": size, "mtime": st.st_mtime,
                    "custom": custom, "ai": ai}
        _title_index_dirty = True
    return custom, ai


def _resolve_title(jsonl: Path) -> str | None:
    """Authoritative display title for *jsonl* — the file, plus any rename pin.

    Prefers ``custom-title`` (user rename) over ``ai-title`` (auto summary).
    """
    custom, ai = _resolve_title_raw(jsonl)
    return _apply_rename_pin(str(jsonl), custom) or ai


def title_from_jsonl(jsonl: Path) -> str | None:
    """Extract a session's display title directly from a JSONL *path*.

    Prefers ``custom-title`` (user rename) over ``ai-title`` (auto summary).
    Reads the file it's handed — no re-resolution by session id — so it's
    inherently account-correct.  Goes through the incremental index so repeat
    lookups (and unchanged files) are cheap.
    """
    if not jsonl.exists():
        return None
    return _resolve_title(jsonl)


def read_session_title(session_id: str, config_dir: str | None = None) -> str | None:
    """Look up a session's display title from JSONL (custom-title or ai-title).

    *config_dir* scopes the lookup to a specific account's projects tree (for
    cross-account hub sessions); defaults to the active env account.
    """
    project = find_session_dir(session_id, config_dir)
    if project is None:
        return None
    return _resolve_title(project / f"{session_id}.jsonl")


def write_session_title(
    session_id: str, title: str, config_dir: str | None = None
) -> None:
    """Set a session's title by appending a ``custom-title`` record.

    *config_dir* scopes the write to a specific account's projects tree.  When
    it names an account other than the hub process's own
    ``CLAUDE_CONFIG_DIR``, the SDK ``rename_session`` path is skipped entirely:
    the SDK resolves the file via the *process* env, so it would either write
    to the wrong account or fail.  We locate the file ourselves via
    ``find_session_dir(session_id, config_dir)`` and append the record
    directly, which is account-correct and side-effect-free.

    Either way the rename is also *pinned* on our side (``_pin_rename``),
    because appending to the JSONL alone does not make a rename stick — see the
    long note above ``_RENAME_PIN_PATH``.
    """
    project = find_session_dir(session_id, config_dir)
    jsonl = (project / f"{session_id}.jsonl") if project is not None else None
    # Snapshot what the file says *before* we append.  That's the value the CLI
    # has cached and will keep re-stamping at EOF, so the pin has to recognise
    # it as obsolete rather than as a competing rename.
    prev = (_resolve_title_raw(jsonl)[0]
            if jsonl is not None and jsonl.exists() else None)

    # Only trust the SDK helper when we're targeting the process's own
    # account (config_dir is None or matches the env).  Otherwise the SDK's
    # env-scoped lookup would misfire, so go straight to the manual append.
    env_dir = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip() or None
    same_account = config_dir is None or (
        env_dir is not None and os.path.normpath(config_dir) == os.path.normpath(env_dir)
    )
    if same_account:
        sdk_ok = False
        try:
            from claude_agent_sdk import rename_session as _sdk_rename  # type: ignore
            _sdk_rename(session_id, title)
            sdk_ok = True
        except (ImportError, AttributeError):
            pass
        except FileNotFoundError:
            # The SDK searches by its own project-dir logic and may miss the
            # file (e.g. a non-default CLAUDE_CONFIG_DIR).  Fall through to the
            # manual append, which locates the file via our find_session_dir.
            pass
        if sdk_ok:
            # Preferred path: it goes through the running CLI, which also
            # updates that process's in-memory title — so its next compaction
            # stamp agrees with us instead of fighting us.  Pin anyway: the CLI
            # process can be replaced (resume) and lose that memory.
            if jsonl is not None:
                _pin_rename(jsonl, title, prev)
            return
    if project is None:
        raise OSError(f"session {session_id} not found on disk")
    assert jsonl is not None
    if not jsonl.exists():
        raise OSError(f"session jsonl missing: {jsonl}")
    record = {
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }
    with jsonl.open("a", encoding="utf-8") as f:
        # Compact separators, byte-identical to the CLI's own JSON.stringify
        # output.  Its external-writer check is
        # ``line.startsWith('{"type":"custom-title"')`` — a space after the
        # colon would defeat it outright, so this record has to be compact to
        # have even a chance of being noticed.
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    _pin_rename(jsonl, title, prev)


# ---------------------------------------------------------------------------
# Session info parsing
# ---------------------------------------------------------------------------

# Content prefixes the Claude Code harness uses for synthetic prompts
# it feeds to the model.  These are indistinguishable from user-typed
# prompts at the JSONL field-shape level (same ``type:"user"`` record,
# same ``permissionMode``, same queue-operation enqueue) — the only
# reliable signal is the content itself.
_HARNESS_INJECTED_PREFIXES = (
    "# Autonomous loop check",
    "# Autonomous loop tick",
    "This session is being continued from a previous conversation",
    # Claude Code's proactive/autonomous mode: each wake-up sends a
    # tick prompt of the form ``<tick>HH:MM:SS</tick>`` (see
    # claude-code-mod/src/cli/print.ts:~1845).  Multiple ticks may be
    # batched into one message, but the payload still starts with
    # ``<tick>``.  Without this prefix the orchestrator classified
    # them as user-typed messages, so the "claude set up an
    # autonomous loop" wake-ups didn't render as collapsed injected
    # boxes the way compaction prompts did.
    "<tick>",
    # The orchestrator's retired auto-continue prompt.  That loop has been
    # removed (see known-issues.md), so nothing emits this text any more —
    # but historical transcripts still contain it, including sessions from
    # the single-file Python Agent orchestrator, which *does* still
    # implement auto-continue.  Without this prefix those replay as ordinary
    # user bubbles instead of collapsed injected-prompt boxes, so this entry
    # is load-bearing for history rendering and must not be removed with the
    # rest of the auto-continue code.
    "If you need input from me before continuing, pause and include",
    # Orchestrator's ScheduleWakeup heartbeat re-injection
    # (WAKEUP_RESOLVED_PROMPT from config.py).  Same rationale as the
    # auto-continue prompt above: surface it as a collapsed injected box
    # on both the live echo and history replay rather than a user bubble.
    # A custom wakeup prompt (model-supplied /loop task text) won't match
    # — accepted tradeoff: custom text only shows as injected on the live
    # path, not on replay.
    "Autonomous-loop wakeup (scheduled by you)",
)

# XML-wrapped CLI internals that the user shouldn't see at all.
_XML_INTERNAL_PREFIXES = (
    "<bash",
    "<tool",
    "<task-notification",
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<system-reminder>",
)


def _classify_user_text(text: str) -> str:
    """Classify a ``type:"user"`` record's text payload.

    Returns one of:

    * ``"drop"`` — XML-wrapped CLI internal, don't surface at all.
    * ``"injected_prompt"`` — harness-injected synthetic prompt.
    * ``"user"`` — user-typed message.

    The harness writes both kinds with the same JSONL field shape, so
    classification has to be content-based: prefix match against
    ``_HARNESS_INJECTED_PREFIXES``.  These cover the cases where the
    harness owns the entire payload (autonomous-loop ticks, post-compact
    continuation, proactive ``<tick>``, the retired auto-continue prompt).

    Anything not matching is treated as user-typed.  Substring scans
    were tried but caused false positives — a user who pastes the
    autonomous-loop instructions into their own prompt (e.g. to discuss
    them with Claude) would have had their real message reclassified as
    harness-injected.
    """
    for p in _XML_INTERNAL_PREFIXES:
        if text.startswith(p):
            return "drop"
    for p in _HARNESS_INJECTED_PREFIXES:
        if text.startswith(p):
            return "injected_prompt"
    return "user"


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


# How much of a session JSONL to read from the front when gathering list
# metadata.  cwd, the first user message, and the AI title all live in the
# opening records, so a bounded head read captures them without touching the
# rest of a (possibly hundreds-of-MB) file.
_HEAD_SCAN_BYTES = 512 * 1024
# How many bytes to read from the tail to catch a recent rename (custom-title,
# appended at rename time) plus the last user message / timestamp.  A fixed
# byte budget keeps the tail read cheap even on a 276 MB file (a records-based
# tail would over-read hundreds of MB to guarantee a record count).
_TAIL_SCAN_BYTES = 256 * 1024

# Cache of parsed session info keyed by absolute path → (mtime, size, info).
# Session JSONLs are append-only, so an unchanged (mtime, size) pair means the
# cached parse is still valid.  This keeps repeated lobby refreshes (every few
# seconds while the overlay is open) from re-scanning every file on disk.
_session_info_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _absorb_session_record(
    info: dict[str, Any],
    rec: dict[str, Any],
    titles: dict[str, str],
    *,
    count: bool,
) -> None:
    """Fold a single JSONL record into the accumulating *info* / *titles*.

    *count* controls whether this record bumps ``msg_count`` — set True for the
    head pass (exact for records we actually see) and False for the tail pass
    (those records are estimated into the total separately, not double-counted).
    """
    t = rec.get("type")
    if t == "custom-title":
        v = rec.get("customTitle")
        if isinstance(v, str) and v.strip():
            titles["custom"] = v.strip()
        return
    if t == "ai-title":
        v = rec.get("aiTitle")
        if isinstance(v, str) and v.strip():
            titles["ai"] = v.strip()
        return
    if count:
        info["msg_count"] += 1
    if info["cwd"] is None and isinstance(rec.get("cwd"), str):
        info["cwd"] = rec["cwd"]
    ts = rec.get("timestamp")
    if isinstance(ts, str):
        info["last_timestamp"] = ts
    if t == "user":
        msg = rec.get("message")
        text = ""
        if isinstance(msg, dict):
            text = _extract_text(msg.get("content"))
        elif isinstance(msg, str):
            text = msg
        text = text.strip()
        # Skip SDK-injected XML messages (e.g. <bash>, <tool>,
        # <local-command-caveat>, <command-name>, etc.) that aren't real
        # user input.
        if text and not (len(text) > 1 and text[0] == '<' and text[1:2].isalpha()):
            if info["first_user_msg"] is None:
                info["first_user_msg"] = text
            info["last_user_msg"] = text


def _parse_session_info(jsonl: Path, project_slug: str) -> dict[str, Any] | None:
    """Read a session JSONL and extract id, cwd, messages, timestamp, title.

    Bounded + cached: reads only the head (first ``_HEAD_SCAN_BYTES``) and — for
    files larger than that — the tail (last ``_TAIL_SCAN_BYTES``), rather than
    the whole file.  A session JSONL can be hundreds of MB; scanning every one
    in full on each lobby refresh froze the UI for many seconds.  The head
    yields cwd / first user message; the latest user message comes from the
    tail.  The title comes from the authoritative incremental index
    (``_resolve_title``), not the bounded read.  ``msg_count`` is exact for
    fully-read (small) files and estimated for large ones.

    Results are cached by (mtime, size); append-only JSONLs mean an unchanged
    stat is a valid cache hit.
    """
    try:
        st = jsonl.stat()
    except OSError:
        return None

    key = str(jsonl)
    cached = _session_info_cache.get(key)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]

    size = st.st_size
    info: dict[str, Any] = {
        "session_id": jsonl.stem,
        "project_slug": project_slug,
        "cwd": None,
        "first_user_msg": None,
        "last_user_msg": None,
        "last_timestamp": None,
        "mtime": st.st_mtime,
        "size": size,
        "msg_count": 0,
        "title": None,
        "msg_count_estimated": False,
    }
    titles: dict[str, str] = {}

    # --- Head pass: read complete lines until we cross the byte budget. ---
    head_bytes = 0
    head_count = 0
    try:
        with jsonl.open("rb") as fb:
            for raw in fb:
                head_bytes += len(raw)
                s = raw.strip()
                if s:
                    try:
                        rec = json.loads(s)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        rec = None
                    if isinstance(rec, dict):
                        head_count += 1
                        _absorb_session_record(info, rec, titles, count=True)
                if head_bytes >= _HEAD_SCAN_BYTES:
                    break
    except OSError:
        return None

    fully_read = head_bytes >= size

    # --- Tail pass: only when the head didn't already cover the whole file. ---
    if not fully_read:
        tail_recs: list[dict[str, Any]] = []
        try:
            with jsonl.open("rb") as fb:
                start = max(head_bytes, size - _TAIL_SCAN_BYTES)
                if start > 0:
                    fb.seek(start)
                    fb.readline()  # discard the (likely partial) first line
                for raw in fb:
                    s = raw.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(rec, dict):
                        tail_recs.append(rec)
        except OSError:
            tail_recs = []
        for rec in tail_recs:
            _absorb_session_record(info, rec, titles, count=False)
        # Estimate the total message count from the head sample's density.
        if head_count > 0 and head_bytes > 0:
            est = int(size / (head_bytes / head_count))
            info["msg_count"] = max(est, head_count)
        info["msg_count_estimated"] = True

    # Title comes from the authoritative incremental index (a rename buried
    # mid-file wouldn't be caught by the bounded head/tail read); fall back to
    # whatever the bounded scan happened to see.
    info["title"] = _resolve_title(jsonl) or titles.get("custom") or titles.get("ai")

    _session_info_cache[key] = (st.st_mtime, size, info)
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

def list_projects(config_dir: str | None = None) -> list[dict[str, Any]]:
    """Light-weight project listing (for session picker / REST endpoint).

    *config_dir* overrides which account's ``projects`` dir is scanned; when
    ``None`` it falls back to ``CLAUDE_CONFIG_DIR`` / ``~/.claude``.
    """
    projects_dir = claude_projects_dir(config_dir)
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

def _tail_read_jsonl(
    jsonl: Path,
    max_records: int,
) -> tuple[list[dict[str, Any]], int]:
    """Read the last *max_records* JSON records from a JSONL file.

    For small files (< 2 MB) reads the whole thing.  For large files,
    seeks near the end and reads only the tail — avoiding the multi-second
    stall of parsing a 200+ MB file when we only need the last 2000 lines.

    Returns ``(records, total_skipped)`` where *total_skipped* is a lower
    bound on how many records were before the returned window (exact for
    small files, approximate for tail-read files).
    """
    SMALL_THRESHOLD = 2 * 1024 * 1024  # 2 MB

    try:
        file_size = jsonl.stat().st_size
    except OSError:
        file_size = 0

    if file_size <= SMALL_THRESHOLD:
        # Small file — read the whole thing (fast enough).
        records: list[dict[str, Any]] = []
        try:
            f = jsonl.open(encoding="utf-8", errors="replace")
        except OSError:
            return [], 0
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
        total = len(records)
        if max_records and total > max_records:
            return records[-max_records:], total - max_records
        return records, 0

    # Large file — binary seek to approximate tail position.
    # Over-read by 4x the average line estimate to ensure we get enough.
    # Average JSONL line in our sessions is ~2-10 KB; budget generously.
    avg_line_bytes = max(file_size // max(max_records * 8, 1), 2048)
    seek_bytes = min(max_records * avg_line_bytes * 4, file_size)

    records = []
    try:
        with jsonl.open("rb") as fb:
            if seek_bytes < file_size:
                fb.seek(file_size - seek_bytes)
                fb.readline()  # discard partial first line
            for raw in fb:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                records.append(rec)
    except OSError:
        return [], 0

    if max_records and len(records) > max_records:
        skipped = len(records) - max_records
        records = records[-max_records:]
    else:
        skipped = 0

    # We don't know the exact total for the seek-based path; estimate.
    if seek_bytes < file_size:
        # We skipped (file_size - seek_bytes) bytes worth of lines.
        approx_skipped_before_seek = int(
            (file_size - seek_bytes) / max(avg_line_bytes, 1)
        )
        skipped += approx_skipped_before_seek

    return records, skipped


def render_session_history(
    jsonl: Path,
    *,
    max_history: int = 2000,
) -> tuple[int, list[dict[str, Any]], list[str]]:
    """Build structured history for the frontend.

    Returns ``(message_count, messages, orphan_ids)``.

    Reads the JSONL, keeps the last *max_history* records, and emits
    structured messages the frontend can render.  tool_use and
    tool_result are emitted as separate messages — the frontend pairs
    them by ``tool_use_id``.

    For large JSONL files (> 2 MB), only the tail of the file is read
    to avoid blocking for seconds on 200+ MB files.
    """
    rendered = 0

    # --- Read and truncate ---
    try:
        records, truncated = _tail_read_jsonl(jsonl, max_history)
    except OSError as e:
        return 0, [{
            "type": "system",
            "subtype": "error",
            "content": f"Failed to open {jsonl.name}: {e}",
            "is_history": True,
        }], []

    # --- Emit structured messages ---
    messages: list[dict[str, Any]] = []

    if truncated:
        messages.append({
            "type": "system",
            "content": f"({truncated} older messages not shown)",
            "is_history": True,
        })

    for rec in records:
        t = rec.get("type")
        msg = rec.get("message")

        # --- User messages ---
        if t == "user" and isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                text = content.strip()
                classified = _classify_user_text(text)
                if classified == "drop":
                    pass  # XML wrapper / internal — don't render
                else:
                    messages.append({
                        "type": classified,  # "user" or "injected_prompt"
                        "content": text,
                        "is_history": True,
                    })
                    rendered += 1

            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text" and isinstance(block.get("text"), str):
                        text = block["text"].strip()
                        if text:
                            classified = _classify_user_text(text)
                            if classified == "drop":
                                continue
                            messages.append({
                                "type": classified,
                                "content": text,
                                "is_history": True,
                            })
                            rendered += 1
                    elif bt == "tool_result" and block.get("tool_use_id"):
                        inner = block.get("content")
                        text = (
                            inner if isinstance(inner, str)
                            else _extract_text(inner)
                        )
                        text = (text or "").strip()
                        messages.append({
                            "type": "tool_result",
                            "tool_use_id": block["tool_use_id"],
                            "content": text,
                            "is_error": bool(block.get("is_error")),
                            "size_hint": _humanize_size(text),
                            "is_history": True,
                        })

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
                    messages.append({
                        "type": "tool_use",
                        "name": name,
                        "input": inp,
                        "tool_use_id": block.get("id"),
                        "status": "complete",
                        "header": format_tool_header(name, inp),
                        "is_history": True,
                    })
                elif bt == "thinking":
                    thinking_text = (block.get("thinking") or "").strip()
                    if thinking_text:
                        messages.append({
                            "type": "thinking",
                            "content": thinking_text,
                            "is_history": True,
                        })
                    elif block.get("signature"):
                        # Encrypted reasoning (opus-4-8 / opus-5 and newer):
                        # signature only, no text.  Still show the block so the
                        # replayed transcript matches what the live turn showed.
                        messages.append({
                            "type": "thinking",
                            "content": "",
                            "encrypted": True,
                            "is_history": True,
                        })
            rendered += 1

    return rendered, messages, []


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
        "title": title_from_jsonl(jsonl),
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
