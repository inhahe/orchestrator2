"""Tests for the multi-session hub: SessionRuntime + registry lifecycle.

These cover the Phase 1-4 machinery that turns the server into a hub hosting
several live sessions at once:

* ``SessionRuntime`` — per-session state, client set, ``meta()``, ``broadcast``.
* Idle-teardown timers — arming rules and actual teardown.
* Registry teardown — dropping a runtime, never the default one.
* User-initiated close — the lobby's per-session × button.
* Lobby watchers — the live-update opt-in for open lobby overlays.

They avoid the real Claude SDK entirely by registering hand-built runtimes with
fake bridges/state, so they run fast and offline.  Async helpers are driven with
``asyncio.run`` so no pytest-asyncio plugin is needed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Import the modules under test (repo root is this file's parent's parent).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from session_runtime import SessionRuntime  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeWS:
    """Minimal stand-in for a Starlette WebSocket: records what was sent."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send_text(self, data: str) -> None:
        if self.fail:
            raise RuntimeError("socket broken")
        self.sent.append(json.loads(data))


class FakeBridge:
    """Stand-in for SDKBridge: records that stop() was awaited.

    ``stop()`` **suspends**, because the real one does — it awaits
    ``cancel_and_join`` on the worker task and then ``disconnect()``.  A
    ``stop()`` with no await point runs atomically and can therefore never be
    interrupted part-way, which is exactly what hid the self-cancelling
    teardown bug from this suite for three weeks: the fake always finished, so
    ``stopped`` was always True no matter what cancellation the caller took.

    ``entered`` / ``stopped`` bracket the suspension so a test can tell "never
    called" apart from "called but abandoned half-way".
    """

    def __init__(self) -> None:
        self.entered = False
        self.stopped = False

    async def stop(self) -> None:
        self.entered = True
        await asyncio.sleep(0)      # a real stop() yields; so must this one
        self.stopped = True


def make_runtime(rid=None, *, cwd="C:/proj", session_id="sid-1",
                 title=None, busy=False) -> SessionRuntime:
    state = SimpleNamespace(
        session_id=session_id, session_title=title, busy=busy,
        queued_prompts=[],
    )
    cfg = SimpleNamespace(cwd=cwd, session_idle_timeout=300)
    rt = SessionRuntime(config=cfg, state=state, rid=rid)
    rt.bridge = FakeBridge()
    return rt


# ---------------------------------------------------------------------------
# Global-state isolation: snapshot/restore server module globals per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_server_globals(monkeypatch):
    saved = {
        "runtimes": dict(server.runtimes),
        "lobby_clients": set(server.lobby_clients),
        "_lobby_watchers": set(server._lobby_watchers),
        "_ws_runtime": dict(server._ws_runtime),
        "_default_runtime": server._default_runtime,
        "config": server.config,
        # Closing the primary session clears these legacy shadows of the
        # default runtime, so a test that does it must not leak that into the
        # next one (they were already being clobbered by the routing test).
        "state": server.state,
        "bridge": server.bridge,
    }
    server.runtimes.clear()
    server.lobby_clients.clear()
    server._lobby_watchers.clear()
    server._ws_runtime.clear()
    server._default_runtime = None
    server.config = SimpleNamespace(session_idle_timeout=300)
    # The lobby list scans the filesystem for recent sessions; stub it out so
    # tests stay offline and deterministic.
    monkeypatch.setattr(server, "_recent_disk_sessions", lambda limit=40: [])
    try:
        yield
    finally:
        server.runtimes.clear()
        server.runtimes.update(saved["runtimes"])
        server.lobby_clients.clear()
        server.lobby_clients.update(saved["lobby_clients"])
        server._lobby_watchers.clear()
        server._lobby_watchers.update(saved["_lobby_watchers"])
        server._ws_runtime.clear()
        server._ws_runtime.update(saved["_ws_runtime"])
        server._default_runtime = saved["_default_runtime"]
        server.config = saved["config"]
        server.state = saved["state"]
        server.bridge = saved["bridge"]


# ---------------------------------------------------------------------------
# SessionRuntime
# ---------------------------------------------------------------------------

def test_rid_is_unique_and_stable():
    a = make_runtime()
    b = make_runtime()
    assert a.rid != b.rid
    assert a.rid == a.rid  # stable across calls


