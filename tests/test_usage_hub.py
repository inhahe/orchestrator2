"""/usage typed in a tab: the hub's handler.

Asked 2026-09-27: "the claude code TUI has a /usage command that tells you in
ascii all about your usage, including what percentage of the 5-hr limit and
7-day limit's been used and when they reset. can you implement /usage in
orchestrator2?"

The fetch itself is tests/test_usage.py; this file is the server's half, kept
apart because importing server.py is slow and those tests do not need it.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plan_usage                                # noqa: E402

USAGE = {"five_hour": {"utilization": 18.0,
                       "resets_at": "2026-09-27T21:10:00.132120+00:00"}}

import server                                    # noqa: E402
from hubkit import FakeWS                         # noqa: E402


def _usage_msgs(ws):
    return [m["data"] for m in ws.sent
            if m.get("type") == "command_data" and m.get("label") == "usage"]


def _type_usage(ws):
    return server._handle_ws_message(ws, {"type": "message", "text": "/usage"})


def test_typing_usage_opens_loading_then_answers(hub, monkeypatch):
    asked = []

    def report(config_dir):
        asked.append(config_dir)
        return {"usage": USAGE, "subscription_type": "max", "account": {}}

    monkeypatch.setattr(plan_usage, "usage_report", report)
    ws = FakeWS()
    asyncio.run(_type_usage(ws))
    msgs = _usage_msgs(ws)
    assert msgs[0] == {"loading": True}, msgs
    assert msgs[1]["usage"] == USAGE
    assert len(msgs) == 2
    assert asked == [None]           # the default runtime: the hub's account


def test_it_asks_as_the_runtimes_account(hub, monkeypatch):
    import dataclasses
    hub.config = dataclasses.replace(
        hub.config, config_dir=r"D:\accounts\.claude-work")
    asked = []
    monkeypatch.setattr(plan_usage, "usage_report",
                        lambda d: asked.append(d) or {"error": "x"})
    asyncio.run(_type_usage(FakeWS()))
    assert asked == [r"D:\accounts\.claude-work"]


def test_the_fetch_does_not_block_the_hub(hub, monkeypatch):
    # It runs in a thread: the event loop stays free while it waits.
    import threading
    loop_thread = []
    fetch_thread = []

    def report(_d):
        fetch_thread.append(threading.get_ident())
        return {"error": "x"}

    monkeypatch.setattr(plan_usage, "usage_report", report)

    async def go():
        loop_thread.append(threading.get_ident())
        await _type_usage(FakeWS())

    asyncio.run(go())
    assert fetch_thread and fetch_thread != loop_thread


def test_only_the_asking_tab_is_answered(hub, monkeypatch):
    monkeypatch.setattr(plan_usage, "usage_report", lambda d: {"error": "x"})
    other = FakeWS()
    asyncio.run(server._attach_ws(other, hub))
    before = len(other.sent)
    asyncio.run(_type_usage(FakeWS()))
    assert not _usage_msgs(other) and len(other.sent) == before


def test_a_bug_in_the_report_still_answers(hub, monkeypatch, caplog):
    def boom(_d):
        raise KeyError("oops")

    monkeypatch.setattr(plan_usage, "usage_report", boom)
    ws = FakeWS()
    with caplog.at_level("ERROR", logger="orchestrator2"):
        asyncio.run(_type_usage(ws))
    msgs = _usage_msgs(ws)
    # Not left on "Loading usage data..." for ever.
    assert msgs[-1] == {"error": "Failed to load usage data (see the hub log)."}
    assert "/usage" in caplog.text


def test_it_is_not_sent_to_the_session(hub, monkeypatch):
    monkeypatch.setattr(plan_usage, "usage_report", lambda d: {"error": "x"})
    asyncio.run(_type_usage(FakeWS()))
    assert hub.bridge.event_queue.empty()
    assert not hub.state.queued_prompts
