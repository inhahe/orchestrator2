"""The status bar's timers count from when the state began.

Reported 2026-09-26: "once i even saw the bg-wait time quickly catching up to
present."  The clock in ``busy_label`` is the time each snapshot was *sent*,
and a background tab the browser froze is handed every snapshot it missed at
once -- replayed in order, each showed its own old clock.  The status now also
carries ``busy_since`` (epoch seconds) and ``busy_prefix`` (the label without
its clock), and status.js counts from the start.  The same fixes "working":
it counted from the tab's first sight of the state, so a tab opened four
minutes into a turn read 0:0:00.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_args                                   # noqa: E402
from state import init_state_from_config, state_to_status_dict  # noqa: E402


def _status(**over):
    cfg = parse_args([])
    st = init_state_from_config(cfg)
    for k, v in over.items():
        setattr(st, k, v)
    return state_to_status_dict(st, cfg)


def _ago(seconds):
    return time.monotonic() - seconds


def test_a_turn_says_when_it_began():
    s = _status(busy=True, turn_started_at=_ago(240))

    assert s["busy_prefix"] == "working"
    assert abs(s["busy_since"] - (time.time() - 240)) <= 2


def test_bg_wait_says_when_the_oldest_task_began():
    s = _status(background_tasks={
        "a": {"started_at": _ago(600), "description": "x"},
        "b": {"started_at": _ago(30), "description": "y"},
    })

    assert s["busy_class"] == "bg-wait"
    assert s["busy_prefix"] == "bg wait (2)"
    assert abs(s["busy_since"] - (time.time() - 600)) <= 2


def test_connecting_and_compacting_do_too():
    c = _status(connecting=True, connect_started_at=_ago(12))
    k = _status(busy=True, cli_status="compacting", cli_status_started_at=_ago(90))

    assert c["busy_prefix"] == "connecting"
    assert abs(c["busy_since"] - (time.time() - 12)) <= 2
    assert k["busy_prefix"] == "compacting"
    assert abs(k["busy_since"] - (time.time() - 90)) <= 2


def test_an_untimed_state_has_no_start():
    s = _status()

    assert (s["busy_class"], s["busy_since"], s["busy_prefix"]) == ("idle", None, None)


def test_a_timed_state_with_no_start_recorded_has_none():
    s = _status(busy=True, turn_started_at=None)

    assert s["busy_since"] is None


def test_the_start_is_the_same_in_every_snapshot():
    """Rounded to the second: the ticker sends only snapshots that differ, and
    a start recomputed with sub-second jitter would differ every time."""
    started = _ago(100.4)
    first = _status(busy=True, turn_started_at=started)["busy_since"]
    second = _status(busy=True, turn_started_at=started)["busy_since"]

    assert isinstance(first, int)
    assert first == second


def test_the_old_label_is_still_sent():
    """A page older than the hub, or anything else reading the label."""
    s = _status(busy=True, turn_started_at=_ago(5))

    assert s["busy_label"].startswith("working (")
