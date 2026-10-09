"""Ending a session ends what it started (proc_guard.SessionJob).

Reported 2026-10-09: "after closing my os sessions, it didn't stop those
sessions' background processes, should it have?"  It should have.  A QEMU
that OS A's fast boot started with ``qemu ... 2> file &`` ran on after OS A was
closed, until Windows restarted three minutes later.  The close had killed 16
processes, found by walking the CLI's process tree, and two things hid QEMU
from that walk:

* a program Git Bash backgrounds ends up under a bash whose parent has
  exited, so its chain of parents no longer reaches the CLI;
* a native program Git Bash starts breaks away from any job that allows it,
  and the hub's job allows it (for ⟳ Restart), so it also outlived the hub.

So the CLI now runs in a job of its own that does not allow breakaway, and
ending the session terminates the job.  These tests use real processes, a
real Git Bash and real job objects: the failure is in how Windows and MSYS
build process trees, and a mock of that would prove nothing.  Every process
killed here is one these tests started, and only by pid.
"""

from __future__ import annotations

import asyncio
import ctypes
import inspect
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import proc_guard  # noqa: E402
from sdk_bridge import SDKBridge  # noqa: E402

BASH = shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe"
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not Path(BASH).exists(),
    reason="job objects and Git Bash are Windows-specific")


# --- a stand-in for claude.exe and its Bash tool ------------------------------

_STAND_IN = r"""
import os, subprocess, sys
sys.stdin.readline()                       # wait until told: the job comes first
subprocess.run([sys.argv[1], "-c", os.environ["ORCH2_STAND_IN_SCRIPT"]],
               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL)
"""


class StandIn:
    """A process playing ``claude.exe``: on cue it runs *script* in Git Bash,
    as the Bash tool does, and stays alive while bash does.

    The script reaches it through the environment, not its command line: the
    script names the program's marker, and a stand-in carrying the marker was
    found in the program's place -- which made every check pass for the wrong
    reason (a process is never its own descendant, and killing the stand-in
    "killed the program")."""

    def __init__(self, script: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _STAND_IN, BASH],
            stdin=subprocess.PIPE, text=True,
            env={**os.environ, "ORCH2_STAND_IN_SCRIPT": script})
        self.pid = self.proc.pid

    def go(self) -> None:
        self.proc.stdin.write("go\n")
        self.proc.stdin.flush()

    def kill(self) -> None:
        for d in proc_guard.snapshot_descendants(self.pid):
            _kill(d.pid)
        _kill(self.pid)


def _kill(pid: int) -> None:
    try:
        psutil.Process(pid).kill()
    except psutil.Error:
        pass


def _backgrounding(marker: str) -> str:
    """A native program started in the background from a shell that is
    gone: its chain of parents no longer reaches the CLI.

    fastboot.sh ran ``qemu ... 2> file &`` and waited on it, and a program
    backgrounded that way under the real CLI had a broken chain (measured
    2026-10-09).  Rather than depend on how MSYS arranges its helper
    processes, this breaks the chain outright: the subshell that started the
    program exits at once.  The shell running the script stays, as the CLI's
    Bash tool does."""
    py = sys.executable.replace(chr(92), "/")
    return (f'( "{py}" -c "import time; time.sleep(120)  # {marker}" '
            f'>/dev/null 2>&1 </dev/null & ); while true; do sleep 1; done')


def _find(marker: str, stand_in: "StandIn", timeout: float = 15.0) -> psutil.Process:
    """The backgrounded program: the python whose own command line carries
    *marker* -- never the stand-in, whatever it carries."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            if (p.info["pid"] != stand_in.pid
                    and marker in " ".join(p.info["cmdline"] or [])
                    and (p.info["name"] or "").lower().startswith("python")):
                return p
        time.sleep(0.1)
    raise AssertionError(f"the backgrounded program ({marker}) never started")


def _orphaned(stand_in: StandIn, program: psutil.Process,
              timeout: float = 10.0) -> None:
    """Wait for the subshell that started *program* to exit, so the tree walk
    from the stand-in can no longer reach it: the state QEMU was in.  Until
    then the walk still sees it, and a test closing the session in that window
    would pass without the job."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        walked = {d.pid for d in proc_guard.snapshot_descendants(stand_in.pid)}
        if program.pid not in walked:
            return
        time.sleep(0.1)
    raise AssertionError("the program is still reachable from the stand-in's "
                         "tree -- its starting subshell never exited")


def _gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return not psutil.pid_exists(pid)


