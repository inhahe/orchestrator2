"""Signing a remote MCP server in: ``/mcp auth`` and ``/mcp callback``.

WordPress.com's server (``https://public-api.wordpress.com/wpcom/v2/mcp/v1``)
was the case: it answers 401 until an OAuth sign-in, so it sits on "needs auth".
The CLI runs the flow when sent ``mcp_authenticate`` and finishes it when the
browser comes back to its ``localhost`` listener. From a browser on another
device, ``mcp_oauth_callback_url`` finishes it instead. These tests pin the
requests the bridge sends, the link and instructions the asking tab gets, and
the watcher that says when the sign-in has finished. The CLI's side was tried
against the bundled 2.1.321 and the real server (see design.md).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hubkit                                    # noqa: E402
import server                                    # noqa: E402
from hubkit import FakeWS                         # noqa: E402
from sdk_bridge import SDKBridge                  # noqa: E402

AUTH_URL = ("https://public-api.wordpress.com/oauth2-1/authorize?client_id=x"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A39279%2Fcallback&state=s")
CALLBACK = "http://localhost:39279/callback?code=abc&state=s"


class McpBridge(hubkit.FakeBridge):
    """A bridge whose CLI answers MCP requests from a script: each status
    reading is the next of *statuses*, the last one repeating."""

    def __init__(self):
        super().__init__()
        self.client = object()
        self.auth_answer = {"authUrl": AUTH_URL, "requiresUserAction": True,
                            "callbackPort": 39279}
        self.auth_error = None
        self.reconnect_error = None
        self.during_auth = None
        # Read once by /mcp auth before it starts, then by the watcher. None:
        # a reading that leaves the server out.
        self.statuses = ["needs-auth"]
        self.calls = []

    async def authenticate_mcp_server(self, name):
        self.calls.append(("auth", name))
        if self.during_auth is not None:
            await self.during_auth()
        if self.auth_error:
            raise Exception(self.auth_error)
        return self.auth_answer

    async def submit_mcp_oauth_callback(self, name, url):
        self.calls.append(("callback", name, url))

    async def reconnect_mcp_server(self, name):
        self.calls.append(("reconnect", name))
        if self.reconnect_error:
            raise Exception(self.reconnect_error)
        self.statuses = ["connected"]

    async def get_mcp_status(self):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            return []
        tools = [{"name": "wpcom-mcp-posts"}, {"name": "wpcom-mcp-media"}]
        return [{"name": "wpcom-mcp", "status": status,
                 "tools": tools if status == "connected" else []}]


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setattr(server, "MCP_SIGNIN_POLL_S", 0.01)
    with hubkit.one_session_hub(lambda _c, _s: McpBridge()) as rt:
        said = []

        async def capture(m, **_kw):
            said.append(m)

        rt.broadcast = capture
        rt.said = said
        yield rt
    server._mcp_signin_watchers.clear()


def _texts(msgs):
    return [(m.get("data") or {}).get("message", "") for m in msgs
            if m.get("type") == "system_msg"]


async def _send(ws, text):
    """Send *text* as the tab would, and wait for the sign-in step it
    started, which runs beside the tab's message loop."""
    await server._handle_ws_message(ws, {"type": "message", "text": text})
    await asyncio.gather(*list(server._mcp_signin_steps))


def _watcher(hub, name="wpcom-mcp"):
    return server._mcp_signin_watchers[(hub.rid, name)]


def test_auth_gives_the_asking_tab_the_link_and_the_way_back(hub):
    ws = FakeWS()

    async def go():
        await _send(ws, "/mcp auth wpcom-mcp")
        _watcher(hub).cancel()

    asyncio.run(go())
    assert hub.bridge.calls == [("auth", "wpcom-mcp")]
    msg = [m for m in ws.sent if m.get("type") == "system_msg"][-1]
    assert msg["data"]["link"] == {"url": AUTH_URL, "label": "Sign in wpcom-mcp"}
    text = msg["data"]["message"]
    assert "localhost:39279" in text
    assert "/mcp callback wpcom-mcp <address>" in text


def test_the_watcher_says_when_the_sign_in_lands(hub):
    # needs-auth until the browser comes back, then the CLI's own reconnect.
    hub.bridge.statuses = ["needs-auth", "needs-auth", "pending", "connected"]

    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        await asyncio.wait_for(_watcher(hub), 2)

    asyncio.run(go())
    assert _texts(hub.said) == ["MCP server 'wpcom-mcp' is signed in and connected "
                                "(2 tools). The next turn can use them."]
    assert server._mcp_signin_watchers == {}


def test_a_sign_in_never_finished_is_said_to_have_timed_out(hub, monkeypatch):
    monkeypatch.setattr(server, "MCP_SIGNIN_WATCH_S", 0.05)

    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        await asyncio.wait_for(_watcher(hub), 2)

    asyncio.run(go())
    [text] = _texts(hub.said)
    assert "not seen to finish signing in within 5 minutes (its status: needs-auth)" in text
    assert "/mcp auth wpcom-mcp" in text


