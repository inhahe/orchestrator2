#!/usr/bin/env python3
"""copy_session.py — graphical TUI to copy a Claude session between accounts.

Claude Code / the Agent SDK store each conversation as a JSONL file at:

    <claude-dir>/projects/<project-slug>/<session-id>.jsonl

where <project-slug> is the working directory with every non-alphanumeric
character replaced by a dash.  ``claude --resume <id>`` (and orchestrator2's
resume picker) only find a session when it lives under the config dir they're
using — which is per-account.  To resume a conversation started under one
account from a *different* account, its JSONL has to be copied into the other
account's ``.claude`` directory under the same project-slug sub-path.

Run with no arguments to launch the full-screen TUI wizard:

    python copy_session.py

The wizard walks through:
  1. pick which ``.claude`` account to copy FROM (discovered automatically),
  2. pick a session — shown as a tree grouped by project directory,
  3. pick which ``.claude`` account to copy TO,
  4. name the copied session (defaults to the original id).

If the new name differs from the original session id, the copy rewrites the
``sessionId`` fields inside the JSONL so the session is genuinely resumable
under the new id.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from account import hub_account

# Title prefix that tells tray_minimizer to leave this window visible (never
# minimize it to the tray).  Must match tray_minimizer.NO_HIDE_TITLE_PREFIX.
# When server.py runs us as the --resume/--copy picker in our own console
# (CREATE_NEW_CONSOLE, because the server's own console is hidden in the
# tray), we mark that console so the picker stays on screen while the server
# sits hidden.  This is set *before* the slow `textual` import below so the
# window is already marked by the time tray_minimizer inspects it.
NO_HIDE_TITLE_PREFIX = "[nohide]"

if "--pick" in sys.argv and sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(
            f"{NO_HIDE_TITLE_PREFIX} orchestrator2 session picker"
        )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Discovery / parsing (no TUI dependency — importable & testable on its own)
# ---------------------------------------------------------------------------


def _sanitize_cwd(cwd: str) -> str:
    """Mirror Claude Code's project-slug scheme: non-alphanumerics -> '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _projects_dir(claude_dir: Path) -> Path:
    return claude_dir / "projects"


