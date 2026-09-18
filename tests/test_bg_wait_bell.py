"""`in_bg_wait` gating for the ``bg-done`` bell.

The ``bg-done`` bell should ring only when a background task finishes *while the
session is parked waiting on it* (the status-bar "bg wait" state) — not when the
model spawned the task mid-turn and kept working.  ``state.in_bg_wait`` is the
predicate the bridge uses to gate that bell; it must match the same conditions
the status bar uses to show the ``bg-wait`` class: not busy, not connecting, not
rate-limited.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import State, in_bg_wait


def test_parked_is_bg_wait():
    s = State()
    assert in_bg_wait(s) is True


def test_busy_is_not_bg_wait():
    s = State()
    s.busy = True
    assert in_bg_wait(s) is False


def test_connecting_is_not_bg_wait():
    s = State()
    s.connecting = True
    assert in_bg_wait(s) is False


def test_active_rate_limit_is_not_bg_wait():
    s = State()
    s.rate_limit_status = "rejected"
    s.rate_limit_resets_at = int(time.time()) + 300
    assert in_bg_wait(s) is False


def test_expired_rate_limit_is_bg_wait():
    # A rate limit whose reset time has already passed no longer counts.
    s = State()
    s.rate_limit_status = "rejected"
    s.rate_limit_resets_at = int(time.time()) - 10
    assert in_bg_wait(s) is True
