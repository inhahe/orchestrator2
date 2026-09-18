"""Telling a background task that is working from one that is hung.

Reported 2026-09-16: "it still happens all the time that i still see a
background task in the pane but the model claims it's done with everything. for
example, right now, the 'lithic' session is completely done, but background
tasks shows 'Find every script that writes into D:\\github'".

I had answered an earlier version of this with an event rate -- 44 unmatched
starts out of 50,886 -- which was true and beside the point. A stuck row never
clears, so a 0.1% event rate is a panel that is permanently wrong. Measured per
hub process, **twelve of twelve had stuck rows**: 12 of 5,577 started in one,
7 of 13,074 in another, and 2 within the first 277 of a freshly restarted one.

The rows were also not what I assumed. The reported task was still there::

    pid 34752  started 16:27:15  cpu_time=0.0s  status=running
        grep -rliE "[d]:.github" --include=*.bat ... .

Zero CPU in twenty minutes, output file frozen at 50 bytes since three seconds
before the task was registered. The panel, the CLI and the model were each
telling the truth about different things; the disagreement was an abandoned
hung process. So this is not stale bookkeeping to clean up -- it is a real
process nobody could see, and the panel was the only thing reporting it.

These tests pin the two halves of the decision: what we are willing to *say*
(an observation, never a completion) and what we let it *change* (a proven-dead
task stops holding the session's gates open, and nothing else).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bg_stall import (                                    # noqa: E402
    QUIET,
    STALLED,
    STALL_AFTER_S,
    WORKING,
    active_tasks,
    describe_stall,
    find_task_process,
    is_load_bearing,
    quiet_for,
    stall_state,
    task_output_path,
    update_probe,
)


class FakeProc:
    """Just the psutil surface bg_stall touches."""

    def __init__(self, pid, cmdline, created=0.0, cpu=(0.0, 0.0), kids=(),
                 io=(0, 0)):
        self.pid = pid
        self._cmdline = cmdline
        self._created = created
        self._cpu = cpu
        self._kids = list(kids)
        self._io = io

    def cmdline(self):
        return self._cmdline

    def create_time(self):
        return self._created

    def cpu_times(self):
        return self._cpu

    def children(self, recursive=False):
        return self._kids

    def io_counters(self):
        class _IO:
            read_count = self._io[0]
            write_count = self._io[1]
        return _IO()


def _probed(*, output_sig=(0, 0.0), cpu_s=0.0, io_ops=0, at=0.0):
    """An entry that has been probed once at *at*."""
    entry = {"stall_probed": True}
    update_probe(entry, now=at, output_sig=output_sig, cpu_s=cpu_s,
                 io_ops=io_ops)
    return entry


# --------------------------------------------------------------------------
# What counts as alive
# --------------------------------------------------------------------------

def test_a_task_writing_output_is_working():
    entry = _probed(output_sig=(10, 1.0), cpu_s=0.0, at=0.0)
    update_probe(entry, now=600.0, output_sig=(99, 2.0), cpu_s=0.0, io_ops=0)

    assert stall_state(entry, now=600.0) == WORKING


def test_a_task_burning_cpu_is_working():
    """A compile that buffers its output writes nothing for minutes. Judging on
    output alone would call it hung and release every gate holding it."""
    entry = _probed(output_sig=(10, 1.0), cpu_s=0.0, at=0.0)
    update_probe(entry, now=600.0, output_sig=(10, 1.0), cpu_s=42.0, io_ops=0)

    assert stall_state(entry, now=600.0) == WORKING


def test_neither_for_long_enough_is_stalled():
    """The reported case: zero CPU, frozen output file."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)

    assert stall_state(entry, now=1200.0) == STALLED


def test_a_brief_quiet_spell_is_not_a_stall():
    """Tasks pause. Five minutes of nothing is the bar, not five seconds."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, at=0.0)
    update_probe(entry, now=30.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)

    assert stall_state(entry, now=30.0) == WORKING


def test_an_unprobed_task_is_never_judged():
    """Before the first probe we know nothing, so we claim nothing -- a task
    must not be called stalled merely because we have not looked yet."""
    assert stall_state({}, now=10_000.0) == WORKING


def test_cpu_we_cannot_read_is_only_quiet():
    """If we cannot find the process we cannot prove the CPU half. "I lost
    track of it" is a statement about us, not about the task, and must not
    release the gates."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=None, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=None, io_ops=None)

    assert stall_state(entry, now=1200.0) == QUIET


