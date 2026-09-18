"""A hub must be able to tell you it is running code that has since changed.

Found 2026-09-07 while diagnosing "a background task completed and didn't wake
the model". The answer was that the hub holding that session had been running
since **2026-09-01 22:59:30** -- one day *before* the very bug being reported
was fixed. The decisive evidence was a log line, ``cleared N stale bg task(s)
on reconnect``, that does not exist anywhere in the current source: it could
only have come from a process still executing the pre-fix version.

Python is read once at import. A server therefore goes on running whatever was
on disk when it started, for as long as it lives, and **says nothing**. Six
days of a fixed bug still happening, with the fix sitting on disk unread, and
no way to see it short of comparing process start times against a changelog.

Two rules the implementation has to get right, and both are about not crying
wolf:

* **Only project ``.py`` counts.** A third-party package upgrading does not
  make this hub stale, and its files change for reasons the operator did not
  cause and cannot act on from here.
* **Static assets do not count.** JS and CSS are re-read per request, so a
  browser refresh picks them up. Telling someone to restart the server for a
  CSS edit teaches them to ignore the notice, which costs more than the notice
  ever saves.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    server._source_baseline.clear()
    server._stale_cache = None
    yield
    server._source_baseline.clear()
    server._stale_cache = None


def _fake_module(monkeypatch, name, path):
    """Register a module whose ``__file__`` is *path*."""
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _touch(path, mtime):
    path.write_text("x", encoding="utf-8")
    import os
    os.utime(path, (mtime, mtime))


# ---------------------------------------------------------------------------
# What counts as a source file
# ---------------------------------------------------------------------------

def test_a_project_module_is_tracked():
    names = {n for n, _p in server._project_modules()}
    assert "server" in names, "did not notice its own source"


def test_third_party_modules_are_ignored():
    """`pytest` upgrading does not make this hub stale."""
    names = {n for n, _p in server._project_modules()}
    assert "pytest" not in names
    assert not any(n.startswith("json") for n in names), names


def test_only_python_is_tracked(monkeypatch):
    """A module claiming a non-.py file (namespace packages, frozen imports)
    is not something we can compare meaningfully."""
    _fake_module(monkeypatch, "_fake_notpy", ROOT / "static" / "lobby.js")
    names = {n for n, _p in server._project_modules()}
    assert "_fake_notpy" not in names


# ---------------------------------------------------------------------------
# Detecting the change
# ---------------------------------------------------------------------------

def test_a_fresh_snapshot_reports_nothing_stale(monkeypatch, tmp_path):
    src = ROOT / "_stale_probe_a.py"
    try:
        _touch(src, time.time() - 100)
        _fake_module(monkeypatch, "_stale_probe_a", src)
        server.snapshot_sources()
        assert server.stale_sources()["count"] == 0
    finally:
        src.unlink(missing_ok=True)


def test_a_file_edited_after_the_snapshot_is_reported(monkeypatch):
    """The whole point: the running code and the code on disk have diverged."""
    src = ROOT / "_stale_probe_b.py"
    try:
        _touch(src, time.time() - 100)
        _fake_module(monkeypatch, "_stale_probe_b", src)
        server.snapshot_sources()
        _touch(src, time.time())            # someone edits it
        server._stale_cache = None
        info = server.stale_sources()
        assert info["count"] == 1
        assert info["files"] == ["_stale_probe_b.py"]
    finally:
        src.unlink(missing_ok=True)


def test_an_older_file_is_not_reported(monkeypatch):
    """Only *newer* counts. A checkout that moves a file backwards in time is
    not the hub running ahead of disk, and reporting it would be noise."""
    src = ROOT / "_stale_probe_c.py"
    try:
        _touch(src, time.time())
        _fake_module(monkeypatch, "_stale_probe_c", src)
        server.snapshot_sources()
        _touch(src, time.time() - 500)
        server._stale_cache = None
        assert server.stale_sources()["count"] == 0
    finally:
        src.unlink(missing_ok=True)


def test_a_module_imported_after_the_snapshot_is_baselined_not_flagged(monkeypatch):
    """``sdk_bridge`` and friends load lazily; a genuinely late import is
    running *current* code, so calling it stale would be exactly backwards."""
    src = ROOT / "_stale_probe_d.py"
    try:
        server.snapshot_sources()           # taken before the module exists
        _touch(src, time.time())
        _fake_module(monkeypatch, "_stale_probe_d", src)
        server._stale_cache = None
        assert server.stale_sources()["count"] == 0, (
            "flagged a module it had never read as stale")
    finally:
        src.unlink(missing_ok=True)


def test_a_deleted_file_does_not_crash_the_check(monkeypatch):
    src = ROOT / "_stale_probe_e.py"
    _touch(src, time.time() - 100)
    _fake_module(monkeypatch, "_stale_probe_e", src)
    server.snapshot_sources()
    src.unlink()
    server._stale_cache = None
    assert server.stale_sources()["count"] == 0


def test_the_report_carries_when_the_process_started():
    """"14 files changed" means little without "and this has been up since
    Tuesday" -- the age is what makes the notice actionable."""
    info = server.stale_sources()
    assert info["started_at"] == server._started_at
    assert info["started_at"] <= time.time()