def test_the_watcher_goes_quietly_when_the_session_reconnects(hub):
    # The flow lived in the CLI that went; the new one reports anew.
    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        hub.bridge.client = object()
        await asyncio.wait_for(_watcher(hub), 2)

    asyncio.run(go())
    assert hub.said == []


def test_a_cli_refusal_is_shown_in_its_own_words(hub):
    hub.bridge.auth_error = 'Server type "stdio" does not support OAuth authentication'
    ws = FakeWS()
    asyncio.run(_send(ws, "/mcp auth playwright"))
    [text] = _texts(ws.sent)
    assert text == ("MCP sign-in failed for 'playwright': Server type \"stdio\" "
                    "does not support OAuth authentication")
    assert server._mcp_signin_watchers == {}


def test_callback_finishes_the_one_sign_in_in_progress(hub):
    # The server may be left out: it is the session's only sign-in.
    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        watcher = _watcher(hub)
        await _send(FakeWS(), f"/mcp callback {CALLBACK}")
        await asyncio.sleep(0)
        return watcher

    watcher = asyncio.run(go())
    # The CLI leaves the reconnect to whoever pasted the address.
    assert hub.bridge.calls[-2:] == [("callback", "wpcom-mcp", CALLBACK),
                                     ("reconnect", "wpcom-mcp")]
    assert watcher.cancelled()
    # Said once, by the callback, not again by the watcher.
    assert _texts(hub.said) == ["MCP server 'wpcom-mcp' is signed in and connected "
                                "(2 tools). The next turn can use them."]


def test_a_reconnect_that_fails_after_the_sign_in_is_not_called_a_failed_sign_in(hub):
    hub.bridge.reconnect_error = "HTTP 503"

    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        await _send(FakeWS(), f"/mcp callback wpcom-mcp {CALLBACK}")

    asyncio.run(go())
    assert _texts(hub.said) == ["MCP server 'wpcom-mcp' failed to connect after "
                                "signing in: HTTP 503. Try /mcp reconnect wpcom-mcp."]


def test_the_steps_run_beside_the_tabs_message_loop(hub):
    # The tab's next message (an interrupt, a permission answer) is not held
    # up by a remote server that is slow to answer.
    release = None

    async def slow():
        await release.wait()

    async def go():
        nonlocal release
        release = asyncio.Event()
        hub.bridge.during_auth = slow
        # Inline, the handler would wait on the slow server: a timeout, not a hang.
        await asyncio.wait_for(server._handle_ws_message(
            FakeWS(), {"type": "message", "text": "/mcp auth wpcom-mcp"}), 1)
        for _ in range(5):          # let the step run up to the slow server
            await asyncio.sleep(0)
        assert hub.bridge.calls == [("auth", "wpcom-mcp")]   # started, not finished
        assert server._mcp_signin_watchers == {}
        release.set()
        await asyncio.gather(*list(server._mcp_signin_steps))
        _watcher(hub).cancel()

    asyncio.run(go())


def test_a_connected_server_is_not_signed_in_again(hub):
    # Its status would never show the sign-in finishing.
    hub.bridge.statuses = ["connected"]
    ws = FakeWS()
    asyncio.run(_send(ws, "/mcp auth wpcom-mcp"))
    assert _texts(ws.sent) == ["MCP server 'wpcom-mcp' is already signed in and connected."]
    assert hub.bridge.calls == []


def test_a_disabled_server_is_enabled_first(hub):
    # The CLI does not reconnect a disabled server after a sign-in.
    hub.bridge.statuses = ["disabled"]
    ws = FakeWS()
    asyncio.run(_send(ws, "/mcp auth wpcom-mcp"))
    assert _texts(ws.sent) == ["MCP server 'wpcom-mcp' is disabled: run /mcp enable "
                               "wpcom-mcp first."]
    assert hub.bridge.calls == []


def test_a_server_failing_already_is_not_reported_failed_by_the_sign_in(hub, monkeypatch):
    # Before the browser step, a failing server is still failing: no news.
    monkeypatch.setattr(server, "MCP_SIGNIN_WATCH_S", 0.05)
    hub.bridge.statuses = ["failed"]

    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        await asyncio.wait_for(_watcher(hub), 2)

    asyncio.run(go())
    [text] = _texts(hub.said)
    assert "not seen to finish signing in within 5 minutes (its status: failed)" in text


def test_a_server_failing_already_is_reported_once_its_status_moves(hub):
    hub.bridge.statuses = ["failed", "failed", "pending", "failed"]

    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        await asyncio.wait_for(_watcher(hub), 2)

    asyncio.run(go())
    assert _texts(hub.said) == ["MCP server 'wpcom-mcp' failed to connect after "
                                "signing in. Try /mcp reconnect wpcom-mcp."]


