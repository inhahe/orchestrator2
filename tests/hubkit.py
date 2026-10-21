"""A hub with one session in it, for tests that drive ``server`` by message.

``conftest.hub`` is the fixture.  Tests import ``FakeWS`` from here to be a
tab.  ``server`` is imported only when a hub is made: it is slow to import,
and most of the suite never needs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json


class FakeWS:
    """A tab: what the hub sent it, decoded, in order."""

    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class FakeBridge:
    """Just enough bridge for the hub's message routing."""

    def __init__(self):
        self.event_queue = asyncio.Queue()

    async def flush_pending_turn_end(self):
        pass

    async def stop(self, **_kw):
        pass


@contextlib.contextmanager
def one_session_hub():
    """Make a runtime with a fresh Config and State the hub's only and default
    one, and yield it; the hub's globals are put back afterwards."""
    import server
    from config import parse_args
    from session_runtime import SessionRuntime
    from state import State

    cfg = parse_args([])
    st = State()
    rt = SessionRuntime(config=cfg, state=st, rid="R1")
    rt.bridge = FakeBridge()
    saved = (dict(server.runtimes), server._default_runtime, server.config,
             server.state, server.bridge, dict(server._ws_runtime))
    server.runtimes.clear()
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt
    server.config = cfg
    server.state = st
    server.bridge = rt.bridge
    try:
        yield rt
    finally:
        server.runtimes.clear()
        server.runtimes.update(saved[0])
        server._default_runtime = saved[1]
        server.config = saved[2]
        server.state = saved[3]
        server.bridge = saved[4]
        server._ws_runtime.clear()
        server._ws_runtime.update(saved[5])
