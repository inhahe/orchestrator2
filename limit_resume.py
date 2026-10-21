"""Carrying a session on by itself once a usage limit resets.

``--resume-after-limit`` (or ``/resume-after-limit on``): when a subscription
usage limit -- the 5-hour session limit, a weekly one -- stops a session, it is
sent a prompt once the limit has reset, and carries on.  Off by default: a
session that starts working with nobody having typed anything is the shape of
every "why is it doing that?" report this project has had (design.md §6e).

Live, the bridge knows a session is stopped by the State's rate-limit
rejection, which the CLI's ``RateLimitEvent`` sets (``state.apply_rate_limit_
info``, the status bar's "rate limited").  This module is the part that needs
no bridge, so it can be tested without one:

* **What stopped a session that is being opened**, from its transcript
  (:func:`limit_stop_in`), where the State has nothing yet.  The CLI writes
  each limit it hits as a synthetic assistant record carrying
  ``error: "rate_limit"`` and the API's quota headers, ``resetsAt`` among
  them, a Unix time.  A ``/compact`` that runs into the limit leaves only its
  error text, "Error during compaction: You've hit your session limit ·
  resets 9:30am (America/New_York)", and the reset is read from that.
* **The last prompt it was given** (:func:`last_prompt_in`), which the resume
  repeats word for word: by the time a long turn hits a limit, compaction has
  very likely reduced it to a summary.
* **The resume prompt** itself (:func:`build_resume_prompt`).
* **The schedule**, on disk (``save`` / ``load`` / ``clear``), so that a
  session waiting on a reset survives the hub restarting and the idle teardown
  -- which closes it rather than keep a CLI alive for the hours or days a limit
  can take.  ``server`` reopens it when it is due.  The record says *which*
  session, when, and what it ran with; the transcript decides, when it is
  reopened, whether there is still anything to resume.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from account import account_dir
from record_store import RecordStore
from session import (
    _COMMAND_RECORD_PREFIXES,
    _USER_RECORD,
    CUT_OFF_TAIL_BYTES,
    LIMIT_RESUME_PROMPT_PREFIX,
    SESSION_NOTE_PREFIX,
    TURN_START_SEARCH_BYTES,
    _classify_user_text,
    _has_tool_result,
    _is_cli_resume_bookkeeping,
    _local_when,
    _record_text,
    read_tail,
)

#: How long after the reset the session is resumed.  The limit is the API's
#: to lift, and a prompt sent on the second it was due can still be refused.
MARGIN_S = 60.0

#: How long a session reopened after its reset has passed waits before being
#: resumed, so the hub, its tabs and the CLI have settled first.
SETTLE_S = 20.0

#: How late, past the reset, a session opened then is still resumed by
#: itself.  Later than that the work it was doing is half a day old, and
#: opening it -- to look at it, say -- should not set it going.
#: ``/resume-after-limit on`` still resumes it: that is asking.
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

#: How many times the hub may reopen a session for one record without the
#: session scheduling its resume (which writes a fresh record).  A session
#: that cannot connect -- its login expired, or it is open in a terminal --
#: would otherwise be reopened by every idle teardown until the record went
#: stale.
MAX_REOPENS = 2

#: The longest prompt repeated in full.  Past this it is very likely a pasted
#: log rather than instructions, and the instructions are usually at the top.
REPEAT_MAX_CHARS = 20_000

#: How every resume prompt begins.  One of session.py's harness prefixes, so
#: history shows it as the orchestrator's and not as something the user typed,
#: and so :func:`last_prompt_in` passes over an earlier resume.
PROMPT_PREFIX = LIMIT_RESUME_PROMPT_PREFIX

#: How the CLI's message for a usage limit that stopped something begins
#: (``rateLimitMessages.getLimitReachedText``).  Only the error states: its
#: warnings ("You've used 90% ...") stop nothing.
LIMIT_TEXT_PREFIXES = ("You've hit your", "You're out of extra usage")

#: The CLI's names for each window, for the user and the model.
_LIMIT_NAMES = {
    "five_hour": "5-hour session limit",
    "seven_day": "weekly limit",
    "seven_day_opus": "weekly Opus limit",
    "seven_day_sonnet": "weekly Sonnet limit",
    "overage": "extra-usage limit",
}

#: The window each of the CLI's limit messages names, for a message that comes
#: with no quota headers.  Longest first: "weekly limit" is not "Opus limit".
_KIND_BY_TEXT = (
    ("session limit", "five_hour"),
    ("Opus limit", "seven_day_opus"),
    ("Sonnet limit", "seven_day_sonnet"),
    ("weekly limit", "seven_day"),
    ("out of extra usage", "overage"),
)


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


def _epoch(iso: Any) -> float | None:
    """A transcript timestamp as a Unix time."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# A reset read from the CLI's text
# ---------------------------------------------------------------------------

