"""Bell-event spec parsing tests.

``parse_bell_events`` turns the ``--bell`` / ``--bell-on`` startup spec into the
set of enabled bell events.  It must mirror the ``/bell`` slash command:

* bare names REPLACE the set (ring on only those),
* ``+event`` / ``-event`` MODIFY the defaults (add / remove),
* ``all`` / ``none`` are shortcuts.

Regression: a previous version treated ``+turn-done`` as the literal event name
``"+turn-done"``, so ``--bell +turn-done`` (used by the real launch scripts)
produced a set that never matched the ``"turn-done"`` event name passed to
``ring_bell`` — the bell never fired.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_bell_events, DEFAULT_BELL_EVENTS, BELL_EVENT_NAMES


DEFAULTS = {e for e in DEFAULT_BELL_EVENTS.split(",") if e in BELL_EVENT_NAMES}


def test_plus_prefix_adds_to_defaults():
    # The exact form the launch scripts use.
    events = parse_bell_events("+turn-done")
    assert "turn-done" in events
    # '+' means "defaults plus", so a defaulted event stays enabled too.
    assert events == DEFAULTS | {"turn-done"}


def test_plus_prefix_new_event():
    events = parse_bell_events("+interrupt")
    assert events == DEFAULTS | {"interrupt"}


def test_minus_prefix_removes_from_defaults():
    events = parse_bell_events("-bg-done")
    assert "bg-done" not in events
    assert events == DEFAULTS - {"bg-done"}


def test_bare_names_replace():
    assert parse_bell_events("turn-done") == {"turn-done"}
    assert parse_bell_events("turn-done bg-done") == {"turn-done", "bg-done"}


def test_shortcuts():
    assert parse_bell_events("all") == set(BELL_EVENT_NAMES)
    assert parse_bell_events("none") == set()
    assert parse_bell_events("off") == set()


def test_empty_is_defaults():
    assert parse_bell_events("") == DEFAULTS


def test_default_spec_roundtrips():
    assert parse_bell_events(DEFAULT_BELL_EVENTS) == DEFAULTS


def test_unknown_names_ignored():
    assert parse_bell_events("bogus") == set()
    assert parse_bell_events("+bogus") == DEFAULTS
    assert parse_bell_events("turn-done bogus") == {"turn-done"}


def test_mixed_prefix_and_bare():
    # A bare name mixed with prefixed tokens is treated as an add.
    events = parse_bell_events("+interrupt bg-done")
    assert events == DEFAULTS | {"interrupt", "bg-done"}
