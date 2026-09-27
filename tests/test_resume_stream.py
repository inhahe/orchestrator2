"""A tab whose socket drops resumes where it left off; it does not reload.

Reported 2026-09-27: "often, a tab randomly changes its name from the session
name to 'orchestrator2', and when i click on it, it reloads the history ...
tabs from other things than orchestrator2 never change their names OR have to
reload anything due to the tab falling asleep."

Chrome drops a sleeping tab's socket.  Every reconnect was treated as a first
visit: ``clear_screen`` and the whole history re-rendered from the transcript.
Now each session numbers what it broadcasts and keeps the recent part; a tab
reconnecting to the same session says how far it got (``?resume=epoch.seq``)
and is sent only the rest.  (The rename is status.js: see
reconnect_on_show.test.js.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                            # noqa: E402
import session_runtime                                   # noqa: E402
import ws_channel                                        # noqa: E402
from config import parse_args                            # noqa: E402
from session_runtime import SessionRuntime               # noqa: E402
from state import init_state_from_config                 # noqa: E402


def _rt(rid="s-test"):
    cfg = parse_args([])
    return SessionRuntime(config=cfg, state=init_state_from_config(cfg), rid=rid)


def _say(rt, *texts):
    async def go():
        for t in texts:
            await rt.broadcast({"type": "assistant_text", "content": t})
    asyncio.run(go())


def _texts(msgs):
    return [m["content"] for m in msgs if m.get("type") == "assistant_text"]


class _Q:
    """A socket as far as the query string goes."""
    def __init__(self, **q):
        self.query_params = q


class _Tab:
    """A socket that records what it is sent."""
    def __init__(self):
        self.got: list[dict] = []
        self.query_params = {}

    async def send_text(self, text):
        self.got.append(json.loads(text))


async def _flush():
    await asyncio.sleep(0.05)       # let the per-socket writer tasks run


# --------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------

def test_what_a_session_says_is_numbered_in_order():
    rt = _rt()
    _say(rt, "a", "b", "c")

    kept = rt.replay_since(rt.epoch, 0)
    assert rt.seq == 3
    assert [m["seq"] for m in kept] == [1, 2, 3]
    assert _texts(kept) == ["a", "b", "c"]


def test_it_is_kept_with_nobody_watching():
    """A tab whose socket has dropped is exactly who needs it."""
    rt = _rt()
    assert not rt.clients
    _say(rt, "while you were away")

    assert _texts(rt.replay_since(rt.epoch, 0)) == ["while you were away"]


def test_snapshots_and_bells_are_not_numbered():
    """A resumed tab is sent a fresh snapshot, and an old bell would ring late."""
    rt = _rt()

    async def go():
        for kind in ("status_update", "panel_update", "queue_update",
                     "session_list", "bell"):
            await rt.broadcast({"type": kind})

    asyncio.run(go())

    assert rt.seq == 0 and rt.replay_since(rt.epoch, 0) == []


def test_a_tab_is_sent_only_what_it_missed():
    rt = _rt()
    _say(rt, "a", "b", "c", "d")

    assert _texts(rt.replay_since(rt.epoch, 2)) == ["c", "d"]


def test_a_tab_that_missed_nothing_is_sent_nothing():
    rt = _rt()
    _say(rt, "a", "b")

    assert rt.replay_since(rt.epoch, 2) == []


def test_another_runtimes_stream_is_not_resumed():
    """A hub restart: a new runtime, a different stream, the same rid maybe."""
    rt = _rt()
    _say(rt, "a")

    assert rt.replay_since("an-older-epoch", 0) is None


@pytest.mark.parametrize("seq", [-1, 99, "1", True, None, 1.5])
def test_a_position_the_stream_never_had_is_not_resumed(seq):
    rt = _rt()
    _say(rt, "a", "b")

    assert rt.replay_since(rt.epoch, seq) is None


def test_once_what_it_missed_is_gone_it_reloads(monkeypatch):
    monkeypatch.setattr(session_runtime, "REPLAY_MAX_MESSAGES", 3)
    rt = _rt()
    _say(rt, "a", "b", "c", "d", "e")          # keeps c, d, e

    assert rt.replay_since(rt.epoch, 1) is None, "b was dropped; resuming skips it"
    assert _texts(rt.replay_since(rt.epoch, 2)) == ["c", "d", "e"]


def test_what_is_kept_is_bounded_by_size(monkeypatch):
    """One turn can stream thousands of deltas, or one huge tool result."""
    monkeypatch.setattr(session_runtime, "REPLAY_MAX_BYTES", 2000)
    rt = _rt()
    _say(rt, "x" * 900, "y" * 900, "z" * 900)

    assert rt.replay_since(rt.epoch, 0) is None
    assert _texts(rt.replay_since(rt.epoch, 1)) == ["y" * 900, "z" * 900]


def test_the_tab_that_sent_a_prompt_still_learns_its_place():
    """It drew its own prompt, so the broadcast skips it -- but without the
    position, a resume would hand it that prompt a second time."""
    rt = _rt()
    mine, theirs = _Tab(), _Tab()

    async def go():
        for tab in (mine, theirs):
            ws_channel.register(tab)
            rt.add_client(tab)
        await rt.broadcast({"type": "user_message", "content": "hi"}, exclude=mine)
        await _flush()
        for tab in (mine, theirs):
            ws_channel.unregister(tab)

    asyncio.run(go())

    assert mine.got == [{"type": "seq", "seq": 1}]
    assert theirs.got == [{"type": "user_message", "content": "hi", "seq": 1}]


# --------------------------------------------------------------------------
# The hub
# --------------------------------------------------------------------------

def test_a_reconnect_names_where_it_left_off():
    rt = _rt()
    _say(rt, "a", "b", "c")

    assert _texts(server._missed_since(_Q(resume=f"{rt.epoch}.1"), rt)) == ["b", "c"]


@pytest.mark.parametrize("value", [None, "", "no-dot", "e.x", ".3"])
def test_anything_else_is_a_first_visit(value):
    rt = _rt()
    _say(rt, "a")
    q = {} if value is None else {"resume": value}

    assert server._missed_since(_Q(**q), rt) is None


@pytest.fixture
def hub(monkeypatch):
    async def nothing(*a, **k):
        pass

    monkeypatch.setattr(server, "_push_session_list", nothing)
    monkeypatch.setattr(server, "_ws_runtime", {})
    monkeypatch.setattr(server, "lobby_clients", set())


def test_a_resumed_tab_gets_what_it_missed_and_no_history(hub):
    rt = _rt()
    _say(rt, "a", "b", "c")
    tab = _Tab()

    async def go():
        ws_channel.register(tab)
        await server._resume_ws(tab, rt, rt.replay_since(rt.epoch, 1))
        await _flush()
        ws_channel.unregister(tab)

    asyncio.run(go())

    kinds = [m["type"] for m in tab.got]
    assert kinds[0] == "attached" and tab.got[0]["resumed"] is True
    assert tab.got[0]["epoch"] == rt.epoch
    assert "clear_screen" not in kinds and "history" not in kinds, kinds
    assert "status_update" in kinds, "no fresh snapshot: state moved on meanwhile"
    assert _texts(tab.got) == ["b", "c"]
    assert tab in rt.clients and server._ws_runtime[tab] is rt


def test_nothing_said_as_it_resumes_is_lost_or_doubled(hub):
    rt = _rt()
    _say(rt, "a", "b")
    tab = _Tab()

    async def go():
        ws_channel.register(tab)
        await server._resume_ws(tab, rt, rt.replay_since(rt.epoch, 1))
        await rt.broadcast({"type": "assistant_text", "content": "c"})
        await _flush()
        ws_channel.unregister(tab)

    asyncio.run(go())

    assert _texts(tab.got) == ["b", "c"]


def _connect(monkeypatch, rt, **query):
    """Run the real endpoint for one socket; report how it was attached."""
    server.build_app()      # populates WebSocketDisconnect
    calls: list = []

    async def resumed(ws, r, missed):
        calls.append(("resume", _texts(missed)))

    async def attached(ws, r):
        calls.append(("full history", None))

    monkeypatch.setattr(server, "_resume_ws", resumed)
    monkeypatch.setattr(server, "_attach_ws", attached)
    monkeypatch.setattr(server, "runtimes", {rt.rid: rt})

    class FakeWS:
        headers: dict = {}
        query_params = query

        async def accept(self):
            pass

        async def send_text(self, _t):
            pass

        async def send_json(self, _o):
            pass

        async def close(self, *a, **k):
            pass

        async def receive_text(self):
            raise server.WebSocketDisconnect(1000)

    asyncio.run(server.websocket_endpoint(FakeWS()))
    return calls


def test_the_hub_resumes_a_tab_that_says_where_it_was(monkeypatch):
    rt = _rt()
    _say(rt, "a", "b")

    assert _connect(monkeypatch, rt, rid=rt.rid, resume=f"{rt.epoch}.1") \
        == [("resume", ["b"])]


def test_a_first_visit_gets_the_full_history(monkeypatch):
    rt = _rt()

    assert _connect(monkeypatch, rt, rid=rt.rid) == [("full history", None)]


def test_a_resume_it_cannot_serve_gets_the_full_history(monkeypatch):
    rt = _rt()
    _say(rt, "a")

    assert _connect(monkeypatch, rt, rid=rt.rid, resume="gone-epoch.1") \
        == [("full history", None)]


def test_a_full_attach_says_where_in_the_stream_the_tab_joins(hub):
    """So it has something to resume from if its socket later drops."""
    rt = _rt()
    _say(rt, "a", "b")
    tab = _Tab()
    server._ws_runtime[tab] = rt

    async def go():
        ws_channel.register(tab)
        await server._send_initial_state(tab)
        await _flush()
        ws_channel.unregister(tab)

    asyncio.run(go())

    [attached] = [m for m in tab.got if m["type"] == "attached"]
    assert attached["epoch"] == rt.epoch and attached["seq"] == 2


# --------------------------------------------------------------------------
# Why it reconnected
# --------------------------------------------------------------------------

def test_the_log_says_how_the_last_socket_was_lost(caplog):
    with caplog.at_level(logging.INFO, logger=server.log.name):
        server._log_reconnect_reason(_Q(why="1006", hidden="312", frozen="1"), "s7")

    said = caplog.text
    assert "s7" in said and "1006" in said and "312" in said
    assert "frozen meanwhile: yes" in said


def test_and_when_the_browser_discarded_the_tab(caplog):
    with caplog.at_level(logging.INFO, logger=server.log.name):
        server._log_reconnect_reason(_Q(discarded="1"), "s7")

    assert "discarded" in caplog.text


def test_the_endpoint_logs_it_for_every_connecting_tab(monkeypatch, caplog):
    rt = _rt()
    with caplog.at_level(logging.INFO, logger=server.log.name):
        _connect(monkeypatch, rt, rid=rt.rid, why="1006", hidden="40", frozen="0")

    assert "socket closed with 1006" in caplog.text


def test_a_first_visit_logs_nothing(caplog):
    with caplog.at_level(logging.INFO, logger=server.log.name):
        server._log_reconnect_reason(_Q(rid="s7"), "s7")

    assert "reconnecting" not in caplog.text and "discarded" not in caplog.text