#: The CLI's "resets ..." (``formatResetTime(resetsAt, true)``): a clock time
#: for a reset within a day, "Oct 23, 9am" beyond, a year when it is not this
#: one's, then the time zone it was written in.
_RESET_TEXT_RE = re.compile(
    r"resets (?:(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{1,2}),? (?:(?P<year>\d{4}),? )?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s?(?P<ampm>[ap]m)"
    r"(?: \((?P<tz>[^)]+)\))?",
    re.IGNORECASE)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def _zone(name: str | None):
    """The IANA zone *name*, or None (local time) when it cannot be had --
    Windows Python has the zones only with the ``tzdata`` package."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def reset_from_text(text: str, at: float) -> int | None:
    """The reset a CLI limit message written at Unix time *at* names, or None.

    Only for a message with no quota headers to read it from.  A clock time
    alone is the first such time after *at*; a date without a year is this
    year's, or next year's when that is already past.
    """
    m = _RESET_TEXT_RE.search(text or "")
    if not m:
        return None
    tz = _zone(m.group("tz"))
    base = (datetime.fromtimestamp(at, tz) if tz is not None
            else datetime.fromtimestamp(at).astimezone())
    hour = int(m.group("hour")) % 12 + (12 if m.group("ampm").lower() == "pm" else 0)
    minute = int(m.group("minute") or 0)
    try:
        if m.group("mon"):
            month = _MONTHS.index(m.group("mon").lower()) + 1
            year = int(m.group("year")) if m.group("year") else base.year
            when_ = base.replace(year=year, month=month, day=int(m.group("day")),
                                 hour=hour, minute=minute, second=0, microsecond=0)
            if not m.group("year") and when_ < base - timedelta(days=1):
                when_ = when_.replace(year=year + 1)
        else:
            when_ = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when_ <= base:
                when_ += timedelta(days=1)
    except ValueError:
        return None
    return int(when_.timestamp())


def _kind_from_text(text: str) -> str | None:
    for words, kind in _KIND_BY_TEXT:
        if words in text:
            return kind
    return None


# ---------------------------------------------------------------------------
# Reading the transcript
# ---------------------------------------------------------------------------

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


def _compaction_limit_text(rec: dict) -> str | None:
    """The limit message in a ``/compact`` that ran into a usage limit -- a
    ``local_command`` record, "Error during compaction: You've hit your ..."
    -- or None for any other system record."""
    if rec.get("type") != "system" or rec.get("subtype") != "local_command":
        return None
    content = rec.get("content")
    if not isinstance(content, str):
        return None
    text = re.sub(r"</?local-command-std(?:err|out)>", "", content).strip()
    text = text.removeprefix("Error during compaction:").strip()
    return text if text.startswith(LIMIT_TEXT_PREFIXES) else None


def _passes_by(rec: dict) -> bool:
    """A user record that arrived while the session was stopped and does not
    mean it moved on: a background task's notification, a peer's message,
    something the CLI or a hook added, a local command and its caveat.

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
                  tail: tuple[list[dict], int] | None = None) -> dict | None:
    """Whether *jsonl*'s session was last stopped by a usage limit, and which.

    None unless its last word is the CLI reporting a usage limit with a reset
    that can be read: a limit error, or a ``/compact`` that ran into one.
    Looked past on the way, as the user asked: whatever arrived while the
    session was stopped (:func:`_passes_by`), other commands' output, and the
    CLI's resume bookkeeping.  A prompt the user sent after it, or any reply
    from the model, means the session moved on.  Otherwise::

        {"resets_at": <Unix time the limit lets it go again>,
         "kind": <the CLI's rateLimitType, e.g. "five_hour", or None>,
         "hit_at": <Unix time of the first report of the run that stopped it>}

    The reset and kind are the latest report's: a later limit (the weekly one
    after the session limit) is the one that still holds.  But a reset read
    from text gives way to an earlier report of the same limit with quota
    headers, which say it to the second.  *tail* is ``session.read_tail``'s,
    for a caller that has read it already.
    """
    records, _start = tail if tail is not None else read_tail(jsonl)
    found: dict | None = None
    exact = False                     # whether found's reset is a header's

    def report(at: float | None, reset: int | None, kind: str | None,
               from_headers: bool) -> None:
        nonlocal found, exact
        if found is None:
            found = {"resets_at": reset, "kind": kind, "hit_at": at}
            exact = from_headers and bool(reset)
            return
        if reset and (not found["resets_at"]
                      or (from_headers and not exact
                          and kind == found["kind"])):
            found["resets_at"], found["kind"] = reset, kind
            exact = from_headers
        found["hit_at"] = at or found["hit_at"]

    for i in range(len(records) - 1, -1, -1):
        rec = records[i]
        if _is_cli_resume_bookkeeping(rec):
            continue
        t = rec.get("type")
        at = _epoch(rec.get("timestamp"))
        if t == "system":
            text = _compaction_limit_text(rec)
            if text is not None:
                report(at, reset_from_text(text, at) if at else None,
                       _kind_from_text(text), False)
            continue                  # a boundary, another command's output
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
            reset = effective_reset(
                status=q.get("status"), resets_at=q.get("resetsAt"),
                overage_resets_at=q.get("overageResetsAt"))
            from_headers = reset is not None
            if reset is None and q.get("status") != "allowed" and at:
                reset = reset_from_text(_record_text(rec.get("message")), at)
            report(at, reset, q.get("rateLimitType")
                   or _kind_from_text(_record_text(rec.get("message"))),
                   from_headers)
            continue
        # A user record.
        if rec.get("isCompactSummary") or _has_tool_result(rec):
            break
        if _passes_by(rec):
            continue
        if found is None:
            return None               # a prompt after the error, unanswered
        break                         # the prompt the limit stopped
    if found is None or not found["resets_at"]:
        return None
    return found


