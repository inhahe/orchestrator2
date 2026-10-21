"""Carrying a session on by itself once a usage limit resets.

``--resume-after-limit`` (or ``/resume-after-limit on``): when a subscription
usage limit -- the 5-hour session limit, a weekly one -- stops a session, it is
sent a prompt once the limit has reset, and carries on.  Off by default: a
session that starts working with nobody having typed anything is the shape of
every "why is it doing that?" report this project has had (design.md §6e).

This module is the part that needs no bridge, so it can be tested without one:

* **What stopped the session**, read from its transcript
  (:func:`limit_stop_in`).  The CLI writes each limit it hits as a synthetic
  assistant record carrying ``error: "rate_limit"`` and the API's quota
  headers, ``resetsAt`` among them -- a Unix time, so nothing is parsed out of
  the "resets 9pm (America/New_York)" text.  Read when a session is opened,
  so a session stopped while no hub was watching it, or under a hub that has
  since restarted, is picked up too.
* **The last prompt it was given** (:func:`last_prompt_in`), which the resume
  repeats word for word: by the time a long turn hits a limit, compaction may
  have reduced it to a summary.
* **The resume prompt** itself (:func:`build_resume_prompt`).
* **The schedule**, on disk (``save`` / ``load`` / ``clear``), so that a
  session waiting on a reset survives the hub restarting and the idle teardown
  -- which closes it rather than keep a CLI alive for the hours or days a limit
  can take.  ``server`` reopens it when it is due.  The record says only *which*
  session to look at and when; the transcript decides, when it is reopened,
  whether there is still anything to resume.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from account import account_dir
from session import (
    _COMMAND_RECORD_PREFIXES,
    _USER_RECORD,
    CUT_OFF_TAIL_BYTES,
    LIMIT_RESUME_PROMPT_PREFIX,
    TURN_START_SEARCH_BYTES,
    _classify_user_text,
    _has_tool_result,
    _is_cli_resume_bookkeeping,
    _local_when,
    _record_text,
)

#: How long after the reset the session is resumed.  The limit is the API's
#: to lift, and a prompt sent on the second it was due can still be refused.
MARGIN_S = 60.0

#: How long a session reopened after its reset has passed waits before being
#: resumed, so the hub, its tabs and the CLI have settled first.
SETTLE_S = 20.0

#: How late, past the reset, a session opened then is still resumed by
#: itself.  Later than that the work it was doing is a day old, and opening it
#: -- to look at it, say -- should not set it going.  ``/resume-after-limit on``
#: still resumes it: that is asking.
LATE_S = 12 * 3600.0

#: A reset further ahead than this is not one: the longest limit is a week.
MAX_AHEAD_S = 8 * 86400.0

#: How many resumes in a row may run straight back into the same limit before
#: the session stops resuming by itself, and the first wait between them,
#: doubled each time.
MAX_ATTEMPTS = 5
RETRY_S = 120.0

#: How many times a resume that comes due while a turn is running is put off
#: by a minute before it is dropped.  A running turn means something is
#: already working the session, and if it was stopped by the limit, the end of
#: that turn arms a resume again.
MAX_DEFERS = 30
DEFER_S = 60.0

#: The longest prompt repeated in full.  Past this it is very likely a pasted
#: log rather than instructions, and the instructions are usually at the top.
REPEAT_MAX_CHARS = 20_000

#: How every resume prompt begins.  One of session.py's harness prefixes, so
#: history shows it as the orchestrator's and not as something the user typed,
#: and so :func:`last_prompt_in` passes over an earlier resume.
PROMPT_PREFIX = LIMIT_RESUME_PROMPT_PREFIX

#: The CLI's names for each window, for the user and the model.
_LIMIT_NAMES = {
    "five_hour": "5-hour session limit",
    "seven_day": "weekly limit",
    "seven_day_opus": "weekly Opus limit",
    "seven_day_sonnet": "weekly Sonnet limit",
    "overage": "extra-usage limit",
}


def limit_name(kind: str | None) -> str:
    """What the user calls the limit of *kind* (a ``rateLimitType``)."""
    return _LIMIT_NAMES.get(kind or "", "usage limit")


def effective_reset(*, status: Any, resets_at: Any,
                    overage_resets_at: Any = None) -> int | None:
    """When a rejected limit lets the session go again for free.

    The plan's reset, ``resetsAt``.  Not the earlier of it and extra usage's,
    which is what the CLI's own "resets 9pm" names when both are refused
    (``rateLimitMessages.getLimitReachedText``): extra usage is billed, and
    resuming onto it would spend money nobody chose to spend.  Extra usage's
    only when the plan's is missing.  None when nothing is rejected, or no
    time was given.
    """
    if status != "rejected":
        return None
    for ts in (resets_at, overage_resets_at):
        if _is_time(ts):
            return int(ts)
    return None


def _is_time(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _quota_of(rec: dict) -> dict | None:
    """The quota headers a rate-limit error record carries, if any."""
    for q in (rec.get("quotaLimits"),
              (rec.get("apiErrorParams") or {}).get("rate_limit_info")
              if isinstance(rec.get("apiErrorParams"), dict) else None):
        if isinstance(q, dict):
            return q
    return None


def _is_limit_error(rec: dict) -> bool:
    return rec.get("type") == "assistant" and bool(rec.get("isApiErrorMessage")) \
        and (rec.get("error") == "rate_limit"
             or rec.get("apiError") == "usage_limit_reached")


# ---------------------------------------------------------------------------
# Reading the transcript
# ---------------------------------------------------------------------------

def _tail_records(jsonl: Path, tail_bytes: int) -> list[dict]:
    """The main-chain user, assistant and attachment records at *jsonl*'s end."""
    try:
        size = jsonl.stat().st_size
        with open(jsonl, "rb") as f:
            start = max(0, size - tail_bytes)
            f.seek(start)
            raw = f.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]           # the first is cut in half
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and not rec.get("isSidechain") \
                and rec.get("type") in ("user", "assistant", "attachment"):
            records.append(rec)
    return records