@pytest.fixture
def scene():
    """A stand-in CLI about to background a program; everything killed after."""
    marker = f"orch2-session-job-{uuid.uuid4().hex[:8]}"
    stand_in = StandIn(_backgrounding(marker))
    started: list[int] = []
    yield SimpleNamespace(stand_in=stand_in, marker=marker, started=started)
    stand_in.kill()
    for pid in started:
        _kill(pid)
    for p in psutil.process_iter(["pid", "cmdline"]):
        if marker in " ".join(p.info["cmdline"] or []):
            _kill(p.info["pid"])


# --- the gap, and the job that closes it --------------------------------------

def test_the_tree_walk_cannot_see_a_backgrounded_program(scene):
    """The premise.  If Git Bash stops orphaning these, the job is no longer
    the only thing that finds them -- worth knowing, either way."""
    scene.stand_in.go()
    program = _find(scene.marker, scene.stand_in)
    scene.started.append(program.pid)

    _orphaned(scene.stand_in, program)
    assert program.is_running(), "an orphan should live on: nothing has killed it"


def test_ending_the_job_ends_it(scene):
    """The report: closing the session must stop what it started."""
    job = proc_guard.SessionJob.adopt(scene.stand_in.pid)
    assert job is not None
    scene.stand_in.go()
    program = _find(scene.marker, scene.stand_in)
    scene.started.append(program.pid)

    assert program.pid in job.pids()
    assert job.end() >= 1               # how many more depends on MSYS's helpers
    assert _gone(program.pid), "the backgrounded program outlived its session"
    assert _gone(scene.stand_in.pid)


# --- breakaway ----------------------------------------------------------------

def _job_allowing_breakaway(pid: int) -> int:
    """A kill-on-close job that allows breakaway, as the hub's does."""
    k32 = proc_guard._kernel32()
    job = k32.CreateJobObjectW(None, None)
    info = proc_guard._JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        proc_guard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | proc_guard._JOB_OBJECT_LIMIT_BREAKAWAY_OK)
    assert k32.SetInformationJobObject(
        job, proc_guard._JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info))
    proc = k32.OpenProcess(proc_guard._PROCESS_SET_QUOTA | proc_guard._PROCESS_TERMINATE,
                           False, pid)
    try:
        assert k32.AssignProcessToJobObject(job, proc)
    finally:
        k32.CloseHandle(proc)
    return job


def _in_job(job: int, pid: int) -> bool:
    k32 = proc_guard._kernel32()
    k32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_int)]
    proc = k32.OpenProcess(0x1000, False, pid)        # query-limited
    result = ctypes.c_int()
    try:
        assert k32.IsProcessInJob(proc, job, ctypes.byref(result))
    finally:
        k32.CloseHandle(proc)
    return bool(result.value)


def test_in_the_hub_s_kind_of_job_git_bash_breaks_away(scene):
    """The premise: MSYS starts native programs with CREATE_BREAKAWAY_FROM_JOB
    whenever the job allows it, and the hub's does (for ⟳ Restart).  So the
    hub's job never held what a session started from Git Bash, and its exit
    did not end it either."""
    hub_job = _job_allowing_breakaway(scene.stand_in.pid)
    try:
        scene.stand_in.go()
        program = _find(scene.marker, scene.stand_in)
        scene.started.append(program.pid)
        assert not _in_job(hub_job, program.pid)
    finally:
        proc_guard._kernel32().CloseHandle(hub_job)
    time.sleep(0.5)
    assert psutil.pid_exists(program.pid), "the hub's job took it after all"