def test_meta_reports_clients_and_state():
    rt = make_runtime(rid="s9", cwd="D:/work", session_id="abc",
                      title="My Session", busy=True)
    ws1, ws2 = FakeWS(), FakeWS()
    rt.add_client(ws1)
    rt.add_client(ws2)
    meta = rt.meta()
    assert meta["rid"] == "s9"
    assert meta["cwd"] == "D:/work"
    assert meta["session_id"] == "abc"
    assert meta["title"] == "My Session"
    assert meta["busy"] is True
    assert meta["viewers"] == 2


def test_add_discard_client_updates_viewers():
    rt = make_runtime()
    ws = FakeWS()
    rt.add_client(ws)
    assert rt.meta()["viewers"] == 1
    rt.discard_client(ws)
    assert rt.meta()["viewers"] == 0


def test_broadcast_reaches_clients_and_prunes_stale():
    rt = make_runtime()
    good = FakeWS()
    bad = FakeWS(fail=True)
    rt.add_client(good)
    rt.add_client(bad)
    asyncio.run(rt.broadcast({"type": "ping"}))
    assert good.sent == [{"type": "ping"}]
    # The broken socket is dropped so it isn't retried forever.
    assert bad not in rt.clients
    assert good in rt.clients


# ---------------------------------------------------------------------------
# Idle-teardown timer arming rules
# ---------------------------------------------------------------------------

def test_idle_timer_not_armed_for_default_runtime():
    async def go():
        rt = make_runtime()
        server._default_runtime = rt
        server._maybe_start_idle_timer(rt)
        assert rt.idle_timer is None
    asyncio.run(go())


def test_idle_timer_not_armed_when_viewers_present():
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        rt.add_client(FakeWS())
        server._maybe_start_idle_timer(rt)
        assert rt.idle_timer is None
    asyncio.run(go())


def test_idle_timer_disabled_when_timeout_non_positive():
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        server.config = SimpleNamespace(session_idle_timeout=0)
        server._maybe_start_idle_timer(rt)
        assert rt.idle_timer is None
    asyncio.run(go())


def test_idle_timer_armed_for_viewerless_secondary():
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        server._maybe_start_idle_timer(rt)
        assert rt.idle_timer is not None
        server._cancel_idle_timer(rt)   # don't leak the task
        assert rt.idle_timer is None
    asyncio.run(go())


# ---------------------------------------------------------------------------
# Idle teardown + registry lifecycle
# ---------------------------------------------------------------------------

def test_idle_teardown_removes_runtime_and_stops_bridge():
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        # timeout 0 → fire immediately.
        await server._idle_teardown_after(rt, 0)
        assert rt.rid not in server.runtimes
        assert rt.bridge.stopped is True
    asyncio.run(go())


def test_idle_teardown_skipped_if_viewer_returns():
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        rt.add_client(FakeWS())          # a viewer came back
        await server._idle_teardown_after(rt, 0)
        assert rt.rid in server.runtimes  # not torn down
        assert rt.bridge.stopped is False
    asyncio.run(go())


def test_cancelled_idle_timer_does_not_teardown():
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        server._maybe_start_idle_timer(rt)
        timer = rt.idle_timer
        server._cancel_idle_timer(rt)
        # Let the cancellation propagate.
        with pytest.raises(asyncio.CancelledError):
            await timer
        assert rt.rid in server.runtimes
        assert rt.bridge.stopped is False
    asyncio.run(go())


def test_teardown_skips_default_runtime():
    async def go():
        rt = make_runtime()
        server._default_runtime = rt
        server.runtimes[rt.rid] = rt
        await server._teardown_runtime(rt)
        assert rt.rid in server.runtimes   # default is never dropped
        assert rt.bridge.stopped is False
    asyncio.run(go())


def test_teardown_secondary_drops_and_stops():
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        await server._teardown_runtime(rt)
        assert rt.rid not in server.runtimes
        assert rt.bridge.stopped is True
    asyncio.run(go())