def _passes_by(rec: dict) -> bool:
    """A user record that arrived while the session was stopped and does not
    mean it moved on: a background task's notification, a peer's message,
    something the CLI or a hook added, a local command's output.

    Each of these makes the CLI try a turn, and that turn runs into the limit
    too -- so they sit between limit errors, and none of them is the user
    taking the session somewhere else.
    """
    origin = rec.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind and kind != "human":
        return True
    if rec.get("isMeta"):
        return True
    return _record_text(rec.get("message")).startswith(_COMMAND_RECORD_PREFIXES)


def limit_stop_in(jsonl: Path, *,
                  tail_bytes: int = CUT_OFF_TAIL_BYTES) -> dict | None:
    """Whether *jsonl*'s session was last stopped by a usage limit, and which.

    None unless its last word is the CLI reporting a usage limit with a reset
    time.  Looked past on the way, as the user asked: whatever arrived while
    the session was stopped (:func:`_passes_by`) and the CLI's resume
    bookkeeping.  A prompt the user sent after the error, or any reply from the
    model, means the session moved on.  Otherwise::

        {"resets_at": <Unix time the limit lets it go again>,
         "kind": <the CLI's rateLimitType, e.g. "five_hour", or None>,
         "hit_at": <ISO time of the first error of the run that stopped it>,
         "last_at": <ISO time of the last>}
    """
    records = _tail_records(jsonl, tail_bytes)
    found: dict | None = None
    for i in range(len(records) - 1, -1, -1):
        rec = records[i]
        if _is_cli_resume_bookkeeping(rec):
            continue
        t = rec.get("type")
        if t == "attachment":
            att = rec.get("attachment")
            if isinstance(att, dict) and att.get("type") == "queued_command" \
                    and att.get("commandMode", "prompt") == "prompt":
                break                 # a prompt sent after it: moved on
            continue
        if t == "assistant":
            if not _is_limit_error(rec):
                break                 # the model's reply, or another error
            q = _quota_of(rec) or {}
            if found is None:
                reset = effective_reset(
                    status=q.get("status"), resets_at=q.get("resetsAt"),
                    overage_resets_at=q.get("overageResetsAt"))
                if reset is None:
                    return None       # a limit nobody can schedule against
                found = {"resets_at": reset, "kind": q.get("rateLimitType"),
                         "hit_at": rec.get("timestamp"),
                         "last_at": rec.get("timestamp")}
            else:
                found["hit_at"] = rec.get("timestamp") or found["hit_at"]
            continue
        # A user record.
        if rec.get("isCompactSummary") or _has_tool_result(rec):
            break
        if _passes_by(rec):
            continue
        if found is None:
            return None               # a prompt after the error, unanswered
        break                         # the prompt the limit stopped
    return found


