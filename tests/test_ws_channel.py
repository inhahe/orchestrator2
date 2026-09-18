"""Tests for ws_channel — the per-client outbound WebSocket channel.

Covers the properties that fix the "immediate command hangs during a turn"
bug: snapshot messages coalesce while queued, ordered content is preserved,
and enqueue never blocks on a slow client.

These run each coroutine via ``asyncio.run`` (matching test_multisession.py) so
no pytest-asyncio plugin is needed.
"""

from __future__ import annotations

import asyncio
import json

import ws_channel


class _FakeWS:
    """A WebSocket whose ``send_text`` is gated by an event, so we can hold the
    writer task mid-send and inspect what's still queued."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._gate = asyncio.Event()
        self._gate.set()  # open by default (sends complete immediately)

    def block(self) -> None:
        self._gate.clear()

    def unblock(self) -> None:
        self._gate.set()

    async def send_text(self, data: str) -> None:
        await self._gate.wait()
        self.sent.append(data)


def _types(sent: list[str]) -> list[str]:
    return [json.loads(s)["type"] for s in sent]


def test_snapshot_messages_coalesce_while_queued():
    async def go():
        ws = _FakeWS()
        ch = ws_channel.ClientChannel(ws)
        try:
            ws.block()  # writer will stall on the first send
            ch.send({"type": "status_update", "n": 1})
            await asyncio.sleep(0)  # hand control to the writer task
            # Pile on more snapshots + one content message while it's stuck.
            ch.send({"type": "status_update", "n": 2})
            ch.send({"type": "panel_update", "n": 1})
            ch.send({"type": "assistant_text", "content": "hi"})
            ch.send({"type": "status_update", "n": 3})
            ch.send({"type": "panel_update", "n": 2})

            queued_types = [m.get("type") for m in ch._queue]
            assert queued_types.count("status_update") == 1
            assert queued_types.count("panel_update") == 1
            assert queued_types.count("assistant_text") == 1
            latest_status = next(
                m for m in ch._queue if m["type"] == "status_update")
            assert latest_status["n"] == 3  # newest snapshot content wins

            ws.unblock()
            await ch.drain(1.0)
            await asyncio.sleep(0.01)
            assert "assistant_text" in _types(ws.sent)
        finally:
            ch.close()

    asyncio.run(go())


def test_enqueue_is_non_blocking_when_client_stalls():
    async def go():
        ws = _FakeWS()
        ch = ws_channel.ClientChannel(ws)
        try:
            ws.block()
            ch.send({"type": "assistant_text", "content": "first"})
            await asyncio.sleep(0)  # writer grabs it and blocks inside send
            for i in range(100):
                ch.send({"type": "assistant_text", "content": str(i)})
            assert len(ch._queue) == 100  # all buffered, nothing blocked
            ws.unblock()
            await ch.drain(1.0)
            assert len(ws.sent) == 101
        finally:
            ch.close()

    asyncio.run(go())


def test_send_after_close_is_ignored():
    async def go():
        ws = _FakeWS()
        ch = ws_channel.ClientChannel(ws)
        ch.close()
        ch.send({"type": "assistant_text", "content": "late"})
        assert len(ch._queue) == 0

    asyncio.run(go())


def test_registry_send_falls_back_when_unregistered():
    async def go():
        ws = _FakeWS()
        # Not registered → send() returns False so callers can direct-send.
        assert ws_channel.send(ws, {"type": "x"}) is False
        ch = ws_channel.register(ws)
        try:
            assert ws_channel.send(ws, {"type": "assistant_text"}) is True
            await ch.drain(1.0)
            await asyncio.sleep(0.01)
            assert _types(ws.sent) == ["assistant_text"]
        finally:
            ws_channel.unregister(ws)

    asyncio.run(go())


def test_ordered_content_preserved():
    async def go():
        ws = _FakeWS()
        ch = ws_channel.ClientChannel(ws)
        try:
            ws.block()
            ch.send({"type": "clear_screen"})
            await asyncio.sleep(0)
            ch.send({"type": "assistant_text", "content": "a"})
            ch.send({"type": "tool_use", "name": "Bash"})
            ch.send({"type": "history", "messages": []})
            ws.unblock()
            await ch.drain(1.0)
            await asyncio.sleep(0.01)
            assert _types(ws.sent) == [
                "clear_screen", "assistant_text", "tool_use", "history"]
        finally:
            ch.close()

    asyncio.run(go())


if __name__ == "__main__":
    test_snapshot_messages_coalesce_while_queued()
    test_enqueue_is_non_blocking_when_client_stalls()
    test_send_after_close_is_ignored()
    test_registry_send_falls_back_when_unregistered()
    test_ordered_content_preserved()
    print("ok")