def test_teardown_from_its_own_idle_timer_still_stops_the_bridge():
    """The timer task must not cancel itself out of its own teardown.

    ``rt.idle_timer`` *is* the task running ``_idle_teardown_after`` →
    ``_teardown_runtime``.  Cancelling it from inside that call chain delivers
    a ``CancelledError`` at the next await — which is ``await rt.bridge.stop()``
    — so the bridge is abandoned mid-shutdown.  ``CancelledError`` is not an
    ``Exception``, so the ``except Exception`` around it logs nothing and the
    task ends *cancelled* rather than failed: no traceback, no "torn down"
    line, no hung event loop.  Nothing anywhere says it happened.

    What it costs is a whole ``claude.exe``.  ``stop()`` dies before
    ``disconnect()``, so the CLI subprocess is never terminated, while the
    runtime *has* already been popped from ``runtimes`` — leaving an invisible
    live agent that the launch-time duplicate check can no longer see.  A hub
    up for a day accumulated 19 of them, several resuming the same session.

    The existing teardown tests all miss this because they await
    ``_idle_teardown_after``/``_teardown_runtime`` as plain coroutines, so
    ``rt.idle_timer`` is None and there is nothing to self-cancel.  Going
    through ``_maybe_start_idle_timer`` is the whole point of this test.
    """
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        server.config = SimpleNamespace(session_idle_timeout=0.01)
        server._maybe_start_idle_timer(rt)
        timer = rt.idle_timer
        assert timer is not None
        await asyncio.wait({timer})          # absorb cancellation, don't re-raise

        assert rt.rid not in server.runtimes
        assert rt.bridge.entered is True, "teardown never reached bridge.stop()"
        assert rt.bridge.stopped is True, (
            "bridge.stop() was abandoned part-way — the teardown cancelled the "
            "very task it was running on, so the CLI subprocess is never killed"
        )
        assert not timer.cancelled(), (
            "the idle-timer task ended cancelled: it cancelled itself out of "
            "its own teardown"
        )
    asyncio.run(go())


# ---------------------------------------------------------------------------
# User-initiated close — the lobby's per-session × button
# ---------------------------------------------------------------------------
#
# Closing a session is *not* the same operation as the idle teardown that
# shares its plumbing.  The idle path only ever fires on a viewer-less
# secondary runtime; a close fires on whatever the user pointed at, which can
# be the primary session and can have tabs watching it.  Both of those were
# hard "return early" cases in the teardown before, so they're what these
# tests pin down.


def _drain(loop_iterations: int = 3):
    """Let create_task()-ed follow-ups (e.g. the deferred recent-list push)
    run, so the event loop doesn't close with pending tasks."""
    async def _go():
        for _ in range(loop_iterations):
            await asyncio.sleep(0)
    return _go()


def test_close_stops_the_bridge_and_drops_the_runtime():
    async def go():
        rt = make_runtime(rid="s5")
        server.runtimes[rt.rid] = rt
        result = await server.close_runtime("s5")
        assert result["ok"] is True
        assert result["rid"] == "s5"
        assert "s5" not in server.runtimes
        assert rt.bridge.stopped is True
        await _drain()
    asyncio.run(go())


def test_close_of_an_unknown_rid_is_reported_not_raised():
    async def go():
        result = await server.close_runtime("nope")
        assert result["ok"] is False
        assert result["error"]
    asyncio.run(go())


def test_close_applies_to_the_primary_session_too():
    """The whole point of the button in the common case.

    A hub launched in one folder hosts exactly one session — the default
    runtime.  ``_teardown_runtime`` refuses that one (the idle timer must never
    take the primary down), so an unforced close would leave the button doing
    nothing precisely when it's most wanted.  The primary is demoted instead.
    """
    async def go():
        rt = make_runtime(rid="s1")
        server.runtimes[rt.rid] = rt
        server._default_runtime = rt
        server.state = rt.state
        server.bridge = rt.bridge

        result = await server.close_runtime("s1")

        assert result["ok"] is True
        assert "s1" not in server.runtimes
        assert rt.bridge.stopped is True, "the primary's CLI must actually stop"
        # The hub carries on with no primary — and nothing is left pointing at
        # the stopped session.
        assert server._default_runtime is None
        assert server.state is None
        assert server.bridge is None
        await _drain()
    asyncio.run(go())


def test_the_idle_timer_still_never_takes_the_primary_down():
    """Demotion is only reachable through an explicit close.  An idle primary
    (all its tabs closed for the night) must still be there in the morning."""
    async def go():
        rt = make_runtime()
        server.runtimes[rt.rid] = rt
        server._default_runtime = rt
        await server._idle_teardown_after(rt, 0)
        assert rt.rid in server.runtimes
        assert rt.bridge.stopped is False
    asyncio.run(go())