def _prompt_of(rec: dict) -> str | None:
    """The text of *rec* if it is a prompt the user gave, else None.

    Not one: anything the CLI, a hook, a task or a peer sent; a command; a
    tool result; a compaction summary; harness prompts such as a loop's
    wakeup -- and an earlier resume after a limit, which is what makes a
    resume that runs into a limit again repeat the user's prompt rather than
    itself.
    """
    if rec.get("type") != "user" or rec.get("isSidechain") \
            or rec.get("isCompactSummary") or _has_tool_result(rec) \
            or _is_cli_resume_bookkeeping(rec) or _passes_by(rec):
        return None
    text = _record_text(rec.get("message"))
    if not text or _classify_user_text(text) != "user":
        return None
    return text


def last_prompt_in(jsonl: Path, *,
                   limit: int = TURN_START_SEARCH_BYTES,
                   chunk: int = CUT_OFF_TAIL_BYTES) -> dict | None:
    """The last prompt the user gave in *jsonl*: ``{"text", "at"}``, or None.

    Read backwards in chunks, at most *limit* bytes: a lane's prompt can be
    hours of tool calls back.  Only user records that are not tool results
    are parsed on the way.
    """
    try:
        end = jsonl.stat().st_size
        f = open(jsonl, "rb")
    except OSError:
        return None
    with f:
        floor = max(0, end - limit)
        carry = b""                   # the start of a line cut by the chunk
        pos = end
        while pos > floor:
            begin = max(floor, pos - chunk)
            f.seek(begin)
            raw = f.read(pos - begin) + carry
            lines = raw.split(b"\n")
            carry = lines[0] if begin > floor else b""
            if begin > floor:
                lines = lines[1:]
            elif begin > 0:
                lines = lines[1:]     # cut in half by the limit
            for line in reversed(lines):
                if b'"tool_use_id"' in line or not _USER_RECORD.search(line):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    text = _prompt_of(rec)
                    if text is not None:
                        return {"text": text, "at": rec.get("timestamp")}
            pos = begin
    return None


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def when(ts: float | None, now: datetime | None = None) -> str | None:
    """Unix time *ts* as the user's clock reads it, the way session.py writes
    a transcript's times: "6:00 PM", or "Oct 23, 6:00 PM" on another day."""
    if not _is_time(ts):
        return None
    iso = datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
    return _local_when(iso, now)


def build_resume_prompt(*, kind: str | None, hit_at: str | None,
                        resets_at: float | None, prompt: dict | None,
                        now: datetime | None = None) -> str:
    """The prompt that carries a session on once its limit has reset.

    *prompt* is :func:`last_prompt_in`'s: the user's last prompt, repeated
    word for word.  Repeated whether or not compaction has run since -- the
    rolling context trim can drop it as well, and when neither has, a copy
    costs little -- and framed as a repeat, so the model does not start the
    work over.
    """
    stopped = _local_when(hit_at, now)
    reset = when(resets_at, now)
    what = f"Your {limit_name(kind)} stopped this session"
    what += f" at {stopped}" if stopped else ""
    what += f"; it reset at {reset}." if reset else "; it has reset."
    parts = [
        f"{PROMPT_PREFIX}, so this session is carrying on by itself. {what} "
        "Nobody typed this message: orchestrator2 sends it when a session "
        "running with --resume-after-limit is stopped by a usage limit.",
        "Pick the work back up where the limit stopped it, and check what is "
        "already done before doing anything again. Anything that arrived "
        "while the session was stopped, such as a background task finishing "
        "or a message from another session, is in the conversation above.",
    ]
    text = (prompt or {}).get("text")
    if isinstance(text, str) and text.strip():
        if len(text) > REPEAT_MAX_CHARS:
            rest = len(text) - REPEAT_MAX_CHARS
            text = (text[:REPEAT_MAX_CHARS]
                    + f"\n[... {rest} more characters of this prompt are not "
                      f"repeated here]")
        sent = _local_when((prompt or {}).get("at"), now)
        attr = f' sent="{sent}"' if sent else ""
        parts.append(
            "The last prompt you were given is repeated below, word for word, "
            "in case compaction has summarised it away. It is a repeat for "
            "reference, not a new request: do not redo any part of it that is "
            "already done.")
        parts.append(f"<repeated-prompt{attr}>\n{text}\n</repeated-prompt>")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# The schedule on disk
