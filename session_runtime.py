"""session_runtime.py — one live Claude session hosted by the server.

The server is a **multi-session hub**: it can host several live sessions at
once, each with its own SDK connection, state and set of viewing browser tabs.
A :class:`SessionRuntime` bundles everything that belongs to one such session so
the rest of the server can address "the session this tab is looking at" instead
of a single process-wide global.

Phase 1 note: the server currently creates exactly one *default* runtime that
wraps the existing single-session globals, so behaviour is unchanged.  Later
phases add a lobby, on-demand runtime creation, and per-runtime routing.
"""

from __future__ import annotations

import itertools
import json
import time
from typing import Any

import ws_channel

# Monotonic-ish counter for stable internal ids.  A brand-new session has no
# Claude ``session_id`` until its first result arrives, so we key runtimes by
# our own ``rid`` from the moment they're created.
_rid_counter = itertools.count(1)


def _next_rid() -> str:
    return f"s{next(_rid_counter)}"


class SessionRuntime:
    """One live session: its config, state, SDK bridge and viewing clients.

    * ``config`` / ``state`` — this session's own objects (a fresh config clone
      carries the session's ``cwd`` + ``resume``; resume is cwd-scoped).
    * ``bridge`` — the :class:`SDKBridge` (set once built off the hot path).
    * ``clients`` — the set of WebSockets currently *attached to* (viewing) this
      session.  ``broadcast`` only reaches these, so tabs on other sessions
      aren't disturbed.
    """

    def __init__(self, *, config: Any, state: Any, rid: str | None = None) -> None:
        self.rid = rid or _next_rid()
        self.config = config
        self.state = state
        self.bridge: Any = None
        self.clients: set[Any] = set()
        self.ticker_task: Any = None
        self.idle_timer: Any = None
        # Epoch seconds when the viewer-less idle-teardown fires, or None when
        # no teardown is pending (has viewers / is the default runtime).  Shown
        # in the lobby as a "closing in …" countdown.
        self.idle_deadline: float | None = None
        # True when the pending countdown is the *mobile* one, so a deferral
        # (the session was still working when it fired) re-arms with the same
        # grace instead of silently dropping to the much shorter desktop one.
        self.idle_mobile: bool = False
        self.created_at = time.time()
        self.last_activity = self.created_at
        # Last status+panels snapshot the ticker actually sent, and when.  An
        # idle session's snapshot is byte-identical every tick, and re-sending
        # it costs each viewing tab a repaint of a very large document, so the
        # ticker compares against this and stays quiet.  See _status_ticker.
        self.last_status_sig: str | None = None
        self.last_status_sent: float = 0.0

    # -- clients ----------------------------------------------------------

    def add_client(self, ws: Any) -> None:
        self.clients.add(ws)
        self.touch()

    def discard_client(self, ws: Any) -> None:
        self.clients.discard(ws)

    def touch(self) -> None:
        self.last_activity = time.time()

    # -- messaging --------------------------------------------------------

    async def broadcast(self, msg: dict[str, Any], *, exclude: Any = None) -> None:
        """Enqueue *msg* for every WebSocket attached to this session.

        Non-blocking: each socket has its own outbound writer task (see
        ``ws_channel``), so a slow viewer never stalls the turn loop or the
        other viewers.

        *exclude* skips one socket — for the case where that client has already
        rendered the message itself (the optimistic echo of a prompt it just
        sent).  It is deliberately "skip one viewer", not "skip the
        broadcast": a session can be open in several tabs, and suppressing the
        whole send left every *other* tab without the prompt while still
        showing them the reply to it.
        """
        if not self.clients:
            return
        data: str | None = None
        for ws in list(self.clients):
            if exclude is not None and ws is exclude:
                continue
            if ws_channel.send(ws, msg):
                continue
            # No channel (socket not registered yet) — direct fallback.
            if data is None:
                data = json.dumps(msg, default=str)
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)

    # -- lobby / listing --------------------------------------------------

    def meta(self) -> dict[str, Any]:
        """A small dict describing this session for the lobby list."""
        st = self.state
        cfg = self.config
        return {
            "rid": self.rid,
            "session_id": getattr(st, "session_id", None),
            "title": getattr(st, "session_title", None),
            "cwd": getattr(cfg, "cwd", None),
            "busy": bool(getattr(st, "busy", False)),
            "viewers": len(self.clients),
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "idle_deadline": self.idle_deadline,
            "account": getattr(cfg, "config_dir", None) or None,
        }