def test_the_file_list_is_capped():
    """A wholesale checkout must not put a hundred filenames in a banner."""
    src_dir = ROOT
    made = []
    try:
        base = time.time() - 100
        for i in range(20):
            p = src_dir / f"_stale_probe_many{i}.py"
            _touch(p, base)
            made.append(p)
        import types as _t
        for i, p in enumerate(made):
            m = _t.ModuleType(f"_stale_many{i}")
            m.__file__ = str(p)
            sys.modules[f"_stale_many{i}"] = m
        server.snapshot_sources()
        now = time.time()
        for p in made:
            _touch(p, now)
        server._stale_cache = None
        info = server.stale_sources()
        assert info["count"] == 20, info["count"]
        assert len(info["files"]) == 12, "the list was not capped"
    finally:
        for i in range(20):
            sys.modules.pop(f"_stale_many{i}", None)
        for p in made:
            p.unlink(missing_ok=True)


def test_the_check_is_cached(monkeypatch):
    """It rides every lobby tick, so it must not stat on every one."""
    calls = []
    real = server._project_modules
    monkeypatch.setattr(server, "_project_modules",
                        lambda: (calls.append(1), real())[1])
    server._stale_cache = None
    server.stale_sources()
    server.stale_sources()
    assert len(calls) == 1, "re-scanned inside the TTL"


def test_the_cache_expires(monkeypatch):
    calls = []
    real = server._project_modules
    monkeypatch.setattr(server, "_project_modules",
                        lambda: (calls.append(1), real())[1])
    server._stale_cache = None
    server.stale_sources()
    server._stale_cache = (
        time.monotonic() - server._STALE_CACHE_TTL - 0.01, server._stale_cache[1])
    server.stale_sources()
    assert len(calls) == 2, "a change would never be noticed"


# ---------------------------------------------------------------------------
# The baseline is actually taken
# ---------------------------------------------------------------------------
#
# Everything above tests `stale_sources()` against a baseline the test itself
# established. If the *production* call site vanished, the first real check
# would baseline whatever it found at that moment and nothing would ever be
# reported -- the feature silently absent rather than broken, which is the
# same shape as the bug it exists to catch.

def _run_deferred_startup(monkeypatch):
    """Drive ``_deferred_bridge_startup`` with the heavy import forced to fail.

    A stub ``sdk_bridge`` without ``SDKBridge`` makes the ``from ... import``
    raise immediately, so nothing is constructed and no CLI is spawned.
    """
    taken = []
    monkeypatch.setattr(server, "snapshot_sources",
                        lambda: (taken.append(1), 7)[1])
    monkeypatch.setitem(sys.modules, "sdk_bridge", types.ModuleType("sdk_bridge"))
    server._bridge_ready.clear()
    asyncio.run(server._deferred_bridge_startup(start=False))
    return taken


def test_startup_takes_the_baseline(monkeypatch):
    assert _run_deferred_startup(monkeypatch) == [1], (
        "nothing ever records what this process is running")


def test_the_baseline_is_taken_even_when_the_bridge_fails_to_start(monkeypatch):
    """A hub whose bridge died is still a hub running code, and is still
    exactly as capable of going stale. It is also the case where an operator
    is most likely to be editing and restarting."""
    _run_deferred_startup(monkeypatch)
    assert server._bridge_ready.is_set(), "fixture did not exercise the failure path"


# ---------------------------------------------------------------------------
# Reaching the lobby
# ---------------------------------------------------------------------------

def _payload(monkeypatch, include_recent=True):
    monkeypatch.setattr(server, "runtimes", {})
    server._holders_cache = (float("inf"), {})

    async def go():
        loop = asyncio.get_running_loop()
        orig = loop.run_in_executor

        async def fake_exec(_ex, fn, *a):
            if fn is server._recent_disk_sessions:
                return []
            return fn(*a)

        loop.run_in_executor = fake_exec        # type: ignore[assignment]
        try:
            return await server._session_list_payload(include_recent)
        finally:
            loop.run_in_executor = orig         # type: ignore[assignment]

    try:
        return asyncio.run(go())
    finally:
        server._holders_cache = None


def test_the_session_list_carries_the_staleness(monkeypatch):
    monkeypatch.setattr(server, "stale_sources",
                        lambda: {"count": 3, "files": ["a.py"],
                                 "newest": 1.0, "started_at": 2.0})
    assert _payload(monkeypatch)["stale"]["count"] == 3


def test_the_fast_landing_payload_carries_it_too(monkeypatch):
    """The landing lobby renders before the disk scan finishes. "This hub is
    running old code" is precisely what you want *before* you start reading
    its session list for clues."""
    monkeypatch.setattr(server, "stale_sources",
                        lambda: {"count": 3, "files": [], "newest": None,
                                 "started_at": 2.0})
    msg = _payload(monkeypatch, include_recent=False)
    assert msg["recent_pending"] is True
    assert msg["stale"]["count"] == 3
