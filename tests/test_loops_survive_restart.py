"""A scheduled loop survives the hub being restarted; closing a session ends it.

Found 2026-09-27 while answering "if i restart the server ... how will i /move
it again?": every ``SDKBridge.stop()`` erased the session's wakeup record, and
the hub's own ways of going down -- the lobby's Shut down and ⟳ Restart, the
no-tabs auto-shutdown, Ctrl+C -- all stop their sessions that way.  So the
record ``wakeup_store`` keeps precisely so a loop outlives a restart was gone
before the restart finished, and ``_resurrect_scheduled_wakeups`` only ever
had anything to restore after a crash.

Now ``stop(hub_exiting=True)`` leaves it, and only the four whole-hub exits
pass that.  And the restore no longer skips a session that is already open --
which is always the primary, since a restart relaunches it with ``--resume``
-- so the primary's loop comes back too.

These run the real bridge and the real ``api_shutdown`` / ``close_runtime``.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                            # noqa: E402
import wakeup_store                                      # noqa: E402
from config import parse_args                            # noqa: E402
from session_runtime import SessionRuntime               # noqa: E402
from state import init_state_from_config                 # noqa: E402

SERVER_PY = Path(__file__).resolve().parent.parent / "server.py"


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """Sessions with real bridges, each with a loop armed, registered with the
    hub -- and a way to make more."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(server, "runtimes", {})
    monkeypatch.setattr(server, "_default_runtime", None)

    async def nothing(*a, **k):
        pass

    monkeypatch.setattr(server, "_broadcast_shutdown", nothing)
    monkeypatch.setattr(server, "_push_session_list", nothing)
    exits = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))

    def session(sid, prompt="Resume work now", due_in=900.0):
        from sdk_bridge import SDKBridge
        cwd = tmp_path / f"work-{sid[:4]}"
        cwd.mkdir(exist_ok=True)
        cfg = parse_args(["--cwd", str(cwd)])
        st = init_state_from_config(cfg)
        st.session_id = sid
        rt = SessionRuntime(config=cfg, state=st, rid=f"rt-{sid[:4]}")

        async def bc(m):
            pass

        rt.bridge = SDKBridge(config=cfg, state=st, broadcaster=bc)
        rt.bridge._arm_wakeup(due_in, prompt)
        server.runtimes[rt.rid] = rt
        return rt

    return {"session": session, "exits": exits}


def _record(rt):
    return wakeup_store.load_wakeup(rt.config.cwd, rt.state.session_id)


A = "aaaaaaaa-0000-0000-0000-000000000001"
B = "bbbbbbbb-0000-0000-0000-000000000002"


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------

def test_a_hub_exit_leaves_the_loop_for_the_next_start(hub):
    async def go():
        rt = hub["session"](A)
        await rt.bridge.stop(hub_exiting=True)
        return rt

    rt = asyncio.run(go())

    rec = _record(rt)
    assert rec is not None, "a hub exit erased the loop it exists to outlive"
    assert rec["prompt"] == "Resume work now"


def test_closing_a_session_still_ends_its_loop_for_good(hub):
    """Otherwise the next hub start would bring back a session the user shut."""
    async def go():
        rt = hub["session"](A)
        await rt.bridge.stop()
        return rt

    assert _record(asyncio.run(go())) is None


# --------------------------------------------------------------------------
# The hub
# --------------------------------------------------------------------------

def test_shutting_the_hub_down_keeps_every_sessions_loop(hub):
    """The lobby's Shut down -- and --detach taking over the port -- stop every
    session cleanly, which is exactly what used to erase them all."""
    async def go():
        a, b = hub["session"](A, "tick a"), hub["session"](B, "tick b")
        await server.api_shutdown()
        return a, b

    a, b = asyncio.run(go())

    assert _record(a)["prompt"] == "tick a"
    assert _record(b)["prompt"] == "tick b"


def test_the_lobby_close_still_ends_the_loop(hub):
    async def go():
        rt = hub["session"](A)
        await server.close_runtime(rt.rid)
        return rt

    assert _record(asyncio.run(go())) is None


def test_a_restart_brings_the_loop_back(hub, monkeypatch):
    """The whole story: shut down, come back up, and the loop is re-armed --
    due when it was, not restarted from zero."""
    async def down():
        hub["session"](A, "tick a", due_in=600)
        await server.api_shutdown()

    asyncio.run(down())

    # A fresh process: nothing running, and a reopen for the restore to use.
    monkeypatch.setattr(server, "runtimes", {})
    armed = []

    class _Br:
        def _arm_wakeup(self, delay, prompt):
            armed.append((delay, prompt))

    class _Rt:
        rid = "reopened"
        bridge = _Br()

        async def broadcast(self, m):
            pass

    async def fake_create(**kw):
        return _Rt()

    monkeypatch.setattr(server, "_create_runtime", fake_create)

    async def up():
        await server._resurrect_scheduled_wakeups()
        await asyncio.sleep(0.05)

    asyncio.run(up())

    [(delay, prompt)] = armed
    assert prompt == "tick a"
    assert 550 < delay <= 600


# --------------------------------------------------------------------------
# Which stops are which
# --------------------------------------------------------------------------

#: The four ways the whole hub goes down.  Every bridge stop inside them keeps
#: the loop; every other bridge stop is a session closing and must not.
HUB_EXITS = {"lifespan", "api_shutdown", "api_restart", "_shutdown_after_grace"}


def _bridge_stops():
    """(enclosing function, passes hub_exiting=True) for every bridge stop."""
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    found = []

    def is_bridge(node):
        return (isinstance(node, ast.Name) and node.id == "bridge") or \
               (isinstance(node, ast.Attribute) and node.attr == "bridge")

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            name = fn
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and child.func.attr == "stop" and is_bridge(child.func.value):
                keeps = any(k.arg == "hub_exiting"
                            and isinstance(k.value, ast.Constant)
                            and k.value.value is True for k in child.keywords)
                found.append((fn, keeps))
            walk(child, name)

    walk(tree, None)
    return found


def test_every_hub_exit_keeps_loops_and_nothing_else_does():
    """Pins the split in both directions.  A hub exit that erases loops is the
    bug; a session close that keeps one brings back a session the user shut,
    running a turn nobody asked for."""
    stops = _bridge_stops()

    assert {fn for fn, _ in stops} >= HUB_EXITS, \
        "a hub exit no longer stops its bridges where this test looks"
    wrong = [(fn, keeps) for fn, keeps in stops if keeps != (fn in HUB_EXITS)]
    assert not wrong, wrong