def test_cpu_going_backwards_does_not_count_as_progress():
    """CPU time only increases. Testing for *inequality* would let a sampling
    wobble, or a process that shed a thread, read as work."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=10.0, at=0.0)
    update_probe(entry, now=600.0, output_sig=(50, 1.0), cpu_s=9.0, io_ops=0)

    assert stall_state(entry, now=600.0) == STALLED


def test_a_task_that_resumes_is_working_again():
    """Nothing here is a one-way door: a blocked task that gets its lock back
    must stop being called stalled."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)
    assert stall_state(entry, now=1200.0) == STALLED

    update_probe(entry, now=1210.0, output_sig=(90, 2.0), cpu_s=1.0, io_ops=0)

    assert stall_state(entry, now=1210.0) == WORKING


# --------------------------------------------------------------------------
# What we say about it
# --------------------------------------------------------------------------

def test_a_working_task_gets_no_note():
    entry = _probed(at=0.0)

    assert describe_stall(entry, now=10.0) is None


def test_the_note_reports_an_observation_not_a_verdict():
    """"hung" is a conclusion we cannot prove for a task that may be blocked on
    something about to arrive. "no output or CPU for 20m" is checkable."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)

    note = describe_stall(entry, now=1200.0)

    assert note == "no output, CPU or disk I/O for 20m"
    for word in ("hung", "dead", "stuck", "failed"):
        assert word not in note


def test_the_quiet_note_does_not_claim_the_cpu_half():
    entry = _probed(output_sig=(50, 1.0), cpu_s=None, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=None, io_ops=None)

    note = describe_stall(entry, now=1200.0)

    assert note == "no output for 20m"
    assert "CPU" not in note


def test_long_quiet_spells_read_in_hours():
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, at=0.0)
    update_probe(entry, now=7500.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)

    assert describe_stall(entry, now=7500.0) == "no output, CPU or disk I/O for 2h05m"


def test_quiet_for_never_goes_negative():
    """The monotonic clock is the input; a caller passing an older `now` must
    not produce a negative age in the UI."""
    entry = _probed(at=100.0)

    assert quiet_for(entry, now=50.0) == 0.0


# --------------------------------------------------------------------------
# What it changes: the gates
# --------------------------------------------------------------------------

def test_a_stalled_task_stops_holding_the_gates():
    """Idle teardown, a deferred /model, a CLI recycle, the context trim and
    bg-wait parking all defer to background work. A task burning no CPU and
    writing nothing would pin all five until the hub died."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)

    assert is_load_bearing(entry, now=1200.0) is False


def test_a_quiet_task_still_holds_them():
    """Only a *proven* stall releases a gate. Releasing on "we lost its
    process" would reintroduce the bug this project keeps re-learning: a
    session that is working, reaped because it looked idle."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=None, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=None, io_ops=None)

    assert is_load_bearing(entry, now=1200.0) is True


def test_a_working_task_holds_them():
    entry = _probed(at=0.0)

    assert is_load_bearing(entry, now=10.0) is True


def test_active_tasks_keeps_the_row_but_drops_the_hold():
    """The registry is never edited: we can prove a task is doing nothing, not
    that it is over, so the row stays on screen and only stops counting."""
    live = _probed(at=0.0)
    update_probe(live, now=1200.0, output_sig=(999, 9.0), cpu_s=5.0, io_ops=1)
    dead = _probed(output_sig=(50, 1.0), cpu_s=0.0, at=0.0)
    update_probe(dead, now=1200.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)
    tasks = {"live": live, "dead": dead}

    assert set(active_tasks(tasks, now=1200.0)) == {"live"}
    assert set(tasks) == {"live", "dead"}, "the registry must not be edited"


# --------------------------------------------------------------------------
# Finding the task's process
# --------------------------------------------------------------------------

def test_the_process_is_found_inside_the_wrapped_command_line():
    """The CLI wraps the command (shell snapshot sourcing, cwd write-back), so
    the recorded command appears *within* a much longer line."""
    procs = [FakeProc(1, ["bash", "-c", "source /snap && grep -rliE needle . && pwd"])]

    found = find_task_process("grep -rliE needle .", procs)

    assert found is procs[0]


def test_a_wrapper_chain_resolves_to_the_outermost():
    """Several matches are the task's own wrapper chain (bash calling bash
    calling the worker). process_activity sums a whole subtree, so the ancestor
    covers every layer exactly once -- an inner one would miss a sibling doing
    the work. An earlier version took the youngest, which was left over from
    when only the matched process was measured."""
    procs = [FakeProc(1, ["bash", "-c", "grep -rliE needle ."], created=10.0),
             FakeProc(2, ["bash", "-c", "grep -rliE needle ."], created=20.0)]

    assert find_task_process("grep -rliE needle .", procs).pid == 1


def test_quoting_and_wrapping_do_not_defeat_the_match():
    """Measured against a live task: the CLI wraps the command in shell
    snapshot sourcing and `eval 'cd "..." && <command>'`, which strips quotes,
    flips slashes and flattens newlines. A literal substring test found nothing
    while the command was plainly there -- so the task looked unidentifiable
    and kept its gate holds forever."""
    command = (
        'python -c "\n'
        "import time\n"
        "f = open('orchestrator2.log','rb')\n"
        "for i in range(30):\n"
        '    f.seek(0)"'
    )
    wrapped = (
        r"C:\Program Files\Git\bin\bash.exe -c source /snap.sh "
        "|| true && eval 'cd \"d:/visual studio projects\" && "
        "python -c \nimport time\nf = open(orchestrator2.log,rb)\n"
        "for i in range(30):\n    f.seek(0)'"
    )

    assert find_task_process(command, [FakeProc(1, [wrapped])]) is not None


def test_a_short_command_matches_nothing():
    """A bare "make" would match half the process tree, and sampling the wrong
    process's CPU is how a hung task gets called healthy forever."""
    procs = [FakeProc(1, ["bash", "-c", "make"])]

    assert find_task_process("make", procs) is None