def test_closing_a_watched_session_sends_its_viewers_back_to_the_lobby():
    """A tab left attached to a closed session is the worst outcome here: it
    still shows the transcript and still accepts typing, into a runtime whose
    CLI is being killed.  Viewers must be evacuated, told why, and unwired."""
    async def go():
        rt = make_runtime(rid="s7")
        server.runtimes[rt.rid] = rt
        ws = FakeWS()
        rt.add_client(ws)
        server._ws_runtime[ws] = rt

        await server.close_runtime("s7")

        types = [m["type"] for m in ws.sent]
        assert "clear_screen" in types, "the dead session's transcript must go"
        assert "session_closed" in types
        assert "session_list" in types, "the tab needs the lobby to pick from"
        closed = next(m for m in ws.sent if m["type"] == "session_closed")
        assert closed["rid"] == "s7"
        assert closed.get("message")
        # The socket is a lobby tab again, attached to nothing.
        assert server._ws_runtime.get(ws) is None
        assert ws in server.lobby_clients
        assert ws not in rt.clients
        await _drain()
    asyncio.run(go())


def test_evacuating_viewers_does_not_arm_a_timer_on_the_dead_runtime():
    """``_enter_lobby`` arms the idle timer of the runtime each tab is leaving
    — which, during a close, is the runtime being torn down.  Arming a
    countdown to tear down something already gone would leave a live task
    holding a dead runtime and log a second teardown for a closed session."""
    async def go():
        rt = make_runtime(rid="s8")
        server.runtimes[rt.rid] = rt
        ws = FakeWS()
        rt.add_client(ws)
        server._ws_runtime[ws] = rt

        await server.close_runtime("s8")

        assert rt.idle_timer is None
        assert rt.idle_deadline is None
        await _drain()
    asyncio.run(go())


def test_viewers_are_evacuated_before_the_bridge_is_stopped():
    """Stopping a CLI can take seconds.  If the tabs were only released
    afterwards, the user would sit looking at a session they had just closed,
    able to type into it, for the whole of that."""
    order: list[str] = []

    class SlowBridge(FakeBridge):
        async def stop(self) -> None:
            order.append("bridge-stopped")
            await super().stop()

    async def go():
        rt = make_runtime(rid="s11")
        rt.bridge = SlowBridge()
        server.runtimes[rt.rid] = rt

        class RecordingWS(FakeWS):
            async def send_text(self, data):
                msg = json.loads(data)
                if msg.get("type") == "session_closed":
                    order.append("viewer-released")
                await super().send_text(data)

        ws = RecordingWS()
        rt.add_client(ws)
        server._ws_runtime[ws] = rt

        await server.close_runtime("s11")
        assert order == ["viewer-released", "bridge-stopped"]
        await _drain()
    asyncio.run(go())


def test_lobby_close_message_closes_and_refreshes_the_list():
    async def go():
        rt = make_runtime(rid="s3")
        server.runtimes[rt.rid] = rt
        ws = FakeWS()               # a tab viewing some *other* session
        handled = await server._handle_lobby_message(
            ws, {"type": "close", "rid": "s3"})
        assert handled is True
        assert "s3" not in server.runtimes
        assert rt.bridge.stopped is True
        # The closer sees the session disappear even though it isn't a viewer
        # of it (and so isn't reached by the teardown's lobby broadcast).
        lists = [m for m in ws.sent if m["type"] == "session_list"]
        assert lists and lists[-1]["running"] == []
        await _drain()
    asyncio.run(go())


def test_lobby_close_of_a_stale_rid_just_refreshes():
    """Two clicks, or a click on a card for a session the idle timer already
    reaped.  Nothing to close is not an error — but the list must refresh so
    the stale card goes away."""
    async def go():
        ws = FakeWS()
        handled = await server._handle_lobby_message(
            ws, {"type": "close", "rid": "gone"})
        assert handled is True
        assert any(m["type"] == "session_list" for m in ws.sent)
        await _drain()
    asyncio.run(go())


def test_http_close_endpoint_mirrors_the_button():
    """``POST /api/session/close`` — the escape hatch for stopping a session
    without a browser (the reason this feature exists at all was not knowing
    how to stop a session other than by hunting for a PID)."""
    async def go():
        rt = make_runtime(rid="s4")
        server.runtimes[rt.rid] = rt
        ok = await server.api_session_close({"rid": "s4"})
        assert ok["ok"] is True
        assert "s4" not in server.runtimes
        assert rt.bridge.stopped is True
        missing = await server.api_session_close({})
        assert missing["ok"] is False
        await _drain()
    asyncio.run(go())