def test_a_reading_without_the_server_is_tried_again(hub):
    hub.bridge.statuses = ["needs-auth", None, "connected"]

    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        await asyncio.wait_for(_watcher(hub), 2)

    asyncio.run(go())
    assert "is signed in and connected" in _texts(hub.said)[0]


def test_a_reconnect_while_the_sign_in_starts_is_said(hub):
    # The flow went with the CLI it was started in.
    async def reconnect():
        hub.bridge.client = object()

    hub.bridge.during_auth = reconnect
    ws = FakeWS()
    asyncio.run(_send(ws, "/mcp auth wpcom-mcp"))
    assert _texts(ws.sent) == ["The session reconnected while the sign-in for "
                               "'wpcom-mcp' was starting. Run /mcp auth wpcom-mcp again."]
    assert server._mcp_signin_watchers == {}


def test_an_address_comes_with_or_without_the_flag(hub):
    # The address is what says a browser step is needed.
    hub.bridge.auth_answer = {"authUrl": AUTH_URL}
    ws = FakeWS()

    async def go():
        await _send(ws, "/mcp auth wpcom-mcp")
        _watcher(hub).cancel()

    asyncio.run(go())
    assert ws.sent[-1]["data"]["link"]["url"] == AUTH_URL


def test_an_address_the_page_cannot_link_is_shown_in_the_text(hub):
    hub.bridge.auth_answer = {"authUrl": "myapp://authorize?x=1", "requiresUserAction": True}
    ws = FakeWS()

    async def go():
        await _send(ws, "/mcp auth wpcom-mcp")
        _watcher(hub).cancel()

    asyncio.run(go())
    data = ws.sent[-1]["data"]
    assert "link" not in data
    assert data["message"].endswith("\n\nmyapp://authorize?x=1")


def test_the_list_says_how_to_finish_a_sign_in_in_progress(hub):
    # The message with the link may be gone from a tab reloaded meanwhile.
    ws = FakeWS()

    async def go():
        await _send(FakeWS(), "/mcp auth wpcom-mcp")
        await _send(ws, "/mcp")
        _watcher(hub).cancel()

    asyncio.run(go())
    [modal] = [m for m in ws.sent if m.get("type") == "modal"]
    assert "/mcp callback wpcom-mcp <address>" in modal["content"]


def test_callback_without_a_sign_in_says_how_to_start_one(hub):
    ws = FakeWS()
    asyncio.run(_send(ws, f"/mcp callback {CALLBACK}"))
    [text] = _texts(ws.sent)
    assert "run /mcp auth <server> first" in text
    assert hub.bridge.calls == []


def test_callback_wants_an_address(hub):
    ws = FakeWS()
    asyncio.run(_send(ws, "/mcp callback wpcom-mcp code=abc"))
    [text] = _texts(ws.sent)
    assert text.startswith("usage: /mcp callback")
    assert hub.bridge.calls == []


# -- the bridge's half: the control requests themselves --------------------

class _Query:
    def __init__(self, answer=None):
        self.sent = []
        self.answer = answer or {}

    async def _send_control_request(self, request):
        self.sent.append(request)
        return self.answer


class _Client:
    def __init__(self, query):
        self._query = query


def _bridge(query):
    bridge = SDKBridge.__new__(SDKBridge)   # nothing but its client is touched
    bridge.client = _Client(query)
    return bridge


def test_the_bridge_asks_the_cli_to_authenticate():
    q = _Query({"authUrl": AUTH_URL, "requiresUserAction": True})
    answer = asyncio.run(_bridge(q).authenticate_mcp_server("wpcom-mcp"))
    assert q.sent == [{"subtype": "mcp_authenticate", "serverName": "wpcom-mcp"}]
    assert answer["authUrl"] == AUTH_URL


def test_the_bridge_submits_a_pasted_address():
    q = _Query()
    asyncio.run(_bridge(q).submit_mcp_oauth_callback("wpcom-mcp", CALLBACK))
    assert q.sent == [{"subtype": "mcp_oauth_callback_url", "serverName": "wpcom-mcp",
                       "callbackUrl": CALLBACK}]


def test_an_sdk_without_the_private_channel_is_said_plainly():
    bridge = _bridge(object())
    with pytest.raises(RuntimeError, match="cannot send MCP sign-in requests"):
        asyncio.run(bridge.authenticate_mcp_server("wpcom-mcp"))


def test_a_client_still_connecting_is_not_taken_for_an_old_sdk():
    # The bridge's client exists before its connect() has made the query.
    bridge = _bridge(None)
    with pytest.raises(RuntimeError, match="SDK not connected yet"):
        asyncio.run(bridge.authenticate_mcp_server("wpcom-mcp"))
