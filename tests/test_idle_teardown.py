"""A session is torn down for being *idle*, not for having no socket open.

Two ways that proxy was wrong, both reported/found 2026-09-14.

**A sleeping phone is not a viewer leaving.**  Reported: *"i think if the only
web connection to a session is via mobile, it shouldn't have a session timeout
for the web connection because phones often go to sleep"*.  Phones suspend
their browser within a minute or two of the screen locking, well inside the
5-minute default, so the session its only viewer is still using is reaped.  The
tab reconnects on wake -- `app.js` does that on `visibilitychange` -- and finds
nothing to reconnect to.

**A session that is working is not idle.**  Found while reading that path: the
timer checked `rt.clients` and nothing else, so a turn started and then left to
run (close the tab, lock the phone) was killed mid-edit five minutes later,
along with any background tasks it had spawned.  That is the opposite of what a
server-side agent is for -- you close the tab *because* it keeps working.

The two compound: the reporter's case is a phone, and a phone is exactly the
client most likely to be locked while a long turn runs.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server                                       # noqa: E402
from config import parse_args                       # noqa: E402
from session_runtime import SessionRuntime          # noqa: E402
from state import State                             # noqa: E402

PHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
         "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148")
ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class _WS:
    """Just enough of a Starlette WebSocket for the idle path."""

    def __init__(self, ua: str = DESKTOP) -> None:
        self.headers = {"user-agent": ua}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(server, "runtimes", {})
    monkeypatch.setattr(server, "_mobile_ws", set())
    monkeypatch.setattr(server, "_default_runtime", None)
    monkeypatch.setattr(server, "config", parse_args([]))
    yield


def _runtime(**state_kw):
    st = State()
    for k, v in state_kw.items():
        setattr(st, k, v)
    rt = SessionRuntime(config=server.config, state=st)
    server.runtimes[rt.rid] = rt
    return rt


def _connect(ws):
    """What `websocket_endpoint` does at accept time."""
    if server._ws_looks_mobile(ws):
        server._mobile_ws.add(ws)
    return ws


def _in_loop(fn):
    """Run *fn* with a live event loop.

    ``_maybe_start_idle_timer`` schedules an ``asyncio`` task, so every call
    that arms needs one.  Each test does its whole arm/inspect/cancel sequence
    inside a single loop, so a timer created in one is never cancelled from
    another.
    """
    async def go():
        return fn()
    return asyncio.run(go())


def _arm(rt, ws):
    """Arm, report the seconds it would wait, and cancel.  ``None`` = no clock."""
    def go():
        server._maybe_start_idle_timer(rt, departing=ws)
        left = (None if rt.idle_deadline is None
                else rt.idle_deadline - time.time())
        server._cancel_idle_timer(rt)
        return left
    return _in_loop(go)


# ---------------------------------------------------------------------------
# Recognising a device that sleeps
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ua", [PHONE, ANDROID])
def test_a_phone_is_recognised(ua):
    assert server._ws_looks_mobile(_WS(ua)) is True


def test_a_desktop_is_not(_=None):
    assert server._ws_looks_mobile(_WS(DESKTOP)) is False


def test_a_missing_user_agent_is_treated_as_desktop():
    """The conservative answer: the session is still reaped on the ordinary
    clock rather than living forever because a header was absent."""
    ws = _WS()
    ws.headers = {}
    assert server._ws_looks_mobile(ws) is False


def test_an_unreadable_header_does_not_raise():
    class Hostile:
        @property
        def headers(self):
            raise RuntimeError("no headers here")

    assert server._ws_looks_mobile(Hostile()) is False


# ---------------------------------------------------------------------------
# The clock a departing viewer starts
# ---------------------------------------------------------------------------

def test_a_departing_desktop_starts_the_ordinary_clock():
    rt = _runtime()
    left = _arm(rt, _connect(_WS(DESKTOP)))
    assert left is not None and 290 < left <= 300


def test_a_departing_phone_starts_no_clock_at_all():
    """The reported case. 0 is the default for --mobile-idle-timeout."""
    rt = _runtime()
    assert _arm(rt, _connect(_WS(PHONE))) is None


def test_a_mobile_timeout_can_be_asked_for(monkeypatch):
    """"Never" is the default, not the only option -- someone running many
    phone-viewed sessions may want them reaped eventually."""
    monkeypatch.setattr(server, "config",
                        parse_args(["--mobile-idle-timeout", "3600"]))
    rt = _runtime()
    left = _arm(rt, _connect(_WS(PHONE)))
    assert left is not None and 3590 < left <= 3600


def test_a_desktop_leaving_after_a_phone_still_gets_the_short_clock():
    """It is the *last* viewer that decides. A desktop closing its tab is a
    deliberate departure however many phones looked earlier."""
    rt = _runtime()
    phone, desk = _connect(_WS(PHONE)), _connect(_WS(DESKTOP))
    rt.add_client(phone)
    rt.add_client(desk)
    rt.discard_client(phone)
    _in_loop(lambda: server._maybe_start_idle_timer(rt, departing=phone))
    assert rt.idle_deadline is None, "armed while a viewer was still attached"
    rt.discard_client(desk)
    left = _arm(rt, desk)
    assert left is not None and 290 < left <= 300


def test_a_phone_leaving_last_exempts_the_session():
    """The other order: the desktop goes first, the phone is what is left, and
    the phone is the one that sleeps."""
    rt = _runtime()
    phone, desk = _connect(_WS(PHONE)), _connect(_WS(DESKTOP))
    rt.add_client(phone)
    rt.add_client(desk)
    rt.discard_client(desk)
    _in_loop(lambda: server._maybe_start_idle_timer(rt, departing=desk))
    assert rt.idle_deadline is None, "armed while the phone was still attached"
    rt.discard_client(phone)
    assert _arm(rt, phone) is None


def test_a_socket_that_never_connected_is_not_mobile():
    """`_mobile_ws` membership, not a fresh sniff: the header is read once, at
    accept, because when the socket dies the page may already be suspended."""
    rt = _runtime()
    never_connected = _WS(PHONE)        # looks mobile, but was never recorded
    assert _arm(rt, never_connected) is not None


# ---------------------------------------------------------------------------
# The production registration, not the test's copy of it
# ---------------------------------------------------------------------------
#
# Everything above goes through `_connect`, a helper that *replicates* what
# `websocket_endpoint` does at accept.  A sweep showed that deleting the real
# line changed nothing any test could see -- the same shape as testing a
# feature's helpers while its one call site quietly disappears.

def _drive_endpoint(ua):
    """Run the endpoint for one socket that disconnects immediately.

    Returns whether the socket was registered as mobile *while it was live* --
    the cleanup path discards it, so it cannot be observed afterwards.
    """
    server.build_app()      # populates WebSocketDisconnect / Request globals
    seen: list[bool] = []

    class FakeWS:
        def __init__(self):
            self.headers = {"user-agent": ua}
            self.query_params = {}

        async def accept(self):
            pass

        async def send_text(self, _t):
            pass

        async def send_json(self, _o):
            pass

        async def close(self, *a, **k):
            pass

        async def receive_text(self):
            seen.append(self in server._mobile_ws)
            raise server.WebSocketDisconnect(1006)

    asyncio.run(server.websocket_endpoint(FakeWS()))
    return seen


def test_the_endpoint_registers_a_phone_at_accept():
    """Read from the upgrade request, while it is in hand.  Asking the page
    later is not an option: when the socket dies the page may already be
    suspended, which is the whole case this exists for."""
    assert _drive_endpoint(PHONE) == [True]


def test_the_endpoint_does_not_register_a_desktop():
    assert _drive_endpoint(DESKTOP) == [False]


def test_a_disconnected_socket_is_forgotten():
    """`_mobile_ws` is keyed by socket object; never cleaning it would grow
    without bound for the life of the hub."""
    _drive_endpoint(PHONE)
    assert server._mobile_ws == set()


# ---------------------------------------------------------------------------
# A working session is not idle
# ---------------------------------------------------------------------------

def _fire(rt, timeout=0):
    """Run the timer body to completion; return True if it tore the runtime
    down."""
    torn: list[str] = []

    async def fake_teardown(r, **kw):
        torn.append(r.rid)

    orig = server._teardown_runtime
    server._teardown_runtime = fake_teardown
    try:
        asyncio.run(server._idle_teardown_after(rt, timeout))
    finally:
        server._teardown_runtime = orig
    return bool(torn)


def test_a_quiet_session_is_torn_down():
    assert _fire(_runtime()) is True


def test_a_session_mid_turn_is_not_torn_down():
    """Close the tab on a running turn and it keeps working -- that is what a
    server-side agent is for."""
    assert _fire(_runtime(busy=True)) is False


def test_a_session_waiting_on_a_wakeup_is_not_torn_down():
    """Reported 2026-09-20: two autonomous-loop sessions, OSc and OSa, showed
    "This tab's session is no longer running" days after the user had closed
    nothing and the hub had not restarted.

    Both were reaped *between loop iterations*. From the log::

        22:25:10  wakeup armed: delay=1800s  'Autonomous-loop wakeup ...'
        22:26:45  runtime s5 idle for 300s — tearing down

    Ninety-five seconds. A session waiting on a wakeup is doing exactly what it
    was asked to -- nothing, until the wakeup fires -- and ``state.wakeup_at``
    lives only in memory, so reaping it cancels the loop outright and for good.
    """
    assert _fire(_runtime(wakeup_at=time.time() + 1800)) is False


def test_a_wakeup_that_should_already_have_fired_does_not_pin_the_runtime():
    """Only a *future* wakeup counts. A stale past timestamp means the wakeup
    should already have fired, and deferring on it would hold the runtime open
    forever on a schedule nothing will honour."""
    assert _fire(_runtime(wakeup_at=time.time() - 60)) is True


def test_no_wakeup_is_not_mistaken_for_one():
    """``wakeup_at`` is None on most sessions; a truthiness test would be fine
    but a comparison against None would explode."""
    assert _fire(_runtime(wakeup_at=None)) is True


def test_a_session_with_background_tasks_is_not_torn_down():
    """A turn can finish while the work it spawned is still running; killing
    the runtime takes those with it."""
    rt = _runtime()
    rt.state.background_tasks["t1"] = {"name": "long build"}
    assert _fire(rt) is False


def test_a_deferred_teardown_re_arms():
    """Deferring is not cancelling. Without a re-arm the session would never be
    reaped once it had been busy a single time."""
    rt = _runtime(busy=True)
    _fire(rt)
    assert rt.idle_deadline is not None
    _in_loop(lambda: server._cancel_idle_timer(rt))


def test_a_session_that_goes_quiet_is_reaped_on_the_next_pass():
    rt = _runtime(busy=True)
    assert _fire(rt) is False
    _in_loop(lambda: server._cancel_idle_timer(rt))
    rt.state.busy = False
    assert _fire(rt) is True


def test_a_deferral_keeps_the_mobile_grace(monkeypatch):
    """The re-arm must not silently drop a mobile session onto the short
    desktop clock -- that would reap the phone-viewed session after all, just
    five minutes later than before."""
    monkeypatch.setattr(server, "config",
                        parse_args(["--mobile-idle-timeout", "3600"]))
    rt = _runtime(busy=True)
    phone = _connect(_WS(PHONE))
    _in_loop(lambda: server._maybe_start_idle_timer(rt, departing=phone))
    assert rt.idle_mobile is True
    _fire(rt)
    left = rt.idle_deadline - time.time()
    assert left > 600, f"re-armed with the desktop clock ({left:.0f}s)"
    _in_loop(lambda: server._cancel_idle_timer(rt))


def test_a_viewer_returning_cancels_the_countdown():
    rt = _runtime()

    def go():
        server._maybe_start_idle_timer(rt, departing=_connect(_WS(DESKTOP)))
        assert rt.idle_deadline is not None
        server._cancel_idle_timer(rt)

    _in_loop(go)
    assert rt.idle_deadline is None and rt.idle_mobile is False


def test_a_runtime_already_gone_is_not_torn_down_twice():
    rt = _runtime()
    server.runtimes.clear()
    assert _fire(rt) is False

# ---------------------------------------------------------------------------
# What a stale tab is told
# ---------------------------------------------------------------------------

def test_an_unknown_rid_still_gets_the_old_guess():
    """A rid from a previous hub, or a reboot that restored old tabs, is a case
    we genuinely cannot explain -- so the guess stays for it, and only for it."""
    server._teardown_reasons.clear()

    notice = server._describe_missing_rid("s99")

    assert "restarted" in notice


def test_a_reaped_session_is_told_what_actually_happened(monkeypatch):
    """The reported banner blamed "the server was restarted (or the session was
    closed)". Both halves were wrong: the hub had been up for days and the user
    had closed nothing. It had been reaped for being idle."""
    server._teardown_reasons.clear()
    rt = _runtime(session_title="E OSc")

    server._record_teardown(rt, "was closed after 5 minutes with no tab connected")
    notice = server._describe_missing_rid(rt.rid)

    assert "E OSc" in notice, "the banner does not say which session"
    assert "5 minutes with no tab connected" in notice
    assert "restarted" not in notice, "it still blames a restart that never happened"


def test_the_banner_says_the_conversation_survived():
    """The session file is untouched by a teardown; without saying so the
    banner reads like the work was lost."""
    server._teardown_reasons.clear()
    rt = _runtime(session_title="E OSa")
    server._record_teardown(rt, "was closed")

    notice = server._describe_missing_rid(rt.rid).lower()

    assert "safe" in notice or "reopen" in notice


def test_the_record_is_bounded():
    """A courtesy for reconnecting tabs, not a log."""
    server._teardown_reasons.clear()
    for i in range(server._TEARDOWN_MEMORY + 10):
        rt = _runtime()
        rt.rid = f"s{i}"
        server._record_teardown(rt, "was closed")

    assert len(server._teardown_reasons) == server._TEARDOWN_MEMORY


def test_the_oldest_record_is_the_one_dropped():
    server._teardown_reasons.clear()
    for i in range(server._TEARDOWN_MEMORY + 1):
        rt = _runtime()
        rt.rid = f"s{i}"
        server._record_teardown(rt, "was closed")

    assert "s0" not in server._teardown_reasons
    assert f"s{server._TEARDOWN_MEMORY}" in server._teardown_reasons


def test_an_idle_reap_says_it_was_the_idle_rule():
    """"Was closed" would be true and useless. The user's whole complaint was
    not knowing *why* a session they never closed had gone, so the banner has
    to name the rule and its timeout."""
    captured: dict = {}

    async def fake_teardown(r, **kw):
        captured.update(kw)

    orig = server._teardown_runtime
    server._teardown_runtime = fake_teardown
    try:
        asyncio.run(server._idle_teardown_after(_runtime(), 0))
    finally:
        server._teardown_runtime = orig

    reason = captured.get("reason", "")
    assert "no tab connected" in reason, reason
    assert "minute" in reason, reason


def test_every_teardown_records_a_reason():
    """The recorder is useless if a path forgets to call it, and the banner
    silently falls back to the guess -- which is what it did before."""
    import inspect
    src = inspect.getsource(server._teardown_runtime)

    assert "_record_teardown(rt, reason)" in src, (
        "teardown no longer records why, so stale tabs get the old guess again"
    )

