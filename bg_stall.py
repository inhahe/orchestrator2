"""Telling a background task that is *working* from one that is hung.

A background task leaves the panel when the CLI says it finished. When the CLI
never says so, the row stays forever -- and measured across the whole log, every
single hub process accumulates them: 12 stuck of 5,577 started in one, 7 of
13,074 in another, 2 within the first 277 tasks of a fresh one. About one task
in a thousand, permanent, and cumulative, which is why the panel is reliably
wrong by a row or two even though the per-task rate is tiny.

**Those rows are not stale bookkeeping.** Reported 2026-09-16 -- "the 'lithic'
session is completely done, but background tasks shows 'Find every script that
writes into D:\\github'" -- and the task was still there in the OS::

    pid 34752  started 16:27:15  cpu_time=0.0s  status=running
        grep -rliE "[d]:.github" --include=*.bat ... .

A recursive grep that had burned **zero CPU in twenty minutes**, blocked on I/O,
with its output file frozen at 50 bytes since three seconds before the task was
even registered. The CLI sent no completion because there was none; the model
had read the partial output file and moved on. The panel, the CLI and the model
were all telling the truth about different things, and the disagreement was an
abandoned hung process nobody could see.

So this module does not decide a task is *finished* -- it never removes a row or
invents a completion. It decides whether a task is still *doing* anything, which
is a question we can actually answer:

* **output** -- the CLI streams the task's stdout to a file whose path we can
  resolve; a size/mtime that has not moved is a task producing nothing;
* **CPU** -- the task's own process, found among the CLI's descendants; zero CPU
  growth is a task computing nothing.

Only when *both* say "nothing, for a long time" is a task called ``STALLED``,
and only ``STALLED`` stops a task from holding open the things background work
is allowed to hold open (idle teardown, a deferred ``/model``, bg-wait parking).
If we cannot find the process we cannot prove the CPU half, so the worst we will
say is ``QUIET`` -- which annotates the row and changes nothing else. Guessing
in the other direction would reintroduce the bug this project keeps re-learning:
a session that *is* working, reaped because it looked idle.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

# How long nothing may happen before a task is called stalled.  Five minutes
# matches the idle-teardown grace: the same "long enough that a human would have
# noticed" scale, and long enough that a slow compile between log lines is not
# mistaken for a hang.
STALL_AFTER_S = 300.0

# How often to look.  Two stat calls and one process-tree walk; the 2 s status
# ticker would run it far more often than anything can change, and on Windows
# the tree walk is not free.
BG_PROBE_INTERVAL_S = 15.0

# What we will say about a task.
WORKING = "working"   # produced output or burned CPU recently
QUIET = "quiet"       # produced nothing for a while, but we cannot see its process
STALLED = "stalled"   # produced nothing *and* burned no CPU: proven hung


def _sanitize(s: str) -> str:
    """Match the CLI's path sanitiser (any non-alphanumeric to ``-``)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", s)


def task_output_path(cwd: str, session_id: str, task_id: str,
                     *, tempdir: str | None = None) -> Path:
    """Where the CLI streams *task_id*'s stdout.

    Derived rather than configured, because the CLI tells us this path only in
    the *completion* notification -- which is exactly the message a stuck task
    never sends.  The tool result that starts a task does state it in prose
    ("Output is being written to: ..."), so this layout is observable:

        %TEMP%/claude/<sanitised cwd>/<session id>/tasks/<task id>.output

    A wrong guess is safe: the file simply will not exist, the probe reports no
    output information, and the task can never be called STALLED on the CPU
    half alone.
    """
    base = Path(tempdir or tempfile.gettempdir())
    try:
        resolved = str(Path(cwd).resolve(strict=False))
    except OSError:
        resolved = cwd
    return (base / "claude" / _sanitize(resolved) / str(session_id)
            / "tasks" / f"{task_id}.output")