def test_no_command_matches_nothing():
    assert find_task_process(None, [FakeProc(1, ["bash"])]) is None


def test_an_unreadable_process_is_skipped_not_fatal():
    class Exploding(FakeProc):
        def cmdline(self):
            raise RuntimeError("access denied")

    procs = [Exploding(1, []), FakeProc(2, ["bash", "-c", "grep -rliE needle ."])]

    assert find_task_process("grep -rliE needle .", procs).pid == 2


# --------------------------------------------------------------------------
# The output path
# --------------------------------------------------------------------------

def test_the_output_path_matches_the_cli_layout():
    """Checked against a real one from the reported session."""
    p = task_output_path(
        r"D:\visual studio projects\backup",
        "43ee6a50-3185-4986-bd85-485dfd654408", "bnexix9sl",
        tempdir=r"C:\Temp",
    )

    assert p.as_posix().endswith(
        "claude/D--visual-studio-projects-backup/"
        "43ee6a50-3185-4986-bd85-485dfd654408/tasks/bnexix9sl.output")


def test_a_missing_output_file_is_not_an_error():
    """A wrong guess at the layout must degrade to "no output information", not
    throw -- and then the task can never be called stalled on the CPU half."""
    from bg_stall import read_output_signature
    from pathlib import Path

    assert read_output_signature(Path("nope/does/not/exist.output")) is None


def test_the_stall_threshold_is_the_idle_grace():
    """Same "long enough that a human would have noticed" scale as the idle
    teardown, so the two cannot disagree about what counts as quiet."""
    assert STALL_AFTER_S == 300.0

# --------------------------------------------------------------------------
# Wiring: the panel row, and every gate that defers to background work
# --------------------------------------------------------------------------

def _bridge_with(entry_state):
    from config import parse_args
    from sdk_bridge import SDKBridge
    from state import init_state_from_config
    cfg = parse_args([])
    st = init_state_from_config(cfg)

    async def bcast(msg):
        pass

    return SDKBridge(config=cfg, state=st, broadcaster=bcast), st


def test_the_row_stays_in_the_panel_and_carries_the_note():
    """The whole point of not removing it: the panel was the only thing
    reporting a hung process that nobody else could see."""
    import time as _time
    from state import state_to_panels_dict
    _br, st = _bridge_with(None)
    now = _time.monotonic()
    entry = {"stall_probed": True, "name": "Find every script", "pid": 34752}
    update_probe(entry, now=now - 1200.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)
    update_probe(entry, now=now, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)
    st.background_tasks["b1"] = entry

    rows = state_to_panels_dict(st)["background_tasks"]

    assert len(rows) == 1, "a stalled task must not vanish from the panel"
    assert rows[0]["stalled"] is True
    assert "no output, CPU or disk I/O" in (rows[0]["stall_note"] or "")
    assert rows[0]["killable"] is True


def test_a_task_with_no_identified_process_is_not_killable():
    """The button only appears for something we can actually act on."""
    import time as _time
    from state import state_to_panels_dict
    _br, st = _bridge_with(None)
    st.background_tasks["b1"] = _probed(at=_time.monotonic())

    assert state_to_panels_dict(st)["background_tasks"][0]["killable"] is False


def test_the_bridge_gate_helper_drops_a_stalled_task():
    import time as _time
    br, st = _bridge_with(None)
    now = _time.monotonic()
    dead = {"stall_probed": True}
    update_probe(dead, now=now - 1200.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)
    update_probe(dead, now=now, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)
    st.background_tasks["dead"] = dead
    st.background_tasks["live"] = _probed(at=now)

    assert set(br._active_bg_tasks()) == {"live"}


