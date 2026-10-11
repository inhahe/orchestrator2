"""Sessions billed to an API key: the hub's half.

``/usage`` for a key session, and seeing ``/login console`` finish.  The
rest, and the background, is tests/test_api_key_billing.py; this file is
apart because importing server.py is slow.  A key for one session alone is
tests/test_session_api_key.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth                                      # noqa: E402
import plan_usage                                # noqa: E402
import server                                    # noqa: E402
from session_runtime import SessionRuntime       # noqa: E402


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class _FakeBridge:
    def __init__(self):
        self.event_queue = asyncio.Queue()

    async def flush_pending_turn_end(self):
        pass

    async def stop(self):
        pass


@pytest.fixture
def hub():
    from config import parse_args
    from state import State
    cfg = parse_args([])
    st = State()
    rt = SessionRuntime(config=cfg, state=st, rid="R1")
    rt.bridge = _FakeBridge()
    saved = (dict(server.runtimes), server._default_runtime, server.config,
             server.state, server.bridge, dict(server._ws_runtime))
    server.runtimes.clear()
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt
    server.config = cfg
    server.state = st
    server.bridge = rt.bridge
    yield rt
    server.runtimes.clear()
    server.runtimes.update(saved[0])
    server._default_runtime = saved[1]
    server.config = saved[2]
    server.state = saved[3]
    server.bridge = saved[4]
    server._ws_runtime.clear()
    server._ws_runtime.update(saved[5])


def _usage(ws):
    asyncio.run(server._handle_ws_message(ws, {"type": "message", "text": "/usage"}))
    return [m["data"] for m in ws.sent
            if m.get("type") == "command_data" and m.get("label") == "usage"]


def test_usage_for_a_key_session_explains_instead_of_fetching(hub, monkeypatch):
    # Its account may also have a subscription, whose limits are not what
    # this session spends: they are not asked for at all.
    def no_fetch(_d):
        raise AssertionError("fetched plan limits for a key session")

    monkeypatch.setattr(plan_usage, "usage_report", no_fetch)
    hub.state.api_key_source = "ANTHROPIC_API_KEY"
    msgs = _usage(_FakeWS())
    assert msgs[0] == {"loading": True}
    assert "API key (ANTHROPIC_API_KEY)" in msgs[1]["note"]


def test_usage_for_a_subscription_session_still_fetches(hub, monkeypatch):
    monkeypatch.setattr(plan_usage, "usage_report",
                        lambda d: {"usage": {}, "account": {}})
    hub.state.api_key_source = "none"
    msgs = _usage(_FakeWS())
    assert "usage" in msgs[1] and "note" not in msgs[1]


def _watch(hub, monkeypatch, readings, method):
    # The last reading repeats: a sign-in never seen to finish times out
    # (quickly, here) rather than running out of readings.
    seen = iter(readings)
    last = [readings[-1]]

    def reading(_d=None):
        last[0] = next(seen, last[0])
        return last[0]

    monkeypatch.setattr(auth, "login_fingerprint", reading)
    monkeypatch.setattr(server, "LOGIN_POLL_S", 0.01)
    monkeypatch.setattr(server, "LOGIN_WATCH_S", 1.0)
    monkeypatch.setattr(server, "detect_account_info",
                        lambda _d=None: {"email": "me@example.com"})
    said = []

    async def capture(m):
        said.append(m)

    hub.broadcast = capture
    asyncio.run(server._watch_login(hub, method))
    return " ".join((m.get("data") or {}).get("message", "") for m in said
                    if m.get("type") == "system_msg")


def test_a_console_sign_in_is_seen_to_finish(hub, monkeypatch):
    # No subscription token file is written: the new key is what appears.
    text = _watch(hub, monkeypatch, [(10.0, None), (None, "k1")], "console")
    assert "Signed in to Claude as me@example.com." in text
    assert "billed per token" in text


def test_a_subscription_sign_in_says_nothing_about_a_key(hub, monkeypatch):
    text = _watch(hub, monkeypatch, [(10.0, None), (20.0, None)], "claudeai")
    assert "Signed in to Claude as me@example.com." in text
    assert "billed per token" not in text


def test_typing_login_console_watches_for_a_console_sign_in(hub, monkeypatch):
    monkeypatch.setattr(auth, "is_logged_in", lambda _d=None: True)
    monkeypatch.setattr(auth, "launch_login",
                        lambda **kw: (True, "a window has opened."))
    watched = []

    async def nothing():
        pass

    def watch(rt, method="claudeai"):
        watched.append(method)
        return nothing()

    monkeypatch.setattr(server, "_watch_login", watch)
    asyncio.run(server._handle_ws_message(
        _FakeWS(), {"type": "message", "text": "/login console"}))
    assert watched == ["console"]


