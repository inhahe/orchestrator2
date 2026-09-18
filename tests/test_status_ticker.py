"""Tests for the status ticker's unchanged-snapshot suppression.

The ticker fires every 2s per runtime.  It used to broadcast a ``status_update``
unconditionally, so a session sitting completely idle still woke every viewing
tab 30 times a minute — WebSocket wake-up, JSON parse, panel re-render, repaint
of a document with tens of thousands of elements.  Measured with Playwright
against the real UI that came to ~2% of a CPU core *per open tab*, forever,
with the tab doing nothing; four tabs made orchestrator2 the heaviest thing in
the browser.

``_tick_runtime`` now sends only when the serialised snapshot actually differs,
with a periodic heartbeat so the stream never goes fully silent.  These tests
pin that behaviour: no change → no send, any change → send, and the heartbeat
still fires.  They use hand-built runtimes and stubbed serialisers, so they run
offline with no SDK.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from session_runtime import SessionRuntime  # noqa: E402


class FakeWS:
    """Minimal Starlette-WebSocket stand-in that records what it was sent."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))


def _make_runtime() -> tuple[SessionRuntime, FakeWS]:
    state = SimpleNamespace(
        completed_panel_tools={}, completed_panel_bg={}, pending_bell=None,
    )
    rt = SessionRuntime(config=SimpleNamespace(cwd="C:/proj"), state=state)
    ws = FakeWS()
    rt.add_client(ws)
    return rt, ws


@pytest.fixture(autouse=True)
def _stub_serialisers(monkeypatch):
    """Drive the snapshot from a single mutable dict the tests can poke.

    The real serialisers need a full State/Config; what's under test is the
    *comparison*, not their contents.
    """
    box = {"busy_label": "idle"}
    monkeypatch.setattr(server, "state_to_status_dict",
                        lambda st, cfg: dict(box))
    monkeypatch.setattr(server, "state_to_panels_dict", lambda st: {})
    monkeypatch.setattr(server, "_enrich_panels", lambda p: p)
    return box


def _sent(ws: FakeWS) -> list[str]:
    return [m.get("type") for m in ws.sent]


# ---------------------------------------------------------------------------


def test_first_tick_always_broadcasts():
    rt, ws = _make_runtime()
    assert asyncio.run(server._tick_runtime(rt)) is True
    assert _sent(ws) == ["status_update"]


def test_an_idle_session_stops_sending_after_the_first_tick(_stub_serialisers):
    rt, ws = _make_runtime()

    async def go():
        for _ in range(10):
            await server._tick_runtime(rt)

    asyncio.run(go())
    # Ten ticks, nothing changed: exactly one snapshot on the wire.
    assert _sent(ws) == ["status_update"]


def test_a_changed_snapshot_is_broadcast(_stub_serialisers):
    rt, ws = _make_runtime()

    async def go():
        await server._tick_runtime(rt)
        await server._tick_runtime(rt)          # suppressed
        _stub_serialisers["busy_label"] = "working (0:00:04)"
        await server._tick_runtime(rt)          # must go out
        await server._tick_runtime(rt)          # suppressed again

    asyncio.run(go())
    assert _sent(ws) == ["status_update", "status_update"]
    assert ws.sent[-1]["status"]["busy_label"] == "working (0:00:04)"


def test_a_change_in_the_panels_blob_also_broadcasts(monkeypatch):
    rt, ws = _make_runtime()
    todos: list[dict] = []
    monkeypatch.setattr(server, "state_to_status_dict", lambda st, cfg: {})
    monkeypatch.setattr(server, "state_to_panels_dict",
                        lambda st: {"todos": list(todos)})
    monkeypatch.setattr(server, "_enrich_panels", lambda p: p)

    async def go():
        await server._tick_runtime(rt)
        await server._tick_runtime(rt)
        todos.append({"content": "do the thing", "status": "in_progress"})
        await server._tick_runtime(rt)

    asyncio.run(go())
    assert _sent(ws) == ["status_update", "status_update"]
    assert ws.sent[-1]["panels"]["todos"][0]["content"] == "do the thing"


def test_the_heartbeat_resends_an_unchanged_snapshot(monkeypatch):
    """A silent stream must not be indistinguishable from a wedged ticker."""
    rt, ws = _make_runtime()
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])

    async def go():
        await server._tick_runtime(rt)          # t=1000, first send
        clock[0] += 2.0
        await server._tick_runtime(rt)          # t=1002, suppressed
        clock[0] += server._STATUS_HEARTBEAT_SECONDS
        await server._tick_runtime(rt)          # heartbeat due — send

    asyncio.run(go())
    assert _sent(ws) == ["status_update", "status_update"]


def test_a_runtime_with_no_viewers_is_skipped():
    rt, ws = _make_runtime()
    rt.discard_client(ws)
    assert asyncio.run(server._tick_runtime(rt)) is False
    assert ws.sent == []


def test_a_pending_bell_still_gets_flushed_while_the_snapshot_is_suppressed():
    """The bell must not be collateral damage of the status suppression."""
    rt, ws = _make_runtime()

    async def go():
        await server._tick_runtime(rt)          # first snapshot
        rt.state.pending_bell = "done"
        await server._tick_runtime(rt)          # snapshot suppressed, bell not

    asyncio.run(go())
    assert _sent(ws) == ["status_update", "bell"]
    assert ws.sent[-1]["event"] == "done"
    assert rt.state.pending_bell is None
