"""Tests for proc_guard: the job object that stops orphaned `claude.exe`.

The bug being guarded against: on Windows, killing a server does NOT kill its
child processes.  A killed orchestrator2 left its ``claude.exe`` running,
still resumed on its session — one survived four days and 4.4 CPU-hours,
committing into a repo, while a *new* server resumed the same session on top
of it.  Two agents, one session file, one working tree.

The whole point of the job object is that it works when **no shutdown code
runs**, so the important test here actually hard-kills a process
(``taskkill /F``, no ``/T``) and checks the kernel reaped the child.  Mocking
that would test nothing.

Every PID these tests kill is one they spawned themselves.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)

import proc_guard  # noqa: E402

psutil = pytest.importorskip("psutil")

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="job objects are Windows-specific")


# Parent: installs the reaper, then spawns one ordinary child (should be
# reaped with us) and one breakaway child (the restart-replacement case,
# which must survive us).  Prints both PIDs, then idles.
_PARENT_SRC = """
import subprocess, sys, time
sys.path.insert(0, {repo!r})
import proc_guard
assert proc_guard.install_process_reaper(), "reaper failed to install"
sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
in_job = subprocess.Popen(sleeper)
broke_away = subprocess.Popen(sleeper, creationflags=proc_guard.breakaway_flags())
print(in_job.pid, broke_away.pid, flush=True)
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _kill(pid: int) -> None:
    """Best-effort cleanup of a PID this test spawned."""
    try:
        psutil.Process(pid).kill()
    except psutil.Error:
        pass


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


@windows_only
def test_hard_kill_reaps_children_but_not_breakaway():
    """taskkill /F on the parent must take the in-job child with it.

    No ``/T``: we're testing that the *kernel* reaps the tree via the job
    object, not that taskkill can walk it.  That distinction is the entire
    fix — a killed server runs no cleanup code.
    """
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT_SRC.format(repo=REPO)],
        stdout=subprocess.PIPE, text=True,
    )
    in_job = broke_away = None
    try:
        line = parent.stdout.readline().strip()
        assert line, "parent produced no PIDs (reaper install failed?)"
        in_job, broke_away = (int(x) for x in line.split())
        assert _alive(in_job) and _alive(broke_away)

        # Hard kill the parent only — nothing gets a chance to clean up.
        subprocess.run(["taskkill", "/F", "/PID", str(parent.pid)],
                       capture_output=True)

        assert _wait_gone(in_job), (
            "in-job child survived the parent — the job object is NOT "
            "reaping children, which is exactly the orphaned-claude.exe bug")
        # The breakaway child is the ⟳ Restart replacement server: it MUST
        # outlive the process that spawned it.
        assert _alive(broke_away), (
            "breakaway child was killed with the parent — ⟳ Restart would "
            "shoot its own replacement")
    finally:
        for pid in (in_job, broke_away):
            if pid:
                _kill(pid)
        try:
            parent.kill()
        except OSError:
            pass


@windows_only
def test_install_is_idempotent():
    assert proc_guard.install_process_reaper() is True
    assert proc_guard.install_process_reaper() is True
    assert proc_guard.reaper_active() is True
    assert proc_guard.breakaway_flags() == proc_guard.CREATE_BREAKAWAY_FROM_JOB


def test_breakaway_flags_zero_without_reaper(monkeypatch):
    """Callers add breakaway_flags() unconditionally, so it must be 0-safe."""
    monkeypatch.setattr(proc_guard, "_reaper_installed", False)
    assert proc_guard.breakaway_flags() == 0


def test_snapshot_then_reap_kills_the_cli_s_orphaned_children():
    """The MCP-server leak: kill the CLI, its children survive, we reap them.

    Mirrors the real shape — ``claude.exe`` spawns an MCP stack, the SDK then
    stops ``claude.exe`` with TerminateProcess (no ``/T``, no cleanup code),
    and on Windows the grandchildren are simply orphaned.
    """
    cli = subprocess.Popen([
        sys.executable, "-c",
        "import subprocess, sys, time;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
        "print('up', flush=True); time.sleep(120)"
    ], stdout=subprocess.PIPE, text=True)
    try:
        cli.stdout.readline()                       # child has spawned its own
        snap = proc_guard.snapshot_descendants(cli.pid)
        assert snap, "no descendants captured — snapshot ran too early?"
        grandchild = snap[0].pid

        cli.kill()                                  # what the SDK does
        cli.wait()
        assert _wait_gone(cli.pid)
        assert _alive(grandchild), (
            "grandchild died with the CLI — Windows would not do that; "
            "this test is no longer reproducing the leak")

        assert proc_guard.reap_descendants(snap) == 1
        assert _wait_gone(grandchild)
    finally:
        for d in proc_guard.snapshot_descendants(cli.pid):
            _kill(d.pid)
        _kill(cli.pid)


