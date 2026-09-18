"""``reset_rate_limit`` fully wipes rate-limit state on an account switch.

When a session re-authenticates to a *different* account (a completed
``/login``, propagated to sibling sessions on the same config dir), the previous
account's rate-limit state no longer describes this session.  That includes both
the hard ``rejected`` lockout *and* the per-window utilisation percentages that
drive the status bar's usage display.  ``reset_rate_limit`` must clear all of it,
not just an active rejection — unlike the mid-turn "stale rejection" clear, which
intentionally leaves live utilisation numbers alone.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from state import State, reset_rate_limit, state_to_status_dict


def _cfg() -> Config:
    return Config()


def test_reset_clears_active_rejection():
    s = State()
    s.rate_limit_status = "rejected"
    s.rate_limit_resets_at = int(time.time()) + 300
    s.rate_limit_reset_bell_fired = True
    changed = reset_rate_limit(s)
    assert changed is True
    assert s.rate_limit_status is None
    assert s.rate_limit_resets_at is None
    assert s.rate_limit_reset_bell_fired is False


def test_reset_clears_utilisation_without_rejection():
    # The case the stale-only clear missed: no active rejection, but the old
    # account's usage percentages are still showing in the status bar.
    s = State()
    s.rate_limit_status = "allowed"
    s.rate_limit_utils = {"seven_day": 0.8, "five_hour": 0.3}
    changed = reset_rate_limit(s)
    assert changed is True
    assert s.rate_limit_utils == {}


def test_reset_on_clean_state_is_noop():
    s = State()
    assert reset_rate_limit(s) is False


def test_status_bar_usage_gone_after_reset():
    # End-to-end: a subscription session showing utilisation should have no
    # rate-limit usage field once reset (as after switching accounts).
    s = State()
    s.is_subscription = True
    s.subscription_plan = "max"
    s.rate_limit_utils = {"seven_day": 0.9}
    before = state_to_status_dict(s, _cfg())
    assert before["rate_limits"]  # something was showing
    reset_rate_limit(s)
    after = state_to_status_dict(s, _cfg())
    assert after["rate_limits"] == {}
    assert after["busy_label"] != "rate limited"
