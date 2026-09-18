"""A launch must not join a hub that reads paths differently than it writes them.

From a report: *"i ran server.py from within WSL, then went to localhost:8240
from windows, went into the session, and it says SDK connection failed (attempt
1/10: Failed to start Claude Code: [WinError 267] The directory name is
invalid), retrying in 2s…"*

WSL2 shares ``localhost`` with Windows, so the WSL launch probed port 8240,
found the **Windows** hub, and posted it a session request carrying its own
working directory as ``/mnt/d/mypage/inhahe.com``. The log shows exactly what
the Windows hub made of that::

    09:56:47  runtime s17 started (cwd=D:\\mnt\\d\\mypage\\inhahe.com, resume=None)
    09:56:47  connect: resume=None, cwd=D:\\mnt\\d\\mypage\\inhahe.com
    09:56:51  SDK connect failed (attempt 2): Failed to start Claude Code: ...

``Path.resolve()`` does not reject a POSIX absolute path on Windows — it anchors
it to the current drive, inventing ``D:\\mnt\\d\\mypage\\inhahe.com``, a
directory that has never existed. Then the connect loop retried it every 30 s
for the next nine minutes, reporting an error that does not contain the path.

Three separate things were wrong and each is pinned below: reuse ignored whether
the hub shares our filesystem, runtime creation accepted a directory nobody
could see, and the connect loop treated a permanent failure as a transient one.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                                   # noqa: E402
import server                                       # noqa: E402
from sdk_bridge import SDKBridge, UnusableCwd       # noqa: E402
from config import parse_args                       # noqa: E402
from state import init_state_from_config            # noqa: E402


# The exact path from the report, and what Windows silently turns it into.
WSL_CWD = "/mnt/d/mypage/inhahe.com"


# ---------------------------------------------------------------------------
# The mangling itself
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows path resolution")
def test_windows_invents_a_path_rather_than_rejecting_one():
    """The premise of the whole bug: ``resolve()`` doesn't fail, it fabricates.

    If this ever starts raising instead, the defences below become belt-and-
    braces rather than load-bearing — worth knowing either way.
    """
    from pathlib import Path

    resolved = str(Path(WSL_CWD).resolve(strict=False))
    assert resolved != WSL_CWD, "premise changed: the POSIX path survived intact"
    assert not Path(resolved).is_dir(), \
        f"{resolved!r} exists on this machine; pick a different fixture path"


# ---------------------------------------------------------------------------
# Namespace identity
# ---------------------------------------------------------------------------

def test_windows_and_wsl_are_different_namespaces(monkeypatch):
    """The two ends of the report must not compare equal.

    They share a loopback interface and nothing else that matters: an absolute
    path means a different thing to each.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    win = server._path_namespace()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    wsl = server._path_namespace()

    assert win != wsl


def test_two_wsl_distros_are_different_namespaces(monkeypatch):
    """They share ``/mnt/d`` but not ``/home`` — so they aren't interchangeable.

    Treating "is it Linux?" as the test would let a Debian launch hand an
    Ubuntu hub a home-directory path that resolves to a different tree.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    a = server._path_namespace()
    monkeypatch.setenv("WSL_DISTRO_NAME", "Debian")
    b = server._path_namespace()
    assert a != b


def test_plain_linux_is_not_mistaken_for_wsl(monkeypatch):
    """No ``WSL_DISTRO_NAME`` means a normal Linux host, which is its own case."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert server._path_namespace() == "linux"


# ---------------------------------------------------------------------------
# Hub reuse
# ---------------------------------------------------------------------------

def _fake_probe(monkeypatch, whoami: dict):
    """Make ``_probe_hub`` see a hub returning *whoami*, without a real server."""
    import io
    import json as _json
    import socket as _s

    class _Sock:
        def settimeout(self, *_a): pass
        def connect(self, *_a): pass
        def close(self): pass

    monkeypatch.setattr(server._socket, "socket", lambda *a, **k: _Sock())

    class _Resp:
        def __init__(self) -> None:
            self._b = io.BytesIO(_json.dumps(whoami).encode())
        def read(self): return self._b.read()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())