# ---------------------------------------------------------------------------
# The lobby button itself (no JS test runner here — assert the wiring exists)
# ---------------------------------------------------------------------------

_STATIC = Path(__file__).resolve().parent.parent / "static"


def test_the_close_button_is_rendered_and_sends_close():
    src = (_STATIC / "lobby.js").read_text(encoding="utf-8")
    assert "lobby-card-close" in src, "no × on the running-session cards"
    assert "type: 'close'" in src, "the button doesn't send the close message"


def test_the_close_click_is_claimed_before_the_open_click():
    """The × lives *inside* the card, whose click opens the session.  If the
    card's handler ran first, closing a session would also open it."""
    src = (_STATIC / "lobby.js").read_text(encoding="utf-8")
    close_at = src.index("lobby-card-close')")     # the guard in the handler
    open_at = src.index("_focusOrOpen('/?rid=")
    assert close_at < open_at


def test_closing_is_confirmed_before_it_happens():
    """It kills a live process; a stray click must not be enough."""
    src = (_STATIC / "lobby.js").read_text(encoding="utf-8")
    body = src[src.index("function _closeSession"):src.index("function _shutdownServer")]
    assert "window.confirm" in body
    assert body.index("window.confirm") < body.index("App.send")


def test_the_frontend_handles_session_closed():
    app = (_STATIC / "app.js").read_text(encoding="utf-8")
    lobby = (_STATIC / "lobby.js").read_text(encoding="utf-8")
    assert "session_closed" in app
    assert "onSessionClosed" in app and "onSessionClosed" in lobby
    # It must be exported, or app.js's call is a TypeError at the worst moment.
    assert "onSessionClosed," in lobby.split("return {")[-1]


# ---------------------------------------------------------------------------
# Session-list payload + lobby watchers
# ---------------------------------------------------------------------------

def test_session_list_payload_lists_running_sorted():
    a = make_runtime(rid="s1", session_id="a")
    b = make_runtime(rid="s2", session_id="b")
    a.last_activity = 100
    b.last_activity = 200
    server.runtimes[a.rid] = a
    server.runtimes[b.rid] = b
    payload = asyncio.run(server._session_list_payload())
    assert payload["type"] == "session_list"
    rids = [m["rid"] for m in payload["running"]]
    assert rids == ["s2", "s1"]   # most-recently-active first
    assert payload["recent"] == []


def test_lobby_watch_on_registers_and_sends_snapshot():
    async def go():
        ws = FakeWS()
        handled = await server._handle_lobby_message(
            ws, {"type": "lobby_watch", "on": True})
        assert handled is True
        assert ws in server._lobby_watchers
        # An immediate session_list snapshot was pushed.
        assert any(m["type"] == "session_list" for m in ws.sent)
    asyncio.run(go())


def test_lobby_watch_off_unregisters():
    async def go():
        ws = FakeWS()
        server._lobby_watchers.add(ws)
        handled = await server._handle_lobby_message(
            ws, {"type": "lobby_watch", "on": False})
        assert handled is True
        assert ws not in server._lobby_watchers
    asyncio.run(go())


def test_lobby_targets_is_union():
    a, b = FakeWS(), FakeWS()
    server.lobby_clients.add(a)
    server._lobby_watchers.add(b)
    targets = server._lobby_targets()
    assert targets == {a, b}


def test_lobby_broadcast_reaches_both_sets_and_prunes_stale():
    async def go():
        lobby = FakeWS()
        watcher = FakeWS()
        broken = FakeWS(fail=True)
        server.lobby_clients.add(lobby)
        server._lobby_watchers.add(watcher)
        server._lobby_watchers.add(broken)
        await server.lobby_broadcast({"type": "session_list", "running": []})
        assert lobby.sent and watcher.sent
        # The broken watcher is pruned from both sets.
        assert broken not in server._lobby_watchers
        assert broken not in server.lobby_clients
    asyncio.run(go())


def test_cleanup_ws_discards_lobby_watcher():
    ws = FakeWS()
    server._lobby_watchers.add(ws)
    server._cleanup_ws(ws)
    assert ws not in server._lobby_watchers