def test_the_session_job_lets_nothing_break_away():
    """The job's own terms: kill on close, and no breakaway, silent or asked
    for.  MSYS reads them and does not ask (the test above and below)."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = proc_guard.SessionJob.adopt(proc.pid)
        info = proc_guard._JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        assert proc_guard._kernel32().QueryInformationJobObject(
            job._handle, proc_guard._JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info), None)
        flags = info.BasicLimitInformation.LimitFlags
        assert flags & proc_guard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        assert not flags & proc_guard._JOB_OBJECT_LIMIT_BREAKAWAY_OK
        assert not flags & 0x00001000          # JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        job.end()
    finally:
        _kill(proc.pid)


def test_in_a_session_job_inside_it_git_bash_cannot(scene):
    """Nested in the hub's kind of job, the session's job still holds it --
    and so does the hub's, so the hub's exit takes it too."""
    hub_job = _job_allowing_breakaway(scene.stand_in.pid)
    try:
        job = proc_guard.SessionJob.adopt(scene.stand_in.pid)
        assert job is not None
        scene.stand_in.go()
        program = _find(scene.marker, scene.stand_in)
        scene.started.append(program.pid)

        assert program.pid in job.pids()
        assert _in_job(hub_job, program.pid)
        job.end()
        assert _gone(program.pid)
    finally:
        proc_guard._kernel32().CloseHandle(hub_job)


# --- the job's own contract ---------------------------------------------------

def test_what_started_before_the_cli_joined_is_left_to_the_tree_walk():
    """The boundary: the MCP servers a CLI starts during its handshake are
    not in the job, and the tree walk still covers them."""
    early = subprocess.Popen([
        sys.executable, "-c",
        "import subprocess, sys, time;"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "print(c.pid, flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
    child = int(early.stdout.readline())
    try:
        job = proc_guard.SessionJob.adopt(early.pid)
        assert child not in job.pids()
        assert child in {d.pid for d in proc_guard.snapshot_descendants(early.pid)}
        job.end()
    finally:
        _kill(child)
        _kill(early.pid)


def test_end_says_how_many_it_ended_and_only_once():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = proc_guard.SessionJob.adopt(proc.pid)
        assert job.end() == 1
        assert _gone(proc.pid)
        assert job.end() == 0
        assert job.pids() == []
    finally:
        _kill(proc.pid)


def test_a_cli_that_cannot_be_adopted_is_left_as_it_was():
    assert proc_guard.SessionJob.adopt(None) is None
    assert proc_guard.SessionJob.adopt(-1) is None
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert proc_guard.SessionJob.adopt(gone.pid) is None


# --- the bridge -----------------------------------------------------------------

def make_bridge() -> SDKBridge:
    cfg = SimpleNamespace(cwd=".", permission_mode="default", config_dir=None)
    state = SimpleNamespace(auth_error=False, background_tasks={},
                            completed_panel_bg={}, session_id=None)

    async def broadcaster(_msg):
        pass

    return SDKBridge(config=cfg, state=state, broadcaster=broadcaster)


class FakeClient:
    """The SDK client: stops the CLI it was given, as the real one does."""

    def __init__(self, pid: int, events: list[str]) -> None:
        self._transport = SimpleNamespace(_process=SimpleNamespace(pid=pid))
        self._events = events

    async def disconnect(self) -> None:
        self._events.append("sdk stopped the CLI")
        if self._transport._process.pid > 0:
            _kill(self._transport._process.pid)


def test_closing_the_session_ends_its_background_program(scene):
    """End to end through SDKBridge.disconnect -- what the × runs."""
    bridge = make_bridge()
    bridge.client = FakeClient(scene.stand_in.pid, [])
    bridge._adopt_session_job()
    scene.stand_in.go()
    program = _find(scene.marker, scene.stand_in)
    scene.started.append(program.pid)
    _orphaned(scene.stand_in, program)

    asyncio.run(bridge.disconnect())

    assert _gone(program.pid), "the session closed and its program ran on"
    assert bridge._session_job is None


def test_the_job_is_ended_after_the_sdk_has_stopped_the_cli():
    """Last, so the CLI's graceful stop can finish writing the transcript
    before anything is killed outright."""
    events: list[str] = []
    bridge = make_bridge()
    bridge.client = FakeClient(-1, events)

    class Job:
        pid = -1

        def end(self):
            events.append("job ended")
            return 0

    bridge._session_job = Job()
    asyncio.run(bridge.disconnect())
    assert events == ["sdk stopped the CLI", "job ended"]


def test_a_job_outliving_its_client_is_still_ended():
    bridge = make_bridge()
    ended = []
    bridge._session_job = SimpleNamespace(pid=-1, end=lambda: ended.append(1) or 0)
    bridge.client = None
    asyncio.run(bridge.disconnect())
    assert ended == [1] and bridge._session_job is None


def test_a_new_cli_ends_a_job_left_by_the_last_one():
    bridge = make_bridge()
    ended = []
    bridge._session_job = SimpleNamespace(pid=-1, end=lambda: ended.append(1) or 0)
    bridge.client = None                       # no pid to adopt
    bridge._adopt_session_job()
    assert ended == [1] and bridge._session_job is None


def test_connect_puts_the_cli_in_its_job_as_soon_as_it_is_connected():
    """Pinned in the source, since a real connect needs a real CLI: the job
    is adopted on the line after the handshake, before anything can await --
    a turn is what starts background work, and none can run until connect
    returns."""
    src = inspect.getsource(SDKBridge.connect)
    handshake = src.index("await asyncio.wait_for(self.client.connect(), timeout=budget)")
    adopt = src.index("self._adopt_session_job()")
    assert handshake < adopt
    code = [line for line in src[handshake:adopt].splitlines()[1:]
            if line.strip() and not line.strip().startswith("#")]
    assert code == [], f"code runs between the handshake and the job: {code}"
