"""Scheduled wakeups that outlive the hub process.

Reported 2026-09-20: two autonomous-loop sessions stopped days earlier and said
nothing.  The immediate cause -- the idle timer reaping a session that was
merely *waiting* -- is fixed in ``server._idle_teardown_after``.  This module
covers the other half: ``state.wakeup_at`` lived only in memory, so a hub
restart cancelled every scheduled loop just as silently.

Asked which behaviour a user would expect, and answered: "i guess my loops are
mostly unattended."  That settles a real trade.  Restoring a schedule only when
someone next *opens* the session would be the safe, queue-shaped design -- and
nearly useless here, because the entire point of those loops is that nobody is
watching.  So the hub resurrects them itself.

**This is the most dangerous thing in the codebase and it is worth saying why.**
A wakeup is not a reminder; it carries a prompt that gets *sent as a real turn*,
unattended, in a session the user may not have open.  Persisting the prompt
queue once caused exactly this shape of harm -- a brand-new session inherited an
older one's queued prompt and sent it immediately, as a turn, with no user
action, from an 18-day-old file still waiting to do it again (see
``session.load_persisted_queue``).  Every gate below exists because of that:

* **session-scoped, and the recorded id must match** -- a record is only ever
  restored into the exact session that wrote it;
* **a freshness cap** -- a record older than :data:`WAKEUP_MAX_AGE_S` is junk,
  not a schedule;
* **a lateness budget** -- a wakeup may fire late by at most *its own interval*
  (capped), because a loop that ticks every 20 minutes has no business firing
  eight hours late on a plan that has gone stale.  Past that it is reported as
  paused rather than run;
* **a resurrection cap** -- a boot must not spawn an unbounded number of CLIs
  on a machine whose commit limit has been exhausted before.

The lateness budget is derived rather than configured: ``due_at - armed_at`` is
the loop's own cadence, so a fast loop tolerates little lateness and a slow one
tolerates more, with no knob to get wrong.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator, NamedTuple

#: A record older than this is not a schedule, it is litter.
WAKEUP_MAX_AGE_S = 24 * 3600

#: Bounds on the derived lateness budget.  The floor keeps a very fast loop
#: from being declared stale by a few seconds of restart; the ceiling stops a
#: six-hour loop from firing six hours late.
MIN_LATE_S = 60.0
MAX_LATE_S = 3600.0

#: How many sessions one hub start may resurrect.  Each is a CLI process and a
#: transcript load; this machine has hit its Windows commit limit before.
MAX_RESURRECT = 4

ARM = "arm"          # still in the future: re-arm for the remaining time
FIRE = "fire"        # overdue but within its lateness budget: run it shortly
PAUSED = "paused"    # overdue beyond budget: surface it, do not run it
DISCARD = "discard"  # not a usable record at all


class Plan(NamedTuple):
    """What to do with one restored record."""
    action: str
    delay: float      # seconds from now, for ARM/FIRE
    reason: str       # human-readable, for the log and the banner


def _sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", s)


def _state_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else Path.home() / ".claude"
    return root / "orchestrator2" / "wakeups"


def wakeup_file_for(cwd: str, session_id: str) -> Path:
    """One file per session, named like the prompt queue's."""
    try:
        resolved = str(Path(cwd).resolve(strict=False))
    except OSError:
        resolved = cwd
    return _state_dir() / f"{_sanitize(resolved)}__{_sanitize(session_id)}.json"


def save_wakeup(cwd: str, session_id: str | None, *, due_at: float,
                prompt: str, armed_at: float | None = None,
                config_dir: str | None = None) -> None:
    """Record a scheduled wakeup.  Failures never break arming."""
    if not session_id or not prompt or not isinstance(due_at, (int, float)):
        return
    path = wakeup_file_for(cwd, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "cwd": cwd,
                "session_id": session_id,
                "config_dir": config_dir,
                "due_at": float(due_at),
                "armed_at": float(armed_at if armed_at is not None else time.time()),
                "prompt": prompt,
                "saved_at": time.time(),
            }, f)
        os.replace(tmp, path)
    except OSError:
        pass


def clear_wakeup(cwd: str, session_id: str | None) -> None:
    """Forget a wakeup, because it fired or was cancelled.

    Called on *every* exit from the armed state.  A record that outlives its
    wakeup is a turn waiting to be run twice.
    """
    if not session_id:
        return
    try:
        wakeup_file_for(cwd, session_id).unlink()
    except (OSError, FileNotFoundError):
        pass


def _valid(rec: Any) -> bool:
    return (isinstance(rec, dict)
            and isinstance(rec.get("session_id"), str) and rec["session_id"]
            and isinstance(rec.get("prompt"), str) and rec["prompt"].strip()
            and isinstance(rec.get("due_at"), (int, float))
            and isinstance(rec.get("saved_at"), (int, float))
            and isinstance(rec.get("cwd"), str) and rec["cwd"])


def iter_pending_wakeups() -> Iterator[dict]:
    """Every record on disk, newest-due first.

    Used at hub start, where we do not yet know which sessions exist -- the
    record is what tells us to bring one back.
    """
    try:
        paths = sorted(_state_dir().glob("*.json"))
    except OSError:
        return
    recs = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not _valid(rec):
            continue
        # The filename is derived from cwd+session_id, so a record whose
        # recorded id does not match its own slot has been tampered with or
        # moved.  Restoring it would run a turn in the wrong session.
        if wakeup_file_for(rec["cwd"], rec["session_id"]).name != path.name:
            continue
        recs.append(rec)
    recs.sort(key=lambda r: r.get("due_at", 0.0))
    yield from recs


def lateness_budget(rec: dict) -> float:
    """How late this wakeup may fire and still be worth running.

    Derived from the loop's own cadence (``due_at - armed_at``) rather than
    configured: a loop that ticks every few minutes should not fire an hour
    late, and one that ticks hourly should tolerate more than a minute.
    """
    interval = float(rec.get("due_at", 0.0)) - float(rec.get("armed_at", 0.0))
    if interval <= 0:
        interval = MIN_LATE_S
    return max(MIN_LATE_S, min(MAX_LATE_S, interval))


def plan_restore(rec: Any, *, now: float | None = None,
                 max_age_s: float = WAKEUP_MAX_AGE_S) -> Plan:
    """Decide what to do with one record.  Pure.

    The decision is deliberately separate from everything that acts on it, so
    the rule that can silently run an unattended turn is testable without a
    hub, a CLI or a clock.
    """
    now = time.time() if now is None else now
    if not _valid(rec):
        return Plan(DISCARD, 0.0, "not a usable record")
    if max_age_s > 0 and (now - float(rec["saved_at"])) > max_age_s:
        return Plan(DISCARD, 0.0, "older than the freshness cap")

    due = float(rec["due_at"])
    if due > now:
        return Plan(ARM, due - now, f"due in {int(due - now)}s")

    late = now - due
    budget = lateness_budget(rec)
    if late <= budget:
        return Plan(FIRE, 0.0, f"{int(late)}s late, within its {int(budget)}s budget")
    return Plan(
        PAUSED, 0.0,
        f"{int(late // 60)} minutes late — past its {int(budget // 60)} minute "
        f"budget, so it is paused rather than run",
    )
