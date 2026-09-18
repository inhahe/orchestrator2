"""Per-client outbound WebSocket channel.

Every connected browser tab gets its own :class:`ClientChannel`: a small
outbound queue drained by a dedicated writer task.  ``send()`` is a
*non-blocking* enqueue, so the code paths that fan messages out to clients
(``broadcast``, ``send_to``, ``rt.broadcast``) never ``await ws.send_text``
directly on the event loop.

Why this matters
----------------
Previously each fan-out awaited ``ws.send_text`` per client inline.  A slow
client (a tablet on a weak link, or a busy browser tab draining its socket
slowly) would apply TCP backpressure: the awaiting send suspended, the socket's
write buffer filled with a backlog of ``status_update`` / ``panel_update``
snapshots, and — critically — the *response to an immediate command* (the
``/help`` modal, the ``/cls`` clear) queued behind that backlog.  Worse, the
WebSocket receive loop itself blocked whenever an immediate-command handler
awaited its own ``send_to`` behind the same congested buffer, so the command
appeared to "hang" for a long time before finally running.

The channel fixes both problems:

* **Decoupling** — ``send()`` enqueues and returns immediately; the writer task
  owns the only ``await ws.send_text``.  Fan-out and the receive loop are never
  blocked by a slow client.
* **Coalescing** — full-snapshot message types (status/panel/queue updates) are
  collapsed to their latest value while they sit unsent, so a client that has
  fallen behind doesn't accumulate a long queue of stale snapshots.  Real,
  ordered content (chat text, tool output, ``clear_screen``, ``history``, the
  ``/help`` modal, bells, …) is never dropped or reordered — it flows out as
  fast as the client can take it, right behind at most one pending snapshot.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from typing import Any

log = logging.getLogger("orchestrator.ws")

# Message types that are *full snapshots* of some piece of session state.  When
# more than one is queued for a client, only the newest matters, so we coalesce
# in place (keeping queue position, replacing content).  Everything else is
# ordered content and is delivered verbatim.
_COALESCE_TYPES: frozenset[str] = frozenset({
    "status_update",
    "panel_update",
    "queue_update",
    "session_list",
})


class ClientChannel:
    """Outbound queue + writer task for a single WebSocket."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._queue: collections.deque[dict[str, Any]] = collections.deque()
        self._wake = asyncio.Event()
        self._closed = False
        self.task: asyncio.Task[None] = asyncio.create_task(self._run())

    def send(self, msg: dict[str, Any]) -> None:
        """Enqueue *msg* for delivery (non-blocking).

        Coalescing: if *msg* is a snapshot type and one of the same type is
        already waiting, replace it in place rather than appending a duplicate.
        """
        if self._closed:
            return
        mtype = msg.get("type")
        if mtype in _COALESCE_TYPES:
            for i, pending in enumerate(self._queue):
                if pending.get("type") == mtype:
                    self._queue[i] = msg
                    self._wake.set()
                    return
        self._queue.append(msg)
        self._wake.set()

    async def _run(self) -> None:
        try:
            while True:
                if not self._queue:
                    if self._closed:
                        return
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                msg = self._queue.popleft()
                try:
                    await self.ws.send_text(json.dumps(msg, default=str))
                except Exception:
                    # Client is gone / socket broken.  Stop; the receive loop's
                    # teardown will unregister us.
                    return
        finally:
            self._closed = True

    async def drain(self, timeout: float) -> None:
        """Wait until the queue empties or *timeout* elapses (best effort)."""
        deadline = asyncio.get_event_loop().time() + timeout
        while self._queue and not self._closed:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.02, remaining))

    def close(self) -> None:
        self._closed = True
        self._wake.set()


# ---------------------------------------------------------------------------
# Registry — one channel per live socket
# ---------------------------------------------------------------------------

_channels: dict[Any, ClientChannel] = {}


def register(ws: Any) -> ClientChannel:
    """Create (or return the existing) channel for *ws*."""
    ch = _channels.get(ws)
    if ch is not None and not ch._closed:
        return ch
    ch = ClientChannel(ws)
    _channels[ws] = ch
    return ch


def unregister(ws: Any) -> None:
    """Drop and close the channel for *ws* (on disconnect)."""
    ch = _channels.pop(ws, None)
    if ch is not None:
        ch.close()


def send(ws: Any, msg: dict[str, Any]) -> bool:
    """Enqueue *msg* on *ws*'s channel.  Returns False if there's no channel."""
    ch = _channels.get(ws)
    if ch is None:
        return False
    ch.send(msg)
    return True


async def drain_all(timeout: float) -> None:
    """Best-effort flush of every channel (used on server shutdown)."""
    channels = list(_channels.values())
    if not channels:
        return
    await asyncio.gather(
        *(ch.drain(timeout) for ch in channels),
        return_exceptions=True,
    )
