"""Why did the bell ring? — attribution and flag propagation.

From a report: "it seems i often hear it when a turn wasn't just completed and
no task just finished and there was no rate-hit, even though i start it with
--bell turn-done bg-done rate-hit."

Two things made that unanswerable rather than merely wrong:

1. **Nothing logged a bell.**  Not the ring, not the suppression, not the
   broadcast.  With several sessions live in one hub, a bell from another
   session's tab sounds identical to one from the tab you're looking at, and
   afterwards there was no record of which event fired or where.

2. **``--bell`` is dropped when a launch reuses a running hub.**  The hub
   protocol forwards ``cwd``/``resume``/``model``/``effort``/``config_dir`` and
   nothing else, so a session launched into an existing hub inherited the *hub
   process's* bell set.  Start the hub without ``--bell`` and every later
   ``--bell turn-done bg-done rate-hit`` launch is silently ignored — which is
   exactly "even though i start it with...".
"""

from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod  # noqa: E402
from config import DEFAULT_BELL_EVENTS, parse_args, parse_bell_events  # noqa: E402
from state import init_state_from_config, ring_bell  # noqa: E402


# --------------------------------------------------------------------------
# The flag itself parses the way the user writes it
# --------------------------------------------------------------------------

def test_the_reported_flag_form_means_what_it_says():
    """`--bell turn-done bg-done rate-hit`, space separated, as reported."""
    cfg = parse_args(["--bell", "turn-done", "bg-done", "rate-hit"])
    events = parse_bell_events(cfg.bell_on)
    assert events == {"turn-done", "bg-done", "rate-hit"}
    assert "requires-action" in DEFAULT_BELL_EVENTS, \
        "test premise: requires-action is on by default, so this spec drops it"
    assert "requires-action" not in events
    assert "interrupt" not in events


def test_a_disabled_event_never_becomes_pending():
    cfg = parse_args(["--bell", "turn-done", "bg-done", "rate-hit"])
    state = init_state_from_config(cfg)

    ring_bell(state, "requires-action")
    assert state.pending_bell is None
    ring_bell(state, "interrupt")
    assert state.pending_bell is None
    ring_bell(state, "turn-done")
    assert state.pending_bell == "turn-done"


# --------------------------------------------------------------------------
# Every bell leaves a trace
# --------------------------------------------------------------------------

def _rings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("bell:")]


def test_a_rung_bell_is_logged_with_its_session(caplog):
    cfg = parse_args(["--bell", "turn-done"])
    state = init_state_from_config(cfg)
    state.session_id = "8d532b0a-a05f-469f"
    state.session_title = "forward-raytracer"

    with caplog.at_level(logging.INFO, logger="state"):
        ring_bell(state, "turn-done")

    msgs = _rings(caplog)
    assert msgs, "a bell rang and left no trace at all"
    joined = " ".join(msgs)
    assert "turn-done" in joined
    assert "8d532b0a" in joined, \
        "no way to tell which session's tab made the sound"


def test_a_suppressed_bell_is_logged_too(caplog):
    """Otherwise "I heard one" and "you shouldn't have" can't be told apart."""
    cfg = parse_args(["--bell", "turn-done"])
    state = init_state_from_config(cfg)

    with caplog.at_level(logging.INFO, logger="state"):
        ring_bell(state, "requires-action")

    joined = " ".join(_rings(caplog))
    assert "requires-action" in joined and "suppressed" in joined


# --------------------------------------------------------------------------
# --bell survives a launch into an already-running hub
# --------------------------------------------------------------------------

def test_the_hub_launch_payload_carries_the_bell_spec():
    """The payload is the whole contract — a field missing here is a flag
    silently dropped, with no error anywhere to notice it by."""
    import server

    captured: dict = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True, "rid": "rid-1"}).encode()

    class _FakeReq:
        def __init__(self, url, data=None, method=None, headers=None):
            captured.update(json.loads(data.decode()))

    import urllib.request
    orig_req, orig_open = urllib.request.Request, urllib.request.urlopen
    urllib.request.Request = _FakeReq
    urllib.request.urlopen = lambda req, timeout=None: _FakeResp()
    try:
        rid = server._launch_into_hub(
            8420, cwd="D:/x", resume=None, no_continue=False,
            model=None, effort=None, config_dir=None,
            bell_on="turn-done bg-done rate-hit",
        )
    finally:
        urllib.request.Request, urllib.request.urlopen = orig_req, orig_open

    assert rid == "rid-1"
    assert captured.get("bell_on") == "turn-done bg-done rate-hit", \
        "--bell is dropped when the launch joins a running hub"


def test_a_launch_spec_overrides_the_hub_process_bell_set():
    """The hub was started with the defaults; the launch asks for fewer."""
    hub_cfg = parse_args([])            # hub: defaults, incl. requires-action
    assert "requires-action" in parse_bell_events(hub_cfg.bell_on)

    import dataclasses
    launched = dataclasses.replace(hub_cfg, bell_on="turn-done bg-done rate-hit")
    state = init_state_from_config(launched)

    assert state.bell_events == {"turn-done", "bg-done", "rate-hit"}, \
        "session inherited the hub's bell set instead of the launch's"