def test_lobby_attach_unknown_rid_errors():
    async def go():
        ws = FakeWS()
        handled = await server._handle_lobby_message(
            ws, {"type": "attach", "rid": "does-not-exist"})
        assert handled is True
        # An error system_msg plus a refreshed session_list were sent.
        types = [m["type"] for m in ws.sent]
        assert "system_msg" in types
        assert "session_list" in types
        # ...and a lobby_notice too: on mobile the lobby overlay stays open
        # across an in-place attach and covers the chat, so a system_msg alone
        # would be invisible and the tap would look ignored.
        assert "lobby_notice" in types
        notice = next(m for m in ws.sent if m["type"] == "lobby_notice")
        assert notice.get("message")
    asyncio.run(go())


def test_lobby_open_without_session_id_notifies_overlay():
    """A failed ``open`` must surface on the lobby overlay, not just the chat.

    Same reason as the attach case: the mobile lobby switches sessions in place
    with the overlay still covering the chat.
    """
    async def go():
        ws = FakeWS()
        handled = await server._handle_lobby_message(
            ws, {"type": "open", "session_id": "   "})
        assert handled is True
        types = [m["type"] for m in ws.sent]
        assert "system_msg" in types
        assert "lobby_notice" in types
    asyncio.run(go())


# ---------------------------------------------------------------------------
# Message routing for unattached (lobby) sockets
# ---------------------------------------------------------------------------

def test_unattached_socket_message_auto_attaches_and_is_visible():
    """A ``message`` from a socket that isn't attached to any runtime (e.g. it
    reloaded into the lobby) must NOT be silently queued into the default
    session with no feedback.  Regression: ``_runtime_for_ws``'s default-runtime
    fallback used to route the prompt into the default queue while the sender —
    not being in that runtime's client set — never saw the ``queue_update``, so
    the prompt appeared to vanish (then all pending prompts surfaced at once
    the next time the tab attached).  The socket should now be auto-attached and
    receive the queue update immediately.
    """
    from state import State
    from config import parse_args

    async def go():
        cfg = parse_args([])
        st = State()
        st.busy = True                     # a turn is running → prompt queues
        rt = SessionRuntime(config=cfg, state=st, rid="R1")
        rt.bridge = FakeBridge()
        server.runtimes[rt.rid] = rt
        server._default_runtime = rt
        server.state = st
        server.bridge = rt.bridge
        server.config = cfg

        # Socket is sitting in the lobby — NOT attached to any runtime.
        ws = FakeWS()
        server.lobby_clients.add(ws)

        await server._handle_ws_message(
            ws, {"type": "message", "text": "hello", "client_echoed": False})

        # The prompt was queued once (not duplicated)...
        assert list(st.queued_prompts) == ["hello"]
        # ...the socket is now a genuine viewer of the runtime...
        assert ws in rt.clients
        assert server._ws_runtime.get(ws) is rt
        # ...and it actually received feedback: the initial state on attach
        # plus a queue_update reflecting the pending prompt.
        types = [m["type"] for m in ws.sent]
        assert "attached" in types
        assert "queue_update" in types
        last_queue = [m for m in ws.sent if m["type"] == "queue_update"][-1]
        assert [i["text"] for i in last_queue["queue"]] == ["hello"]

    asyncio.run(go())


# ---------------------------------------------------------------------------
# External-access auth middleware
# ---------------------------------------------------------------------------

async def _drive_auth(mw, scope):
    """Run the middleware over *scope*; return (inner_ran, sent_messages)."""
    sent: list[dict] = []
    ran = {"inner": False}

    async def inner_app(s, r, snd):
        ran["inner"] = True
        if s["type"] == "http":
            await snd({"type": "http.response.start", "status": 200, "headers": []})
            await snd({"type": "http.response.body", "body": b"ok"})

    mw.app = inner_app

    async def receive():
        return {"type": "websocket.connect"}

    async def send(m):
        sent.append(m)

    await mw(scope, receive, send)
    return ran["inner"], sent


def _http_scope(ip, headers=None, qs=b""):
    return {"type": "http", "scheme": "http", "client": (ip, 50000),
            "headers": headers or [], "query_string": qs}


def _ws_scope(ip, headers=None, qs=b""):
    return {"type": "websocket", "scheme": "ws", "client": (ip, 50000),
            "headers": headers or [], "query_string": qs}