def _same_dir(a: Path, b: Path) -> bool:
    """True when two paths point at the same directory (resolved)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def discover_claude_dirs() -> list[Path]:
    """Find candidate ``.claude`` directories on this machine.

    Scans the home directory for ``.claude*`` folders that contain a
    ``projects`` sub-folder, plus this process's account
    (``account.hub_account``) if it has one elsewhere.  Returns a
    de-duplicated list sorted by name.
    """
    found: dict[str, Path] = {}

    def _consider(p: Path) -> None:
        try:
            rp = p.resolve(strict=False)
        except OSError:
            rp = p
        if _projects_dir(rp).is_dir():
            found[str(rp).lower()] = rp

    home = Path.home()
    try:
        for child in home.iterdir():
            if child.is_dir() and child.name.startswith(".claude"):
                _consider(child)
    except OSError:
        pass

    own = hub_account()
    if own:
        _consider(Path(own))

    # The plain default, even if it doesn't match the glob for some reason.
    _consider(home / ".claude")

    return sorted(found.values(), key=lambda p: p.name.lower())


def count_sessions(claude_dir: Path) -> int:
    root = _projects_dir(claude_dir)
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.glob("*/*.jsonl"))


def sessions_with_id(claude_dir: Path, session_id: str) -> list[Path]:
    """All session files named ``<session_id>.jsonl`` anywhere in *claude_dir*.

    A session id is a filename stem under ``projects/<slug>/``.  The same id
    living under two different project slugs makes ``claude --resume <id>``
    ambiguous, so callers warn before minting a duplicate.
    """
    root = _projects_dir(claude_dir)
    if not root.is_dir():
        return []
    return [p for p in root.glob(f"*/{session_id}.jsonl") if p.is_file()]


def _find_recent_for_cwd(claude_dir: Path, cwd: str | None) -> Path | None:
    """Most-recently-modified session ``.jsonl`` for *cwd* under *claude_dir*.

    Mirrors what the server would auto-continue: the newest session in the
    project directory for *cwd* (Claude's ``<cwd>`` → slug scheme).  Falls back
    to matching a project by its stored cwd when the direct slug isn't present.
    """
    if not cwd:
        return None
    root = _projects_dir(claude_dir)
    if not root.is_dir():
        return None
    slug_dir = root / _sanitize_cwd(cwd)
    proj: Path | None = slug_dir if slug_dir.is_dir() else None
    if proj is None:
        try:
            target = str(Path(cwd).resolve(strict=False))
        except OSError:
            target = cwd
        for pdir in root.iterdir():
            if not pdir.is_dir():
                continue
            matched = False
            for f in pdir.glob("*.jsonl"):
                stored = _read_session_meta(f).get("cwd")
                if stored:
                    try:
                        matched = str(Path(stored).resolve(strict=False)) == target
                    except OSError:
                        matched = stored == cwd
                break  # only sniff one jsonl per project
            if matched:
                proj = pdir
                break
    if proj is None:
        return None
    best: tuple[Path, float] | None = None
    for f in proj.glob("*.jsonl"):
        if not f.is_file():
            continue
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best[1]:
            best = (f, m)
    return best[0] if best else None


def _read_session_meta(jsonl: Path) -> dict[str, Any]:
    """Extract a display title, cwd and mtime from a session JSONL."""
    custom_title: str | None = None
    ai_title: str | None = None
    first_user: str | None = None
    cwd: str | None = None
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
                if cwd is None and isinstance(rec.get("cwd"), str):
                    cwd = rec["cwd"]
                t = rec.get("type")
                if t == "custom-title":
                    v = rec.get("customTitle")
                    if isinstance(v, str) and v.strip():
                        custom_title = v.strip()
                elif t == "ai-title":
                    v = rec.get("aiTitle")
                    if isinstance(v, str) and v.strip():
                        ai_title = v.strip()
                elif first_user is None and t == "user":
                    first_user = _first_text(rec)
    except OSError:
        pass

    try:
        mtime = jsonl.stat().st_mtime
    except OSError:
        mtime = 0.0

    title = custom_title or ai_title or first_user or "(untitled)"
    title = " ".join(title.split())  # collapse whitespace/newlines
    if len(title) > 70:
        title = title[:69] + "…"
    return {
        "path": jsonl,
        "id": jsonl.stem,
        "title": title,
        "cwd": cwd,
        "mtime": mtime,
    }


def _first_text(rec: dict[str, Any]) -> str | None:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                v = block.get("text")
                if isinstance(v, str):
                    return v
    return None


def scan_sessions(claude_dir: Path, progress=None) -> list[dict[str, Any]]:
    """Return per-project groups of sessions, newest project first.

    Each group: ``{"dir": Path, "label": str, "sessions": [meta, ...]}``.

    Reading a session's title means opening its JSONL, so a large account can
    take a while.  Pass *progress* — a callable ``progress(done)`` — to be
    notified after each session is read (used by the TUI to show a live
    "scanning… N/total" indicator instead of appearing to hang).
    """
    root = _projects_dir(claude_dir)
    groups: list[dict[str, Any]] = []
    if not root.is_dir():
        return groups
    done = 0
    for pdir in root.iterdir():
        if not pdir.is_dir():
            continue
        sessions = []
        for f in pdir.glob("*.jsonl"):
            if not f.is_file():
                continue
            sessions.append(_read_session_meta(f))
            done += 1
            if progress is not None:
                progress(done)
        if not sessions:
            continue
        sessions.sort(key=lambda s: s["mtime"], reverse=True)
        cwd = next((s["cwd"] for s in sessions if s["cwd"]), None)
        groups.append({
            "dir": pdir,
            "label": cwd or pdir.name,
            "sessions": sessions,
        })
    groups.sort(key=lambda g: g["sessions"][0]["mtime"], reverse=True)
    return groups


def copy_session_file(src_jsonl: Path, dest_jsonl: Path, new_id: str, *,
                      new_cwd: str | None = None,
                      new_branch: str | None = None,
                      old_id: str | None = None,
                      relocate: tuple[Path, Path] | None = None) -> None:
    """Copy *src_jsonl* to *dest_jsonl*, rewriting the metadata that has moved.

    If *new_id* differs from the source's session id -- *old_id*, which is
    the file's own name unless given (a subagent's transcript is named for the
    subagent but records its session's id) -- every top-level ``sessionId``
    field equal to the old id is rewritten to *new_id* so the copy resumes
    cleanly under the new name.

    *new_cwd* moves the session to a different **working directory**: every
    top-level ``cwd`` is rewritten, and *new_branch* (pass ``""`` for "not a
    git repo") replaces every top-level ``gitBranch``.  Both are metadata
    describing where the session *is*, so leaving them stale would make the
    copy contradict the project directory it now lives in -- ``sniff_session_cwd``
    would report the old path, and the hub's "switch cwd to match the session's
    recorded cwd" step on resume would drag the session straight back.

    **Only the top-level fields are touched.**  Paths inside ``message`` and
    ``toolUseResult`` are a record of what actually happened -- that Read really
    did read ``D:\\old\\thing.py`` -- and rewriting them would falsify the
    transcript rather than relocate it.

    Every rewrite is applied to *every* record, not only the ones matching the
    old value, so the copy cannot end up half in one directory and half in
    another (a session that used ``/cwd`` mid-flight has records from two
    directories, but the copy lands in exactly one project dir and must agree
    with it).

    *relocate* -- ``(old, new)`` session folders, the copy having its own --
    is the one exception to leaving paths in messages alone.  The folder is the
    CLI's storage, not the user's: a tool result too large to keep inline is
    saved there and the transcript says where, and a compaction summary names
    the transcript beside it.  Those paths are rewritten to the copy's, so the
    copy does not depend on files that go when the original does.
    """
    old_id = old_id or src_jsonl.stem
    dest_jsonl.parent.mkdir(parents=True, exist_ok=True)
    rewrite_id = new_id != old_id
    fix = _relocator(*relocate) if relocate is not None else None
    # How the folder's name shows in a line of JSON that mentions a path into
    # it: the id, then a separator (a backslash is escaped, but begins with
    # one) or the transcript's extension.  Most lines do not, and skip the walk.
    mentions = (old_id + "\\", old_id + "/", old_id + ".jsonl")

    if not rewrite_id and new_cwd is None and new_branch is None and fix is None:
        shutil.copy2(src_jsonl, dest_jsonl)
        return

    tmp = dest_jsonl.with_name(dest_jsonl.name + ".tmp")
    try:
        # newline="": the CLI writes and appends LF, and a text-mode write on
        # Windows would make every copied record CRLF.
        with src_jsonl.open(encoding="utf-8", errors="replace") as fin, \
                tmp.open("w", encoding="utf-8", newline="") as fout:
            for line in fin:
                stripped = line.strip()
                if not stripped:
                    fout.write(line)
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    fout.write(line)
                    continue
                if isinstance(rec, dict):
                    if rewrite_id and rec.get("sessionId") == old_id:
                        rec["sessionId"] = new_id
                    if new_cwd is not None and "cwd" in rec:
                        rec["cwd"] = new_cwd
                    if new_branch is not None and "gitBranch" in rec:
                        rec["gitBranch"] = new_branch
                    if fix is not None and any(m in stripped for m in mentions):
                        rec = _map_strings(rec, fix)
                fout.write(json.dumps(rec) + "\n")
        os.replace(tmp, dest_jsonl)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _relocator(old: Path, new: Path):
    """A function of a string that points every path into folder *old* -- or
    at the transcript named for it -- into *new* instead.

    Matched with either separator, and on Windows in any case, since the path
    was written by the CLI from its config dir as *it* was given it.  A
    replacement keeps the separators of what it replaces: the path may sit in
    text that escapes them (JSON a tool printed), and must stay escaped there.
    """
    split = re.compile(r"[\\/]+")
    pattern = re.compile(
        split.pattern.join(re.escape(p) for p in split.split(str(old)))
        + r"(?=[\\/]|\.jsonl)",
        re.IGNORECASE if os.name == "nt" else 0)
    new_parts = split.split(str(new))

    def fix(s: str) -> str:
        return pattern.sub(
            lambda m: split.search(m.group(0)).group(0).join(new_parts), s)
    return fix


def _map_strings(obj: Any, fn) -> Any:
    """*obj*, a decoded JSON value, with *fn* applied to every string in it."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _map_strings(v, fn) for k, v in obj.items()}
    return obj


def copy_whole_session(src_jsonl: Path, dest_jsonl: Path, new_id: str, *,
                       src_cfg: str | Path, dest_cfg: str | Path,
                       new_cwd: str | None = None,
                       new_branch: str | None = None) -> None:
    """Copy the session whose transcript is *src_jsonl*, in account *src_cfg*,
    to *dest_jsonl* in account *dest_cfg* as session *new_id* -- all of it,
    so the copy stands without the original.

    A session is more than its transcript.  Beside it the CLI keeps a folder
    of the same name (``subagents/`` -- the transcripts of the agents it ran
    -- and ``tool-results/``), and in the account a ``file-history/<id>``
    folder (the backups ``/rewind`` restores from).  Each is copied under the
    new id; the transcripts in the folder are rewritten as the main one is
    (``copy_session_file``).  The folder's ``custom-title.json`` is not: it
    holds the original's name, which the CLI would stamp back on the copy.

    If any part fails, what this wrote is removed and the error raised: half a
    copy is not one.
    """
    old_id = src_jsonl.stem
    old_folder, new_folder = src_jsonl.with_suffix(""), dest_jsonl.with_suffix("")
    old_hist = Path(src_cfg) / "file-history" / old_id
    new_hist = Path(dest_cfg) / "file-history" / new_id
    ours = [p for p in (dest_jsonl, new_folder, new_hist) if not p.exists()]
    rewrite = dict(new_cwd=new_cwd, new_branch=new_branch,
                   relocate=(old_folder, new_folder))
    try:
        copy_session_file(src_jsonl, dest_jsonl, new_id, **rewrite)
        if old_folder.is_dir():
            for path in sorted(old_folder.rglob("*")):
                rel = path.relative_to(old_folder)
                if path.is_dir() or rel == Path("custom-title.json"):
                    continue
                dest = new_folder / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Subagents' transcripts, not every .jsonl: a tool's output
                # saved under tool-results/ is kept byte for byte.
                if rel.parts[0] == "subagents" and path.suffix == ".jsonl":
                    copy_session_file(path, dest, new_id, old_id=old_id, **rewrite)
                else:
                    shutil.copy2(path, dest)
        if old_hist.is_dir():
            shutil.copytree(old_hist, new_hist)
    except BaseException:
        for p in ours:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        raise


def count_records(jsonl: Path) -> int:
    """How many records transcript *jsonl* holds: its non-blank lines."""
    with jsonl.open("rb") as f:
        return sum(1 for line in f if line.strip())


def git_branch_for(cwd: str) -> str:
    """The current git branch in *cwd*, or ``""`` when it isn't a repo.

    Used when a session is copied into a different directory: the recorded
    branch belonged to the *old* directory and would otherwise be carried into
    a tree it has nothing to do with.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _fmt_time(mtime: float) -> str:
    if not mtime:
        return "????-??-?? ??:??"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Entry point -- the TUI, imported only when it is actually going to run
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the wizard.  The ``textual`` import lives past this point on purpose.

    Everything above is plain discovery and parsing that server.py leans on
    constantly; the wizard is a program you run by hand.  Keeping the import
    here means a machine without ``textual`` loses the picker and keeps the
    hub, rather than the other way round.
    """
    try:
        from copy_session_tui import main as tui_main
    except ImportError as exc:            # pragma: no cover - install-dependent
        print(f"The session picker needs the 'textual' package: {exc}\n"
              f"Install it with:  pip install textual", file=sys.stderr)
        return 2
    return tui_main()


if __name__ == "__main__":
    raise SystemExit(main())