def test_reap_skips_recycled_pids():
    """A pid that got recycled between snapshot and reap must be spared.

    Without the creation-time check this would kill an unrelated process that
    merely inherited the number — a far worse bug than the leak it fixes.
    """
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # Same pid, but claiming a creation time that isn't this process's.
        stale = [proc_guard.Descendant(victim.pid, 1.0)]
        assert proc_guard.reap_descendants(stale) == 0
        assert _alive(victim.pid), "reaped a process whose pid was recycled"
    finally:
        victim.kill()


def test_reap_of_dead_pids_is_harmless():
    """Reaping is best-effort: already-gone entries must not raise."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert proc_guard.reap_descendants(
        [proc_guard.Descendant(dead.pid, 1.0)]) == 0
    assert proc_guard.reap_descendants([]) == 0


def test_snapshot_of_dead_pid_is_empty():
    assert proc_guard.snapshot_descendants(-1) == []


def test_sdk_still_exposes_the_cli_process_we_reach_for():
    """Canary: ``_cli_pid`` reads SDK-private attributes.

    It fails soft by design, so an SDK rename would silently bring the MCP
    leak back with no error anywhere.  This test is the thing that notices.
    """
    pytest.importorskip("claude_agent_sdk")
    import inspect
    from claude_agent_sdk import ClaudeSDKClient
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport)
    import anyio.abc

    assert "_transport" in inspect.getsource(ClaudeSDKClient.__init__), (
        "ClaudeSDKClient no longer has _transport — update SDKBridge._cli_pid")
    assert "_process" in inspect.getsource(SubprocessCLITransport.__init__), (
        "transport no longer has _process — update SDKBridge._cli_pid")
    assert hasattr(anyio.abc.Process, "pid")


def test_cli_pid_is_none_when_sdk_shape_changes():
    """A missing attribute must degrade to 'no cleanup', never raise."""
    pytest.importorskip("claude_agent_sdk")
    import sdk_bridge

    bridge = sdk_bridge.SDKBridge.__new__(sdk_bridge.SDKBridge)
    for client in (None, object(),
                   type("C", (), {"_transport": None})(),
                   type("C", (), {"_transport": type("T", (), {"_process": None})()})()):
        bridge.client = client
        assert bridge._cli_pid() is None

    real = type("C", (), {"_transport": type(
        "T", (), {"_process": type("P", (), {"pid": 4321})()})()})()
    bridge.client = real
    assert bridge._cli_pid() == 4321


def test_no_session_id_is_not_a_duplicate():
    assert proc_guard.find_foreign_claude_for_session("") == []
    assert proc_guard.find_foreign_claude_for_session(None) == []


def test_own_children_are_not_foreign():
    """A session we're running ourselves must not trip the guard.

    The check exists to find processes *outside* our tree; matching our own
    ``claude.exe`` would make every session refuse to start.
    """
    sid = "test-session-id-not-real"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        # Our own descendants are excluded by PID regardless of cmdline.
        assert child.pid in proc_guard._own_tree_pids()
        assert proc_guard.find_foreign_claude_for_session(sid) == []
    finally:
        child.kill()


def test_duplicate_error_message_is_actionable():
    procs = [proc_guard.ClaudeProc(pid=4321, started="2026-08-06 00:33:18",
                                   cmdline="claude --resume abc")]
    err = proc_guard.DuplicateSessionError("abcdef01-2345", procs)
    text = str(err)
    # The user has to kill the other process, so the message must name it and
    # hand over the exact command.
    assert "4321" in text
    assert "taskkill /PID 4321" in text
    assert "--allow-duplicate-session" in text
    assert err.procs == procs


def test_match_requires_the_resume_argument_pair():
    """A bare id appearing anywhere in argv must not count as a match.

    Guards against a false positive refusing a legitimate session — e.g. the
    id showing up inside a prompt or a file path.
    """
    argv_hit = ["claude.exe", "--resume", "sid-1", "--verbose"]
    argv_miss = ["claude.exe", "--prompt", "look at sid-1", "--resume", "sid-2"]

    def matches(argv, sid):
        return any(a == "--resume" and i + 1 < len(argv) and argv[i + 1] == sid
                   for i, a in enumerate(argv))

    assert matches(argv_hit, "sid-1")
    assert not matches(argv_miss, "sid-1")
    assert matches(argv_miss, "sid-2")


# ---------------------------------------------------------------------------
# --fork-session: reads a conversation, does not hold it
# ---------------------------------------------------------------------------

class _IterProc:
    """What ``psutil.process_iter(attrs)`` yields: an object with ``.info``."""

    def __init__(self, pid, name, cmdline, create_time=0.0):
        self.info = {"pid": pid, "name": name, "cmdline": cmdline,
                     "create_time": create_time}


def _fake_iter(monkeypatch, procs):
    """Point the real scanners at *procs* instead of the live process table."""
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(proc_guard, "_own_tree_pids", lambda: set())


SID = "1aa74fb0-ca4e-42cb-80c3-ef7302fee0a4"


def test_a_foreign_resume_is_still_detected(monkeypatch):
    """The control.  Without it the skip test below could pass because the
    harness never matches anything -- which is exactly how a test ends up
    proving nothing."""
    _fake_iter(monkeypatch, [
        _IterProc(4321, "claude.exe", ["claude.exe", "--resume", SID]),
    ])

    found = proc_guard.find_foreign_claude_for_session(SID)

    assert [p.pid for p in found] == [4321]


def test_a_fork_is_not_a_holder(monkeypatch):
    """``--fork-session`` resumes a conversation and writes to a *new* id, so
    it never touches the transcript it read.  Counting it would make a session
    look held elsewhere for the seconds a /btw takes to answer -- and this scan
    is what stops a session opening, so a false positive is not cosmetic."""
    _fake_iter(monkeypatch, [
        _IterProc(4321, "claude.exe",
                  ["claude.exe", "--resume", SID, "--fork-session"]),
    ])

    assert proc_guard.find_foreign_claude_for_session(SID) == []


def test_a_fork_alongside_a_real_holder_does_not_mask_it(monkeypatch):
    """Skipping forks must not skip the process actually driving the session."""
    _fake_iter(monkeypatch, [
        _IterProc(111, "claude.exe",
                  ["claude.exe", "--resume", SID, "--fork-session"]),
        _IterProc(222, "claude.exe", ["claude.exe", "--resume", SID]),
    ])

    found = proc_guard.find_foreign_claude_for_session(SID)

    assert [p.pid for p in found] == [222]


def test_the_holder_map_skips_forks_too(monkeypatch):
    """The lobby and the peer probe read this one.  A fork listed here would
    show the session as running somewhere else."""
    _fake_iter(monkeypatch, [
        _IterProc(4321, "claude.exe",
                  ["claude.exe", "--resume", SID, "--fork-session"]),
    ])
    assert SID not in proc_guard.map_foreign_session_holders()


def test_the_holder_map_still_lists_a_real_holder(monkeypatch):
    """The matching control for the map."""
    _fake_iter(monkeypatch, [
        _IterProc(4321, "claude.exe", ["claude.exe", "--resume", SID]),
    ])
    assert SID in proc_guard.map_foreign_session_holders()


def test_the_flag_is_the_one_the_sdk_actually_passes():
    """Pinned against the SDK rather than assumed: the filter is worthless if
    the flag is spelled differently.  ``subprocess_cli.py`` does
    ``cmd.append("--fork-session")``."""
    import os
    import claude_agent_sdk
    src = os.path.join(os.path.dirname(claude_agent_sdk.__file__),
                       "_internal", "transport", "subprocess_cli.py")
    with open(src, encoding="utf-8") as f:
        text = f.read()

    assert '"--fork-session"' in text, (
        "the SDK no longer passes --fork-session; the foreign-holder filter "
        "silently stops working and /btw starts looking like a duplicate"
    )


def test_a_mention_of_the_flag_is_not_the_flag(monkeypatch):
    """Matched as an *argument*, never as a substring of the joined line — the
    same rule the ``--resume`` pair match already follows, for the same reason.
    A session whose prompt happens to discuss ``--fork-session`` would
    otherwise be waved through as a fork, disabling the duplicate guard for a
    conversation something really is driving."""
    _fake_iter(monkeypatch, [
        _IterProc(4321, "claude.exe",
                  ["claude.exe", "--resume", SID,
                   "--prompt", "what does --fork-session do?"]),
    ])

    found = proc_guard.find_foreign_claude_for_session(SID)

    assert [p.pid for p in found] == [4321], (
        "a process merely mentioning the flag was treated as a fork"
    )


def test_the_flag_must_be_its_own_argument():
    assert proc_guard._is_session_fork(
        ["claude.exe", "--prompt", "explain --fork-session"]) is False


def test_a_plain_session_is_not_mistaken_for_a_fork():
    assert proc_guard._is_session_fork(
        ["claude.exe", "--resume", SID, "--fork-session"]) is True
    assert proc_guard._is_session_fork(["claude.exe", "--resume", SID]) is False


# ---------------------------------------------------------------------------
# find_foreign_session_holder — enrich a foreign holder with its hub window so
# the lobby can offer to focus it instead of forking a doomed duplicate.
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, status, port):
        self.status = status
        self.laddr = type("Addr", (), {"port": port})()


class _FakeProc:
    def __init__(self, pid, cmdline, parent=None, conns=None):
        self.pid = pid
        self._cmdline = cmdline
        self._parent = parent
        self._conns = conns or []

    def parent(self):
        return self._parent

    def cmdline(self):
        return self._cmdline

    def net_connections(self, kind="inet"):
        return self._conns


def test_session_holder_none_when_nothing_foreign(monkeypatch):
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session",
                        lambda sid: [])
    assert proc_guard.find_foreign_session_holder("sid") is None


def test_session_holder_resolves_owning_hub_port(monkeypatch):
    """A holder whose parent is one of our servers is a focusable hub: we
    return its listening port so the UI can send the user there."""
    holder = proc_guard.ClaudeProc(pid=999, started="2026-08-28 10:22:03",
                                   cmdline="claude --resume sid")
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session",
                        lambda sid: [holder])
    server = _FakeProc(888, ["python.exe", "server.py", "--cwd", "x"],
                       conns=[_FakeConn(psutil.CONN_LISTEN, 53935)])
    child = _FakeProc(999, ["claude", "--resume", "sid"], parent=server)
    monkeypatch.setattr(psutil, "Process", lambda pid: child)

    fh = proc_guard.find_foreign_session_holder("sid")
    assert fh is not None
    assert (fh.holder_pid, fh.server_pid, fh.port) == (999, 888, 53935)
    assert fh.started == "2026-08-28 10:22:03"


def test_session_holder_in_a_terminal_has_no_focusable_port(monkeypatch):
    """A bare `claude --resume` under a shell is a real duplicate but not a
    window we can focus — server_pid/port stay None so the UI explains instead
    of offering a dead link."""
    holder = proc_guard.ClaudeProc(pid=999, started="", cmdline="claude --resume sid")
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session",
                        lambda sid: [holder])
    shell = _FakeProc(500, ["C:\\Windows\\system32\\cmd.exe"])
    child = _FakeProc(999, ["claude", "--resume", "sid"], parent=shell)
    monkeypatch.setattr(psutil, "Process", lambda pid: child)

    fh = proc_guard.find_foreign_session_holder("sid")
    assert fh is not None and fh.holder_pid == 999
    assert fh.server_pid is None and fh.port is None


def test_session_holder_hub_port_unreadable_degrades(monkeypatch):
    """We know the hub but can't read its port (AccessDenied): identify the
    server, leave the port None — never raise out of a best-effort detector."""
    holder = proc_guard.ClaudeProc(pid=999, started="", cmdline="claude --resume sid")
    monkeypatch.setattr(proc_guard, "find_foreign_claude_for_session",
                        lambda sid: [holder])

    class _Denied(_FakeProc):
        def net_connections(self, kind="inet"):
            raise psutil.AccessDenied(self.pid)

    server = _Denied(888, ["python.exe", "server.py"])
    child = _FakeProc(999, ["claude", "--resume", "sid"], parent=server)
    monkeypatch.setattr(psutil, "Process", lambda pid: child)

    fh = proc_guard.find_foreign_session_holder("sid")
    assert fh.server_pid == 888
    assert fh.port is None