def test_a_foreign_namespace_hub_is_not_joined(monkeypatch, capsys):
    """The fix for the report, at the earliest point it can be made.

    Refusing here protects *everything* the launch would have handed over —
    ``config_dir`` and the resume id's project directory as much as ``cwd`` —
    rather than only the one field that happened to fail loudly.
    """
    monkeypatch.setattr(server, "_path_namespace", lambda: "wsl:Ubuntu")
    _fake_probe(monkeypatch, {"app": "orchestrator2", "namespace": "win32"})

    assert server._probe_hub(8240) is None, \
        "a WSL launch joined the Windows hub again"
    out = capsys.readouterr().out
    assert "win32" in out and "wsl:Ubuntu" in out, \
        "the refusal must name both sides, or it looks like the hub is missing"


def test_a_matching_namespace_hub_is_still_joined(monkeypatch):
    """Reuse is the normal path and must not become collateral damage.

    Splitting same-OS relaunches onto second ports would break the central hub
    outright — a far more common operation than the bug being fixed.
    """
    monkeypatch.setattr(server, "_path_namespace", lambda: "win32")
    _fake_probe(monkeypatch, {"app": "orchestrator2", "namespace": "win32"})
    assert server._probe_hub(8240) is not None


def test_a_hub_without_the_key_is_still_joined(monkeypatch):
    """An older hub predates ``namespace``; absence is not evidence of a mismatch.

    Refusing on a missing key would make the first launch after any upgrade
    silently start a second server. The server-side cwd check below is what
    covers that window instead.
    """
    monkeypatch.setattr(server, "_path_namespace", lambda: "win32")
    _fake_probe(monkeypatch, {"app": "orchestrator2"})
    assert server._probe_hub(8240) is not None


# ---------------------------------------------------------------------------
# The hub's own check
# ---------------------------------------------------------------------------

def test_a_runtime_is_not_created_for_a_directory_that_does_not_exist(tmp_path):
    """The backstop for an old launcher, a typo, or a deleted directory.

    ``_create_runtime`` is the single funnel for every way a session is born —
    the launch API, the lobby's *open* and *new* — so the check belongs here
    rather than in any one caller.
    """
    server.config = parse_args([])
    missing = str(tmp_path / "no-such-dir")

    async def go():
        with pytest.raises(NotADirectoryError) as ei:
            await server._create_runtime(cwd=missing)
        return str(ei.value)

    msg = asyncio.run(go())
    assert "no-such-dir" in msg, "the error must name the directory it rejected"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path resolution")
def test_the_reported_wsl_path_is_rejected_with_both_forms():
    """The report, end to end.

    Showing only the resolved form (``D:\\mnt\\d\\...``) would leave the user
    hunting for a path they never typed; showing only the requested form would
    hide *why* it failed. Both, or the message doesn't explain itself.
    """
    server.config = parse_args([])

    async def go():
        with pytest.raises(NotADirectoryError) as ei:
            await server._create_runtime(cwd=WSL_CWD)
        return str(ei.value)

    msg = asyncio.run(go())
    assert WSL_CWD in msg, "the path the launcher actually sent is missing"
    assert "mnt" in msg and ("D:" in msg or "d:" in msg.lower()), \
        "the resolved path Windows invented is missing"


def test_an_existing_directory_is_accepted(tmp_path, monkeypatch):
    """The check must not reject the normal case.

    Stops short of starting a bridge — this pins the validation, not runtime
    construction.
    """
    server.config = parse_args([])
    from pathlib import Path

    resolved = str(Path(str(tmp_path)).resolve(strict=False))
    assert Path(resolved).is_dir()
    # The validation is the first thing _create_runtime does; reaching anything
    # afterwards proves it passed.
    raised: list[str] = []

    def boom(*a, **k):
        raised.append("reached")
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "init_state_from_config", boom)

    async def go():
        with pytest.raises(RuntimeError):
            await server._create_runtime(cwd=str(tmp_path))

    asyncio.run(go())
    assert raised == ["reached"], "a real directory was rejected"