def test_auth_lan_client_passes_without_credentials():
    import asyncio as _a
    mw = server._ExternalAuthMiddleware(None, password="pw")
    ran, _ = _a.run(_drive_auth(mw, _http_scope("192.168.1.9")))
    assert ran is True


def test_auth_external_http_basic_sets_cookie_and_ws_accepts_it():
    """The crux of external access: an HTTP page authenticates via Basic Auth
    and receives an ``orch2_auth`` cookie; the follow-up WebSocket upgrade —
    which browsers do NOT send Basic credentials on — then authenticates purely
    via that cookie.  Regression: without the cookie the WS was rejected (close
    1008) even though the password was correct, so external sign-in looked
    broken.
    """
    import asyncio as _a
    import base64 as _b64

    mw = server._ExternalAuthMiddleware(None, password="test-pw")

    # Blank-username Basic Auth (what the browser sends for user-less login).
    hdr = b"Basic " + _b64.b64encode(b":test-pw")
    ran, sent = _a.run(_drive_auth(mw, _http_scope("8.8.8.8", [(b"authorization", hdr)])))
    assert ran is True
    set_cookie = None
    for m in sent:
        if m.get("type") == "http.response.start":
            for k, v in m["headers"]:
                if k == b"set-cookie":
                    set_cookie = v.decode()
    assert set_cookie and set_cookie.startswith(f"{server._AUTH_COOKIE}={mw.token}")

    # WS upgrade with only the cookie (no Authorization header) → accepted.
    cookie = f"{server._AUTH_COOKIE}={mw.token}".encode()
    ran, sent = _a.run(_drive_auth(mw, _ws_scope("8.8.8.8", [(b"cookie", cookie)])))
    assert ran is True
    assert not any(m.get("type") == "websocket.close" for m in sent)


def test_auth_external_ws_without_credentials_rejected():
    import asyncio as _a
    mw = server._ExternalAuthMiddleware(None, password="pw")
    ran, sent = _a.run(_drive_auth(mw, _ws_scope("8.8.8.8")))
    assert ran is False
    closes = [m for m in sent if m.get("type") == "websocket.close"]
    assert closes and closes[0]["code"] == 1008


def test_auth_external_ws_password_query_param_accepted():
    import asyncio as _a
    mw = server._ExternalAuthMiddleware(None, password="test-pw")
    ran, _ = _a.run(_drive_auth(mw, _ws_scope("8.8.8.8", qs=b"password=test-pw")))
    assert ran is True


def test_auth_disabled_when_no_password_sends_no_challenge():
    """password=None blocks external access without a Basic-Auth challenge, so
    users aren't prompted for a password that can never be accepted."""
    import asyncio as _a
    mw = server._ExternalAuthMiddleware(None, password=None)
    ran, sent = _a.run(_drive_auth(mw, _http_scope("8.8.8.8")))
    assert ran is False
    start = [m for m in sent if m.get("type") == "http.response.start"][0]
    assert start["status"] == 401
    assert not any(k == b"www-authenticate" for k, v in start["headers"])


def test_external_password_resolution_precedence(monkeypatch):
    """Flag beats env var, and neither one alone opens the door.

    Rewritten 2026-09-03: this used to assert that an unconfigured server fell
    back to a built-in default password (``"uncommon11"``).  That default was
    removed — a password published in the source is not a password — so the
    property under test is now "flag > env, and nothing means nothing".
    Off-by-default and the on-without-a-password refusal live in
    ``tests/test_external_access_policy.py``.
    """
    from config import parse_args

    monkeypatch.delenv("ORCH2_EXTERNAL_PASSWORD", raising=False)
    monkeypatch.setenv("ORCH2_EXTERNAL_ACCESS", "on")

    # 1. Nothing specified → no password at all, so nobody gets in.
    assert server._resolve_external_password(parse_args([])) is None

    # 2. Env var set, no flag → env var supplies it.
    monkeypatch.setenv("ORCH2_EXTERNAL_PASSWORD", "from-env")
    assert server._resolve_external_password(parse_args([])) == "from-env"

    # 3. Explicit flag beats the env var.
    cfg = parse_args(["--external-password", "from-flag"])
    assert server._resolve_external_password(cfg) == "from-flag"

    # 4. An empty flag is not a password.
    assert server._resolve_external_password(
        parse_args(["--external-password", ""])) is None
