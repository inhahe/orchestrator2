"""The session list's dot shows the state the session's status bar shows.

Reported 2026-10-27: "the status circle in sessions view shows grey when it's
in bg-wait mode instead of purple".  A running card carried only whether its
session was busy; it carries the status bar's state now, and what the dot
reads besides (background tasks, a loop's wakeup, a resume after a usage
limit), from its own runtime or, for a session another hub runs, from that
hub.  The drawing is static/lobby.js's, in tests/lobby_running_card.test.js.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                         # noqa: E402
from config import parse_args                         # noqa: E402
from session_runtime import SessionRuntime            # noqa: E402
from state import init_state_from_config, state_to_status_dict  # noqa: E402
from tool_manager import register_bg_task             # noqa: E402


def _runtime():
    cfg = parse_args([])
    return SessionRuntime(config=cfg, state=init_state_from_config(cfg), rid="r1")


def test_a_card_carries_the_state_its_status_bar_shows():
    rt = _runtime()
    register_bg_task(rt.state, "t1", "local_bash", "build", tool_use_id="tu-1")

    meta = rt.meta()

    status = state_to_status_dict(rt.state, rt.config)
    assert meta["state"] == status["busy_class"] == "bg-wait"
    assert meta["state_label"].startswith("bg wait (1)")
    assert meta["bg_count"] == 1


def test_and_what_is_to_come():
    rt = _runtime()
    rt.state.wakeup_at = 1893456000.0
    rt.state.limit_resume_at = 1893457000.0

    meta = rt.meta()

    assert meta["state"] == "idle"
    assert meta["wakeup_at"] == 1893456000.0 and meta["limit_resume_at"] == 1893457000.0


def test_another_hubs_card_carries_what_that_hub_said():
    live = {"busy": False, "viewers": 0, "state": "bg-wait", "state_label": "bg wait (1)",
            "bg_count": 1, "wakeup_at": None, "limit_resume_at": None}

    entry = server._foreign_running_entry({"session_id": "s", "cwd": "D:/x"}, None, live)

    for key in ("state", "state_label", "bg_count", "wakeup_at", "limit_resume_at"):
        assert entry[key] == live[key], key


def test_and_nothing_it_could_not_ask():
    entry = server._foreign_running_entry({"session_id": "s", "cwd": "D:/x"}, None, None)

    assert entry["state"] is None and entry["busy"] is None
