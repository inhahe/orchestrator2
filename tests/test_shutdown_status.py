"""A hub that is shutting down stops saying its sessions are working.

Reported 2026-09-29: "i shut down the server, and two of my os sessions still
said they were working. and i think the one that said the server may have shut
down also said it was still working."

After ``server_shutdown`` the hub stops every session's CLI, which takes
seconds, and its status ticker went on sending each session's state until the
process exited.  A tab applied the last "working" it was sent, and once the
socket closed nothing corrected it.  The page now ignores status after the
notice (tests/reconnect_on_show.test.js).  The hub also stops the ticker there,
and first sends each session's tabs a last full status reading "server
stopped", so a page loaded before the fix drops its busy state too.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from session_runtime import SessionRuntime               # noqa: E402
from state import init_state_from_config                 # noqa: E402


class _WS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


def _rt(rid, *, busy=True, title="OS D"):
    cfg = parse_args([])
    st = init_state_from_config(cfg)
    st.busy = busy
    st.session_id = f"sid-{rid}"
    st.session_title = title
    return SessionRuntime(config=cfg, state=st, rid=rid)


@pytest.fixture(autouse=True)
def hub(monkeypatch):
    monkeypatch.setattr(server, "_hub_going", False)
    monkeypatch.setattr(server, "runtimes", {})
    monkeypatch.setattr(server, "_ws_clients", set())


def _viewer(rt):
    ws = _WS()
    rt.add_client(ws)
    server._ws_clients.add(ws)
    return ws


def test_each_tab_is_told_the_session_stopped_before_the_hub_goes():
    rt = _rt("s1")
    server.runtimes["s1"] = rt
    ws = _viewer(rt)

    asyncio.run(server._broadcast_shutdown("Server shut down from the lobby."))

    kinds = [m["type"] for m in ws.sent]
    assert kinds.index("status_update") < kinds.index("server_shutdown"), kinds
    status = next(m for m in ws.sent if m["type"] == "status_update")["status"]
    assert status["busy_class"] == "shutdown"
    assert status["busy_label"] == "server stopped"
    assert status["busy_since"] is None and status["busy_prefix"] is None


def test_the_last_status_is_a_whole_one():
    """A page from before 2026-09-27 reads missing fields as "no session" and
    renames the tab "orchestrator2"; the rest of the status comes too."""
    rt = _rt("s1", title="Lane D")
    server.runtimes["s1"] = rt
    ws = _viewer(rt)

    asyncio.run(server._broadcast_shutdown("bye"))

    status = next(m for m in ws.sent if m["type"] == "status_update")["status"]
    assert status["session_title"] == "Lane D"
    assert status["session_id"] == "sid-s1"


def test_each_session_gets_its_own():
    a, b = _rt("s1", title="Lane A"), _rt("s2", title="Lane B")
    server.runtimes.update(s1=a, s2=b)
    wa, wb = _viewer(a), _viewer(b)

    asyncio.run(server._broadcast_shutdown("bye"))

    titles = lambda ws: [m["status"]["session_title"] for m in ws.sent
                         if m["type"] == "status_update"]
    assert titles(wa) == ["Lane A"] and titles(wb) == ["Lane B"]


def test_a_session_nobody_is_viewing_is_skipped():
    rt = _rt("s1")
    server.runtimes["s1"] = rt
    sent = []

    async def bc(msg, exclude=None):
        sent.append(msg)

    rt.broadcast = bc

    asyncio.run(server._broadcast_shutdown("bye"))

    assert sent == []


def test_the_ticker_stops_once_the_tabs_are_told(monkeypatch):
    rt = _rt("s1")
    server.runtimes["s1"] = rt
    ticked = []

    async def tick(r):
        ticked.append(r.rid)

    async def no_probe(r):
        pass

    real_sleep = asyncio.sleep

    async def quick(_s):
        await real_sleep(0)

    monkeypatch.setattr(server, "_tick_runtime", tick)
    monkeypatch.setattr(server, "_probe_bg_stalls", no_probe)
    monkeypatch.setattr(server, "_lobby_targets", lambda: [])
    monkeypatch.setattr(server.asyncio, "sleep", quick)

    async def go():
        server._hub_going = True
        await asyncio.wait_for(server._status_ticker(), timeout=2)

    asyncio.run(go())

    assert ticked == [], "it kept ticking after the tabs were told"


def test_telling_the_tabs_is_what_stops_it():
    asyncio.run(server._broadcast_shutdown("bye"))

    assert server._hub_going is True