# ---------------------------------------------------------------------------
# The connect loop
# ---------------------------------------------------------------------------

def _bridge(argv=()):
    cfg = parse_args(list(argv))
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    return SDKBridge(config=cfg, state=state, broadcaster=bcast), state, sent


def test_connect_names_the_directory_instead_of_winerror_267(tmp_path, monkeypatch):
    """"[WinError 267] The directory name is invalid" does not say which one.

    That is the entire user-visible content of the reported failure, repeated
    ten times. The SDK can't do better — it never sees a name it could print —
    so the check has to happen on our side of the call.
    """
    missing = str(tmp_path / "gone")
    br, state, _ = _bridge(["--cwd", missing])
    created: list[str] = []
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient",
                        lambda options=None: created.append("client"))

    async def go():
        with pytest.raises(UnusableCwd) as ei:
            await br.connect()
        return str(ei.value)

    msg = asyncio.run(go())
    assert "gone" in msg, "the failure still doesn't name the directory"
    assert created == [], "a CLI subprocess was spawned for a missing directory"


def _run_connect_loop(br, state, timeout=3.0):
    """Drive the real ``worker_loop`` connect phase; return attempts made.

    The loop parks on ``event_queue`` once it gives up, so ``quit`` is how it
    is let go. A timeout here means it never gave up at all.
    """
    attempts: list[str] = []
    real_connect = br.connect

    async def counting_connect(*a, **k):
        attempts.append("try")
        return await real_connect(*a, **k)

    br.connect = counting_connect

    async def go():
        task = asyncio.ensure_future(br.worker_loop())
        # Let it reach the parked state, then release it.
        for _ in range(200):
            await asyncio.sleep(0.005)
            if attempts and br.event_queue.qsize() == 0:
                break
        br.event_queue.put_nowait(("quit", ""))
        await asyncio.wait_for(task, timeout)

    asyncio.run(go())
    return attempts


def test_a_missing_directory_is_tried_once_not_ten_times(tmp_path):
    """Retrying cannot make a directory exist.

    The log shows the cost of pretending otherwise: attempt 1 at 09:56:47 and
    still going at 10:05:11, one spawn attempt every 30 s for nine minutes,
    every one of them identical.
    """
    br, state, sent = _bridge(["--cwd", str(tmp_path / "gone")])
    attempts = _run_connect_loop(br, state)

    assert len(attempts) == 1, \
        f"a directory that cannot exist was tried {len(attempts)} times"


def test_a_queued_prompt_does_not_restart_an_unfixable_connect(tmp_path):
    """The retry-forever branch is the worse half of the loop's optimism.

    Ten attempts end; "keep trying while the user has something queued" does
    not — and a queued prompt is exactly what a user does when a session won't
    start. Against a missing directory that is an unbounded spin.
    """
    br, state, sent = _bridge(["--cwd", str(tmp_path / "gone")])
    state.queued_prompts.append("continue")
    attempts = _run_connect_loop(br, state)

    assert len(attempts) == 1, \
        f"a queued prompt restarted an unfixable connect ({len(attempts)} tries)"
    msgs = [m.get("data", {}).get("message", "")
            for m in sent if m.get("type") == "system_msg"]
    assert not any("Still can't connect" in m for m in msgs), \
        "the session entered the retry-forever loop on a permanent failure"


def test_the_fatal_message_does_not_promise_a_retry():
    """"Send a message to retry" is false here, and sends the user in circles.

    The reported session sat offering exactly that while every retry failed the
    same way. The message has to name the one action that can work: relaunch
    somewhere the server can see.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sdk_bridge.py"), encoding="utf-8").read()
    i = src.index("if _fatal:")
    block = src[i:i + 900]
    assert "relaunch orchestrator2" in block.lower()
    assert "WSL" in block, "the actual cause deserves a mention where it lands"
    assert "Send a message to retry" not in block