def test_every_gate_asks_the_filtered_set():
    """Six places let background work defer something: an idle teardown, a
    deferred /model, its flush, the bg-all-done wakeup, the context trim and
    bg-wait parking. A seventh reading the raw registry would re-pin the
    session on a task proven to be doing nothing, so the count is pinned."""
    import inspect
    import sdk_bridge
    src = inspect.getsource(sdk_bridge.SDKBridge)
    gates = [l for l in src.splitlines() if "_active_bg_tasks()" in l
             and "def " not in l]

    assert len(gates) == 6, (
        f"expected 6 gate sites through _active_bg_tasks(); found {len(gates)}"
    )

def test_cpu_counts_the_children_doing_the_work():
    """The matched process is the task's *shell*, and a shell sleeps while a
    child compiles or runs tests.  Reading it alone reports a flat zero for a
    task working hard -- and a job like that can also go minutes without
    writing output, so both halves would read "nothing" and it would be
    declared stalled.  Found by probing a real task: the shell sat at 0.015 s
    while its children did everything."""
    from bg_stall import process_activity
    worker = FakeProc(2, ["python", "train.py"], cpu=(90.0, 10.0))
    shell = FakeProc(1, ["bash", "-c", "python train.py"], cpu=(0.01, 0.0),
                     kids=[worker])

    assert process_activity(shell)[0] == 100.01


def test_an_unreadable_child_does_not_lose_the_parents_cpu():
    from bg_stall import process_activity

    class Exploding(FakeProc):
        def cpu_times(self):
            raise RuntimeError("access denied")

    shell = FakeProc(1, ["bash"], cpu=(5.0, 0.0), kids=[Exploding(2, [])])

    assert process_activity(shell)[0] == 5.0


def test_an_unreadable_process_reports_no_cpu_at_all():
    """None, not zero: zero would be indistinguishable from a hung task and
    would let an unreadable process release the gates."""
    from bg_stall import process_activity

    class Exploding(FakeProc):
        def cpu_times(self):
            raise RuntimeError("gone")

    assert process_activity(Exploding(1, [])) is None

# --------------------------------------------------------------------------
# Disk I/O: the third signal
# --------------------------------------------------------------------------

def test_a_task_moving_data_is_working():
    """Copying a large file, walking a huge tree or pulling over the network
    burns almost no CPU and may write nothing to its own output file for
    minutes.  On output and CPU alone it is indistinguishable from a task
    blocked forever -- and would be stripped of its gate holds mid-copy."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.1, io_ops=1000, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=0.1,
                 io_ops=99000)

    assert stall_state(entry, now=1200.0) == WORKING


def test_blocked_on_io_is_not_the_same_as_doing_io():
    """The reported grep was *blocked* on I/O -- zero CPU, and its counters
    were not moving either.  That is the difference the third signal exists to
    draw, and it must still resolve to stalled."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, io_ops=1000, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=0.0,
                 io_ops=1000)

    assert stall_state(entry, now=1200.0) == STALLED


def test_io_going_backwards_does_not_count_as_progress():
    """A cumulative counter that appears to drop means a process left the tree,
    not that work happened."""
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, io_ops=9000, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=0.0,
                 io_ops=10)

    assert stall_state(entry, now=1200.0) == STALLED


def test_io_is_counted_in_operations_not_bytes():
    """A task re-reading the same cached page makes no byte progress but is
    unmistakably alive."""
    from bg_stall import process_activity
    proc = FakeProc(1, ["bash", "-c", "something long enough to match"],
                    io=(7, 5))

    assert process_activity(proc)[1] == 12


def test_io_is_summed_across_the_tree_like_cpu():
    """Same reason as CPU: the shell sleeps while the child does the copying."""
    from bg_stall import process_activity
    worker = FakeProc(2, ["cp", "big", "dest"], io=(500, 400))
    shell = FakeProc(1, ["bash", "-c", "cp big dest"], io=(2, 1), kids=[worker])

    assert process_activity(shell)[1] == 903


def test_a_process_with_no_readable_io_still_reports_its_cpu():
    """``io_counters`` can be denied per-process on Windows.  Losing the whole
    sample over it would turn a readable process into an unknown one, and
    unknown never releases a gate -- so a hung task would hold on forever."""
    from bg_stall import process_activity

    class NoIO(FakeProc):
        def io_counters(self):
            raise RuntimeError("access denied")

    got = process_activity(NoIO(1, ["bash"], cpu=(3.0, 1.0)))

    assert got == (4.0, 0)


def test_the_note_names_all_three_signals():
    entry = _probed(output_sig=(50, 1.0), cpu_s=0.0, io_ops=0, at=0.0)
    update_probe(entry, now=1200.0, output_sig=(50, 1.0), cpu_s=0.0, io_ops=0)

    note = describe_stall(entry, now=1200.0)

    assert "output" in note and "CPU" in note and "I/O" in note