def read_output_signature(path: Path) -> tuple[int, float] | None:
    """``(size, mtime)`` for the task's output file, or None if unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime)


def _normalise_cmd(text: str) -> str:
    """Flatten a command line so wrapping and quoting stop mattering.

    The CLI does not run the recorded command verbatim.  It wraps it -- shell
    snapshot sourcing, TEMP exports, ``eval 'cd "..." && <command>'`` -- and the
    layers rewrite quoting on the way through.  Measured against a live task,
    the recorded command appeared in the process's command line with its quotes
    stripped, its backslashes flipped and its newlines turned into spaces, so a
    literal substring test found nothing while the command was plainly there.

    Dropping quotes, unifying slashes and collapsing whitespace makes the
    comparison survive all of that.  It cannot create a false match on its own:
    what is left is still a long, highly specific run of the user's own text.
    """
    text = text.replace("\\", "/")
    text = re.sub(r"[\"']", "", text)
    return re.sub(r"\s+", " ", text).strip()


def find_task_process(command: str | None,
                      descendants: Iterable[Any]) -> Any | None:
    """Find the process running *command* among the CLI's descendants.

    The CLI does not tell us a task's pid, so the command line is the only
    handle we have -- normalised on both sides, because the wrapping layers
    rewrite quoting (see :func:`_normalise_cmd`).

    When several processes match they are the task's own wrapper chain
    (``bash`` calling ``bash`` calling the worker), and the **outermost** is
    the right one: :func:`process_activity` sums a whole subtree, so the
    ancestor covers every layer exactly once, while an inner one would miss any
    sibling doing the work.  An earlier version took the youngest to avoid
    double-counting, which was reasoning left over from when only the matched
    process was measured.
    """
    if not command:
        return None
    needle = _normalise_cmd(command)
    if len(needle) < 12:
        # Too short to identify anything; a bare "make" would match half the
        # tree.  Better to report no process than the wrong one.
        return None
    needle = needle[:120]
    hits = []
    for proc in descendants:
        try:
            cmdline = " ".join(proc.cmdline() or [])
        except Exception:
            continue
        if needle in _normalise_cmd(cmdline):
            hits.append(proc)
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    try:
        return min(hits, key=lambda p: p.create_time())
    except Exception:
        return None


def process_activity(proc: Any) -> tuple[float, int] | None:
    """``(cpu_seconds, io_operations)`` for *proc* and everything under it.

    Returns None only when the process itself cannot be read -- which means "we
    lost track of it", not "it is doing nothing", and must never be confused
    with zero.

    **The descendants are the point.**  What we match is the task's *shell*, and
    a shell sleeps while a child does the work -- ``python train.py``, a
    compiler, a test runner.  Reading the shell alone reports a flat zero for a
    task working hard.  Found by probing a live task rather than a fixture: the
    shell sat at 0.015 s while its children did everything.

    **I/O is the third signal because CPU misses a whole class of work.**  A
    task copying a large file, walking a huge tree or pulling over the network
    burns almost no CPU and may write nothing to its own output file for
    minutes, while moving gigabytes.  Judged on output and CPU alone it looks
    identical to a task blocked forever.  ``io_counters`` separates them: a
    process that is *doing* I/O has climbing read/write counts, and one that is
    *blocked* on I/O does not.

    Counted in operations rather than bytes: a task reading the same cached
    page in a loop makes no byte progress but is unmistakably alive, and the
    count is what moves.

    Walks the tree once for both numbers.  The walk is the expensive part on
    Windows, and doing it twice per task per poll is the kind of cost that ends
    up being measured later rather than avoided now.
    """
    if proc is None:
        return None

    def _sample(p: Any) -> tuple[float, int]:
        cpu = 0.0
        ops = 0
        try:
            t = p.cpu_times()
            cpu = float(t[0]) + float(t[1])
        except Exception:
            pass
        try:
            io = p.io_counters()
            ops = int(io.read_count) + int(io.write_count)
        except Exception:
            pass
        return cpu, ops

    try:
        proc.cpu_times()          # the liveness check: can we read it at all?
    except Exception:
        return None
    cpu_total, io_total = _sample(proc)
    try:
        children = proc.children(recursive=True)
    except Exception:
        return cpu_total, io_total
    for child in children:
        c_cpu, c_io = _sample(child)
        cpu_total += c_cpu
        io_total += c_io
    return cpu_total, io_total


def update_probe(entry: dict, *, now: float,
                 output_sig: tuple[int, float] | None,
                 cpu_s: float | None,
                 io_ops: int | None = None) -> None:
    """Fold one observation into *entry*'s stall bookkeeping.

    Records *when each signal last changed* rather than the raw values, because
    "unchanged since" is the only thing the decision needs and it survives a
    poll interval that varies.  Deliberately monotonic-clock based: a task's
    liveness must not be affected by the wall clock moving.
    """
    if "stall_started_at" not in entry:
        entry["stall_started_at"] = now

    if output_sig != entry.get("output_sig"):
        entry["output_sig"] = output_sig
        entry["output_changed_at"] = now
    entry.setdefault("output_changed_at", now)

    # CPU only ever increases, so "changed" means "increased".  Comparing for
    # inequality instead would let a process that *lost* a thread look busy.
    prev = entry.get("cpu_s")
    if cpu_s is not None and (prev is None or cpu_s > prev):
        entry["cpu_changed_at"] = now
    if cpu_s is not None:
        entry["cpu_s"] = cpu_s
    entry.setdefault("cpu_changed_at", now)

    # I/O operations are a cumulative counter too, so the same "only counts if
    # it went up" rule applies.
    prev_io = entry.get("io_ops")
    if io_ops is not None and (prev_io is None or io_ops > prev_io):
        entry["io_changed_at"] = now
    if io_ops is not None:
        entry["io_ops"] = io_ops
    entry.setdefault("io_changed_at", now)

    # One flag for both process-derived signals: they come from the same walk,
    # so they are known or unknown together.
    entry["proc_known"] = cpu_s is not None


def quiet_for(entry: dict, *, now: float) -> float:
    """Seconds since this task last did anything we could observe."""
    # max, not min: quiet means *neither* signal has moved, so the clock runs
    # from the most recent evidence of activity.  Taking the older of the two
    # would call a task stalled while its output file was still growing.
    quiet_since = max(
        entry.get("output_changed_at", now),
        entry.get("cpu_changed_at", now),
        entry.get("io_changed_at", now),
    )
    return max(0.0, now - quiet_since)


def stall_state(entry: dict, *, now: float,
                stall_after: float = STALL_AFTER_S) -> str:
    """WORKING, QUIET or STALLED for one task.

    STALLED requires *both* halves and a process we could actually see.  A task
    we have merely lost track of is QUIET: it annotates the row and nothing
    more, because "I cannot find its process" is a statement about us, not
    about the task.
    """
    if not entry.get("stall_probed"):
        # Never probed: we know nothing, so claim nothing.
        return WORKING
    if quiet_for(entry, now=now) < stall_after:
        return WORKING
    if not entry.get("proc_known"):
        return QUIET
    return STALLED


def _fmt_age(seconds: float) -> str:
    mins = int(seconds // 60)
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h{mins % 60:02d}m"


def describe_stall(entry: dict, *, now: float,
                   stall_after: float = STALL_AFTER_S) -> str | None:
    """The phrase the panel puts on the row, or None when it is working.

    Says what was observed, not what it means.  "no output or CPU for 20m" is
    checkable by the reader; "hung" is a conclusion they did not ask us to
    reach, and would be wrong for a task legitimately blocked on something that
    is about to arrive.
    """
    state = stall_state(entry, now=now, stall_after=stall_after)
    if state == WORKING:
        return None
    age = _fmt_age(quiet_for(entry, now=now))
    if state == STALLED:
        return f"no output, CPU or disk I/O for {age}"
    return f"no output for {age}"


def is_load_bearing(entry: dict, *, now: float,
                    stall_after: float = STALL_AFTER_S) -> bool:
    """Whether this task may still hold the session's gates open.

    Background work is allowed to defer an idle teardown, a ``/model`` reconnect
    and a CLI recycle, which is right: closing the tab is exactly what you do
    when work should carry on without you.  A *proven* hang is the case that
    reasoning does not cover -- the session is pinned by something burning no
    CPU and writing nothing, and would stay pinned until the hub died.
    """
    return stall_state(entry, now=now, stall_after=stall_after) != STALLED


def active_tasks(tasks: dict, *, now: float,
                 stall_after: float = STALL_AFTER_S) -> dict:
    """The subset of *tasks* that still counts as running."""
    return {
        tid: entry for tid, entry in (tasks or {}).items()
        if is_load_bearing(entry, now=now, stall_after=stall_after)
    }
