"""Tie the ``claude`` CLI subprocesses to this server's lifetime.

Two independent safety nets against the same failure: **two live agents
driving one session, and one repo, at the same time.**

Why this exists
---------------
The SDK spawns ``claude.exe`` as a child process. On Windows, killing a
parent does *not* kill its children — there is no automatic process-tree
teardown — so every server that dies leaves its ``claude.exe`` children
running, still resumed on their sessions, still editing files and making
commits. Nothing in the server tracks them afterwards (they belong to no
``SessionRuntime``), so they're invisible to the lobby.

This was observed in the wild: a server process was killed, its
``claude.exe --resume 7baa01e0…`` child survived for **four days**, burning
4.4 CPU-hours in the `os` repo. When a new server later resumed that same
session, two agents were committing to one repo and appending to one 548 MB
session JSONL. The surviving agent noticed HEAD advancing under it and files
growing while it made no edits.

The two nets
------------
1. :func:`install_process_reaper` — put this process in a Windows **Job
   Object** with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. Children inherit
   job membership, so when this process dies *by any means* — clean exit,
   crash, ``taskkill /F`` — the kernel closes the last job handle and
   terminates every ``claude.exe`` under it. This is the only mechanism that
   survives a hard kill, because no shutdown handler runs for one.

   The job also sets ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` so processes that are
   *supposed* to outlive us (notably the ⟳ Restart replacement server) can
   opt out with :data:`CREATE_BREAKAWAY_FROM_JOB`.

2. :func:`find_foreign_claude_for_session` — detect a ``claude`` process that
   is already resuming a given session id and is **not** part of our own
   process tree. That catches orphans which already existed before the reaper
   was installed, and a second hub started behind our back. The caller
   refuses to connect rather than becoming the second writer.

Non-Windows is a no-op: this is a Windows-specific hazard and orchestrator2
is a Windows app. On POSIX the equivalent would be a process group plus
``prctl(PR_SET_PDEATHSIG)``; not implemented.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Any, NamedTuple

log = logging.getLogger(__name__)

# Pass to subprocess ``creationflags`` for a child that must OUTLIVE this
# server (the restart replacement).  Without it the child inherits our job
# and dies with us — which would silently break ⟳ Restart.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# Win32 constants.
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# The job handle is held for the process lifetime *on purpose*: KILL_ON_JOB_CLOSE
# fires when the LAST handle closes, so dropping this would kill our own
# children immediately.  Module-global so the GC can never collect it.
_job_handle: int | None = None
_reaper_installed = False


if sys.platform == "win32":
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),          # ULONG_PTR
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def install_process_reaper() -> bool:
    """Assign this process to a kill-on-close Job Object.

    Every process spawned from here on (the SDK's ``claude.exe``, and in turn
    its own ``bash.exe`` tool subprocesses) inherits the job, so the whole
    tree is torn down by the kernel when this process dies — however it dies.

    Returns True when the reaper is active. Failure is logged and tolerated:
    a server that can't create a job object should still run, just without
    the guarantee.
    """
    global _job_handle, _reaper_installed

    if sys.platform != "win32":
        log.debug("process reaper: not Windows — skipped")
        return False
    if _reaper_installed:
        return True

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetCurrentProcess.argtypes = []

        job = k32.CreateJobObjectW(None, None)
        if not job:
            log.warning("process reaper: CreateJobObject failed (err=%d) — "
                        "orphaned claude.exe processes are possible if this "
                        "server is killed", ctypes.get_last_error())
            return False

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        if not k32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            log.warning("process reaper: SetInformationJobObject failed "
                        "(err=%d)", ctypes.get_last_error())
            return False

        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            # Most likely cause: we're already inside a job that forbids
            # breakaway and nesting isn't available.  Not fatal.
            log.warning("process reaper: AssignProcessToJobObject failed "
                        "(err=%d) — already in a restrictive job? child "
                        "claude.exe processes may outlive this server",
                        ctypes.get_last_error())
            return False

        _job_handle = job
        _reaper_installed = True
        log.info("process reaper: active — claude.exe children will be killed "
                 "with this server (pid %d)", os.getpid())
        return True
    except Exception:
        log.exception("process reaper: install failed")
        return False


def reaper_active() -> bool:
    """True when :func:`install_process_reaper` succeeded."""
    return _reaper_installed


def breakaway_flags() -> int:
    """``creationflags`` bits for a child that must outlive this server.

    Zero when no reaper is installed, so callers can add it unconditionally.
    """
    return CREATE_BREAKAWAY_FROM_JOB if _reaper_installed else 0


# ---------------------------------------------------------------------------
# Duplicate-session detection
# ---------------------------------------------------------------------------

class DuplicateSessionError(RuntimeError):
    """Another Claude process is already resuming this session.

    Deliberately *not* retryable: the connect loop's exponential backoff is
    for transient failures, and hammering a duplicate 10 times just delays a
    message the user has to act on (kill the other process).
    """

    def __init__(self, session_id: str, procs: list["ClaudeProc"]) -> None:
        self.session_id = session_id
        self.procs = procs
        super().__init__(describe_duplicates(session_id, procs))


class ClaudeProc(NamedTuple):
    """A ``claude`` CLI process found resuming some session."""

    pid: int
    started: str        # human-readable local time, "" if unavailable
    cmdline: str


def _own_tree_pids() -> set[int]:
    """PIDs of this process and all its descendants."""
    import psutil

    me = psutil.Process()
    pids = {me.pid}
    try:
        for child in me.children(recursive=True):
            pids.add(child.pid)
    except psutil.Error:
        pass
    return pids


class Descendant(NamedTuple):
    """A process to reap later, pinned by identity — see :func:`snapshot_descendants`."""

    pid: int
    created: float      # process creation time; guards against PID reuse


def snapshot_descendants(pid: int) -> list[Descendant]:
    """Record every descendant of *pid*, for reaping once *pid* is gone.

    Must be called while *pid* is still **alive**: the parent links that make
    the tree walkable disappear with it, and an orphan's ``ppid`` then points
    at a dead (or, worse, recycled) pid. So we capture identities up front and
    kill from the list afterwards.

    Each entry carries the creation time as well as the pid. Windows recycles
    pids aggressively, and there is a real window here — between the snapshot
    and the reap, ``claude.exe`` exits and its pid becomes available. Killing
    on pid alone could shoot an unrelated process that happened to inherit the
    number.
    """
    import psutil

    out: list[Descendant] = []
    try:
        for p in psutil.Process(pid).children(recursive=True):
            try:
                out.append(Descendant(p.pid, p.create_time()))
            except psutil.Error:
                continue
    except (psutil.Error, ValueError, OSError):
        # ValueError: psutil rejects a non-positive pid outright.  Cleanup is
        # best-effort — never let it propagate into the disconnect path.
        return []
    return out


def snapshot_process(pid: int) -> Descendant | None:
    """Pin *pid*'s identity so it can be killed later, safely.

    Same PID-reuse guard as :func:`snapshot_descendants`, for the one process
    that function deliberately excludes: ``claude.exe`` itself. The SDK is
    *supposed* to terminate it on disconnect, but "supposed to" is not a
    guarantee we can check afterwards without having recorded who it was —
    by then the pid may belong to something else entirely.

    Returns None when the process is already gone, which is the good case.
    """
    import psutil

    try:
        p = psutil.Process(pid)
        return Descendant(p.pid, p.create_time())
    except (psutil.Error, ValueError, OSError):
        return None


def private_bytes(pid: int) -> int | None:
    """Private (non-shareable) committed bytes of *pid*, or None if unknown.

    On Windows this is ``PROCESS_MEMORY_COUNTERS_EX.PrivateUsage`` — the same
    counter PowerShell exposes as ``PrivateMemorySize64`` and Task Manager as
    "Commit size".  It is the number that matters for the CLI leak the bridge
    recycles on: the charge is against the system-wide **commit limit**, and a
    process can hold hundreds of GiB of it while its working set stays at a
    couple of GiB, because reserved-but-untouched commit lives neither in RAM
    nor in the pagefile.  Working set therefore cannot see this leak at all.

    Elsewhere psutil's ``pmem`` has no ``private`` field, so we fall back to
    RSS.  That measures something genuinely different (resident, shareable
    pages included), but it is the closest available "how big has this process
    got" signal, and the recycle policy only ever compares it against its own
    post-connect baseline.

    Returns None when the process is gone or unreadable — callers treat that
    as "no measurement", never as zero.
    """
    # psutil happily reports pid 0 on Windows (the System Idle Process), which
    # is a meaningless 60 KB answer rather than the error it should be.
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        mi = psutil.Process(pid).memory_info()
    except (psutil.Error, ValueError, OSError):
        return None
    val = getattr(mi, "private", None)
    if val is None:
        val = getattr(mi, "rss", None)
    return int(val) if isinstance(val, (int, float)) else None


def reap_descendants(snapshot: list[Descendant]) -> int:
    """Kill whichever snapshotted processes are still alive. Returns the count.

    Used after the SDK tears down a ``claude.exe``: the CLI is killed with
    ``TerminateProcess``, which on Windows leaves *its* children — the stdio
    MCP servers, each a ``cmd.exe`` → ``npx`` → ``cmd.exe`` → server stack —
    running forever under a still-live hub.

    Killing them is unambiguously safe: a stdio MCP server talks to exactly
    one client over the pipes it was born with, so once that client is dead
    the process can never serve anyone again. (Remote HTTP/SSE MCP servers are
    not our children and are never touched by this.)
    """
    import psutil

    killed = 0
    for pid, created in snapshot:
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - created) > 0.001:
                continue        # pid was recycled — not the process we meant
            p.kill()
            killed += 1
        except (psutil.Error, ValueError, OSError):
            continue            # already gone, or not ours to kill
    return killed


def _is_session_fork(argv: list[str]) -> bool:
    """Whether this ``claude`` invocation is a throwaway fork, not a holder.

    ``--fork-session`` makes the CLI resume a conversation and then write to a
    **new** session id, so it never touches the transcript it read.  It is
    therefore not "something else driving that conversation", which is the
    entire thing the foreign-holder scan exists to detect.  Counting it would
    make a session look held elsewhere for the seconds an aside takes to
    answer -- and the scan is what stops a session opening, so a false positive
    is not cosmetic.

    orchestrator2's own ``/btw`` forks exactly this way (see :mod:`btw`), and
    the SDK passes the flag literally: ``cmd.append("--fork-session")`` in
    ``_internal/transport/subprocess_cli.py``.  A fork started by *another*
    hub is skipped too, and should be: it is equally harmless to us.
    """
    return "--fork-session" in argv


def find_foreign_claude_for_session(session_id: str) -> list[ClaudeProc]:
    """Find ``claude`` processes resuming *session_id* outside our own tree.

    A match means something else is already driving that conversation — an
    orphan from a killed server, a second hub, or a ``claude --resume`` the
    user started in a terminal. Connecting anyway would put two agents on one
    session file and one working tree.

    Returns [] when nothing matches, when *session_id* is falsy, or when the
    scan can't run (psutil missing). A best-effort detector must never be the
    reason a session won't start, so every failure is a silent [].
    """
    if not session_id:
        return []
    try:
        import psutil
    except ImportError:
        log.debug("duplicate-session check: psutil not installed — skipped")
        return []

    try:
        mine = _own_tree_pids()
    except Exception:
        return []

    found: list[ClaudeProc] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if proc.info["pid"] in mine:
                continue
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("claude"):
                continue
            argv = proc.info.get("cmdline") or []
            # Match the *argument pair* rather than a substring of the whole
            # line: a bare id could appear in an unrelated --prompt or path.
            hit = any(
                a == "--resume" and i + 1 < len(argv) and argv[i + 1] == session_id
                for i, a in enumerate(argv)
            )
            if not hit:
                continue
            # A fork reads this conversation but writes to a new id, so it is
            # not holding anything.  See _is_session_fork.
            if _is_session_fork(argv):
                continue
            started = ""
            try:
                import datetime
                started = datetime.datetime.fromtimestamp(
                    proc.info["create_time"]).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            found.append(ClaudeProc(pid=proc.info["pid"], started=started,
                                    cmdline=" ".join(argv)))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return found


def map_foreign_session_holders() -> dict[str, ForeignHolder]:
    """Every session currently held by a ``claude`` outside our own tree.

    Same detection and exclusion rules as
    :func:`find_foreign_claude_for_session`, but as **one** process scan
    producing ``{session_id: ForeignHolder}`` instead of a scan per id.  That
    difference is the whole point: the lobby needs to classify up to forty
    sessions on every refresh, and forty full ``process_iter`` walks per
    refresh is not a thing anyone can ship.

    Parent lookups (which give the hub's port, and cost a ``net_connections``
    call apiece) are done once per *holder*, not once per session, and only for
    processes we actually matched.

    Best-effort throughout: any failure yields a smaller map, never an
    exception.  A session missing from the map is simply treated as not
    running elsewhere, which is the pre-existing behaviour.
    """
    try:
        import psutil
    except ImportError:
        return {}
    try:
        mine = _own_tree_pids()
    except Exception:
        return {}

    # pid -> (server_pid, port), so two sessions under one hub cost one lookup.
    parent_cache: dict[int, tuple[int | None, int | None]] = {}

    def _hub_of(pid: int) -> tuple[int | None, int | None]:
        if pid in parent_cache:
            return parent_cache[pid]
        server_pid = port = None
        try:
            parent = psutil.Process(pid).parent()
            if parent is not None:
                cmd = " ".join(parent.cmdline() or [])
                # Only one of *our* servers is a focusable hub; a terminal
                # `claude --resume` has a shell/OS parent with no port.
                if "server.py" in cmd:
                    server_pid = parent.pid
                    port = _listening_port_of(parent)
        except Exception:
            pass
        parent_cache[pid] = (server_pid, port)
        return server_pid, port

    out: dict[str, ForeignHolder] = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if proc.info["pid"] in mine:
                continue
            if not (proc.info.get("name") or "").lower().startswith("claude"):
                continue
            argv = proc.info.get("cmdline") or []
            sid = next(
                (argv[i + 1] for i, a in enumerate(argv)
                 if a == "--resume" and i + 1 < len(argv)),
                None,
            )
            if not sid or sid in out:
                continue          # first holder wins, as elsewhere
            if _is_session_fork(argv):
                continue          # reads it, does not hold it
            started = ""
            try:
                import datetime
                started = datetime.datetime.fromtimestamp(
                    proc.info["create_time"]).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            server_pid, port = _hub_of(proc.info["pid"])
            out[sid] = ForeignHolder(proc.info["pid"], started, server_pid, port)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return out


def describe_duplicates(session_id: str, procs: list[ClaudeProc]) -> str:
    """User-facing explanation of a refused connect."""
    lines = [
        f"Refusing to resume session {session_id[:8]}… — another Claude "
        f"process is already running it.",
        "",
        "Two agents on one session would share a session file and a working "
        "tree, and would commit over each other.",
        "",
        "Already running:",
    ]
    for p in procs:
        when = f", started {p.started}" if p.started else ""
        lines.append(f"  • PID {p.pid}{when}")
    lines += [
        "",
        "This is usually an orphan left behind when a server was killed "
        "(killing the server doesn't kill its claude.exe children on Windows; "
        "newer servers install a job object so it can't happen again).",
        "",
        f"Kill it with:  taskkill /PID {procs[0].pid} /T /F",
        "…then reconnect with /connect. To start anyway (you'll get two "
        "agents on one session), relaunch with --allow-duplicate-session.",
    ]
    return "\n".join(lines)


class ForeignHolder(NamedTuple):
    """A foreign ``claude`` holding a session, enriched with its hub window.

    ``port`` is the TCP port the orchestrator2 server that *owns* the holding
    process is listening on — i.e. the hub whose browser window is already
    driving the session, so the UI can offer to focus it instead of forking a
    doomed duplicate.  It is ``None`` when the holder is not a focusable hub
    child: a bare ``claude --resume`` in a terminal, an orphan whose parent
    server has died, or a process whose connections we can't read.
    """

    holder_pid: int
    started: str
    server_pid: int | None
    port: int | None


def _listening_port_of(proc) -> int | None:
    """First TCP port *proc* is LISTENing on, or None if none/unreadable.

    Best-effort: ``net_connections`` can raise ``AccessDenied`` on Windows, and
    the attribute was renamed from ``connections`` in psutil 6 — both degrade to
    None rather than raising, since this only gates an optional UI affordance.
    """
    try:
        import psutil
    except Exception:
        return None
    getter = getattr(proc, "net_connections", None) or getattr(proc, "connections", None)
    if getter is None:
        return None
    try:
        conns = getter(kind="inet")
    except Exception:
        return None
    for c in conns:
        try:
            if c.status == psutil.CONN_LISTEN and c.laddr:
                return int(c.laddr.port)
        except Exception:
            continue
    return None


def find_foreign_session_holder(session_id: str) -> ForeignHolder | None:
    """First foreign holder of *session_id*, plus its hub port when findable.

    Built on :func:`find_foreign_claude_for_session` (same exclusion rules).
    Best-effort throughout — any failure degrades to a ``None`` port, never an
    exception, so callers can gate UI on it without risk.
    """
    procs = find_foreign_claude_for_session(session_id)
    if not procs:
        return None
    holder = procs[0]
    server_pid: int | None = None
    port: int | None = None
    try:
        import psutil
        parent = psutil.Process(holder.pid).parent()
        if parent is not None:
            cmd = " ".join(parent.cmdline() or [])
            # Only a parent that is one of *our* servers is a focusable hub; a
            # terminal `claude --resume` has a shell/OS parent with no port.
            if "server.py" in cmd:
                server_pid = parent.pid
                port = _listening_port_of(parent)
    except Exception:
        pass
    return ForeignHolder(holder.pid, holder.started, server_pid, port)