def _prompt_of(rec: dict) -> str | None:
    """The text of *rec* if it is a prompt the user gave, else None.

    Not one: anything the CLI, a hook, a task or a peer sent; a command; a
    tool result; a compaction summary; harness prompts such as a loop's
    wakeup -- and an earlier resume after a limit, which is what makes a
    resume that runs into a limit again repeat the user's prompt rather than
    itself.  A one-shot session note the hub put in front of a prompt
    (``SDKBridge._with_session_note``) is not part of it.
    """
    if rec.get("type") != "user" or rec.get("isSidechain") \
            or rec.get("isCompactSummary") or _has_tool_result(rec) \
            or _is_cli_resume_bookkeeping(rec) or _passes_by(rec):
        return None
    text = _record_text(rec.get("message"))
    if text.startswith(SESSION_NOTE_PREFIX) and not text.startswith(PROMPT_PREFIX):
        _note, sep, rest = text.partition("\n\n")
        text = rest.strip() if sep else text
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


def build_resume_prompt(*, kind: str | None, hit_at: float | None,
                        resets_at: float | None, prompt: dict | None,
                        now: datetime | None = None) -> str:
    """The prompt that carries a session on once its limit has reset.

    *prompt* is :func:`last_prompt_in`'s: the user's last prompt, repeated
    word for word.  Repeated every time: compaction keeps only a turn's
    in-flight tail as written (measured: the last tool call and its result),
    so a prompt that started a long turn is very likely only in its summary
    by now, and the rolling context trim can drop it too; when neither has
    run, a copy costs little.  Framed as a repeat, so the model does not start
    the work over.
    """
    stopped = when(hit_at, now)
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

def _state_dir() -> Path:
    return account_dir() / "orchestrator2" / "limit-resumes"


def _valid(rec: Any) -> bool:
    return (isinstance(rec, dict)
            and isinstance(rec.get("session_id"), str) and rec["session_id"]
            and isinstance(rec.get("cwd"), str) and rec["cwd"]
            and _is_time(rec.get("due_at")))


#: Looked up through the module each time, so a test can move the directory.
_store = RecordStore(lambda: _state_dir(), _valid)

#: The per-session settings a record keeps, so a session the hub reopens runs
#: as it did -- on the model, effort and Claude Code binary it had, ringing
#: the bell it rang -- and not on the hub's.
SETTINGS = ("model", "effort", "cli_path", "bell_on")


def save(cwd: str, session_id: str | None, *, due_at: float, resets_at: float,
         config_dir: str | None = None,
         settings: dict[str, Any] | None = None, reopens: int = 0) -> None:
    """Record that *session_id* waits on a limit until *due_at*.  Failures
    never stop the resume being armed in the session itself."""
    if not session_id or not _is_time(due_at):
        return
    fields: dict[str, Any] = {"config_dir": config_dir, "due_at": float(due_at),
                              "resets_at": float(resets_at), "reopens": int(reopens)}
    for key in SETTINGS:
        value = (settings or {}).get(key)
        if isinstance(value, str) and value:
            fields[key] = value
    _store.save(cwd, session_id, fields)


def load(cwd: str, session_id: str | None) -> dict | None:
    """The record for *session_id* in *cwd*, if there is a valid one."""
    return _store.load(cwd, session_id)


def clear(cwd: str, session_id: str | None) -> None:
    """Forget *session_id*'s record: it was resumed, or is no longer waiting."""
    _store.clear(cwd, session_id)


def rekey(cwd: str, old_id: str | None, new_id: str | None) -> bool:
    """Move a record to the id its session now has (``RecordStore.rekey``)."""
    return _store.rekey(cwd, old_id, new_id)


def note_reopened(rec: dict) -> None:
    """Count one reopen of *rec*'s session by the hub (``MAX_REOPENS``)."""
    fields = {k: v for k, v in rec.items()
              if k not in ("cwd", "session_id", "saved_at")}
    fields["reopens"] = int(rec.get("reopens") or 0) + 1
    _store.save(rec["cwd"], rec["session_id"], fields)


def iter_records() -> Iterator[dict]:
    """Every valid record on disk, soonest due first.  For the hub's start,
    which does not yet know which sessions are waiting."""
    recs = _store.records()
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
    if int(rec.get("reopens") or 0) >= MAX_REOPENS:
        return Plan(False, 0.0,
                    f"reopened {rec.get('reopens')} times without resuming — "
                    f"it may not be able to connect")
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