# ---------------------------------------------------------------------------

def _sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", s)


def _state_dir() -> Path:
    return account_dir() / "orchestrator2" / "limit-resumes"


def record_file_for(cwd: str, session_id: str) -> Path:
    """One file per session, named like the wakeup store's."""
    try:
        resolved = str(Path(cwd).resolve(strict=False))
    except OSError:
        resolved = cwd
    return _state_dir() / f"{_sanitize(resolved)}__{_sanitize(session_id)}.json"


def save(cwd: str, session_id: str | None, *, due_at: float, resets_at: float,
         config_dir: str | None = None) -> None:
    """Record that *session_id* waits on a limit until *due_at*.  Failures
    never stop the resume being armed in the session itself."""
    if not session_id or not _is_time(due_at):
        return
    path = record_file_for(cwd, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cwd": cwd, "session_id": session_id,
                       "config_dir": config_dir, "due_at": float(due_at),
                       "resets_at": float(resets_at), "saved_at": time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _valid(rec: Any) -> bool:
    return (isinstance(rec, dict)
            and isinstance(rec.get("session_id"), str) and rec["session_id"]
            and isinstance(rec.get("cwd"), str) and rec["cwd"]
            and _is_time(rec.get("due_at")))


def load(cwd: str, session_id: str | None) -> dict | None:
    """The record for *session_id* in *cwd*, if there is a valid one."""
    if not session_id:
        return None
    try:
        rec = json.loads(record_file_for(cwd, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if _valid(rec) else None


def clear(cwd: str, session_id: str | None) -> None:
    """Forget *session_id*'s record: it was resumed, or is no longer waiting."""
    if not session_id:
        return
    try:
        record_file_for(cwd, session_id).unlink()
    except OSError:
        pass


def iter_records() -> Iterator[dict]:
    """Every valid record on disk, soonest due first.  For the hub's start,
    which does not yet know which sessions are waiting."""
    try:
        paths = sorted(_state_dir().glob("*.json"))
    except OSError:
        return
    recs = []
    for path in paths:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # The name is derived from cwd and id, so a record in someone else's
        # slot has been moved or edited: reopening it would open another
        # session than the one that asked.
        if _valid(rec) and record_file_for(rec["cwd"], rec["session_id"]).name == path.name:
            recs.append(rec)
    recs.sort(key=lambda r: r["due_at"])
    yield from recs


class Plan(NamedTuple):
    """What the hub does with a record: reopen its session *delay* seconds
    from now, or drop it."""
    reopen: bool
    delay: float
    reason: str


def plan_reopen(rec: Any, *, now: float | None = None) -> Plan:
    """Decide what to do with one record.  Pure, so the rule that reopens a
    session nobody is watching is testable without a hub."""
    now = time.time() if now is None else now
    if not _valid(rec):
        return Plan(False, 0.0, "not a usable record")
    due = float(rec["due_at"])
    if due - now > MAX_AHEAD_S:
        return Plan(False, 0.0, "due further ahead than any limit lasts")
    if now - due > LATE_S:
        return Plan(False, 0.0,
                    f"{int((now - due) // 3600)} hours overdue, past the "
                    f"{int(LATE_S // 3600)} hour limit for resuming by itself")
    return Plan(True, max(0.0, due - now),
                f"due in {int(due - now)}s" if due > now
                else f"{int(now - due)}s overdue")
