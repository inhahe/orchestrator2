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

import collections
import itertools
import json
import time
import uuid
from typing import Any

import ws_channel

# --- A tab that loses its socket resumes; it does not reload ----------------
#
# Reported 2026-09-27: "often, a tab randomly changes its name from the session
# name to 'orchestrator2', and when i click on it, it reloads the history ...
# tabs from other things than orchestrator2 never change their names OR have
# to reload anything due to the tab falling asleep."
#
# Chrome drops the socket of a tab it has put to sleep -- neither end closes it
# (the hub runs with WebSocket pings off, and its per-tab channel never gives
# up on a slow reader).  Every reconnect was then treated as a first visit:
# ``clear_screen`` and the whole history re-rendered from the transcript, a
# thousand-odd messages, to show a tab everything it already had plus the few
# it missed.
#
# So a session numbers what it broadcasts (``seq``) and keeps the recent part.
# A tab reconnecting to the same session says how far it got; if that is still
# covered it gets only the rest, appended, and nothing is cleared.  ``epoch``
# is per runtime, so a hub restart -- a new runtime, a different stream --
# never resumes into the wrong one.  Past what is kept, it falls back to the
# full history exactly as before.

#: Broadcasts that are not replayed: full snapshots (a resumed tab is sent a
#: fresh one of each) and a bell, which would ring late.
NOT_REPLAYED = frozenset({"status_update", "panel_update", "queue_update",
                          "session_list", "bell"})

#: How much of a session's recent output is kept for a tab that reconnects.
#: By size, not only count: one turn can stream thousands of small deltas, or
#: one tool result of a few hundred KB.
REPLAY_MAX_BYTES = 4 * 1024 * 1024
REPLAY_MAX_MESSAGES = 20_000

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
        # The numbered stream a reconnecting tab resumes from (see top).
        self.epoch = uuid.uuid4().hex[:12]
        self.seq = 0
        self._replay: collections.deque[tuple[int, dict[str, Any], int]] = \
            collections.deque()
        self._replay_bytes = 0

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
        # Numbered and kept even with nobody watching: a tab whose socket has
        # dropped is exactly who needs it.
        seq: int | None = None
        if msg.get("type") not in NOT_REPLAYED:
            msg = self._record(msg)
            seq = msg["seq"]
        if not self.clients:
            return
        data: str | None = None
        for ws in list(self.clients):
            if exclude is not None and ws is exclude:
                # It drew this itself, but its place in the stream still moves
                # -- or a resume would hand it its own prompt a second time.
                if seq is not None:
                    ws_channel.send(ws, {"type": "seq", "seq": seq})
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

    def _record(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Number *msg* and keep it for a reconnecting tab."""
        self.seq += 1
        msg = {**msg, "seq": self.seq}
        size = len(json.dumps(msg, default=str))
        self._replay.append((self.seq, msg, size))
        self._replay_bytes += size
        while self._replay and (self._replay_bytes > REPLAY_MAX_BYTES
                                or len(self._replay) > REPLAY_MAX_MESSAGES):
            _seq, _msg, dropped = self._replay.popleft()
            self._replay_bytes -= dropped
        return msg

    def replay_since(self, epoch: Any, seq: Any) -> list[dict[str, Any]] | None:
        """What a tab that last saw *seq* of this stream has missed.

        None when it cannot resume and needs the full history: another
        runtime's stream (a hub restart), a position this stream never reached,
        or one so far back that part of what followed has been dropped.
        """
        if epoch != self.epoch or not isinstance(seq, int) or isinstance(seq, bool):
            return None
        if seq < 0 or seq > self.seq:
            return None
        if seq == self.seq:
            return []
        oldest = self._replay[0][0] if self._replay else self.seq + 1
        if seq + 1 < oldest:
            return None
        return [m for s, m, _size in self._replay if s > seq]

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
