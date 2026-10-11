"""Effort levels are the CLI's to judge, not ours.

``--effort`` and ``/effort`` pass any level through, so a level a newer CLI or
model adds (``xhigh`` on Opus 5.5) works without a change here.  A level the
CLI does not know it ignores, saying so on stderr; the bridge shows that line
in the tab and drops the session back to ``auto``, which is what the CLI runs.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands import classify, _cmd_effort_show     # noqa: E402
from config import parse_args                       # noqa: E402
from sdk_bridge import SDKBridge                    # noqa: E402
from state import init_state_from_config            # noqa: E402

WARNING = ("Warning: Unknown --effort value 'xhihg' — ignoring it and using "
           "the default effort. Valid values: low, medium, high, xhigh, max.")


def _bridge(argv=()):
    cfg = parse_args(list(argv))
    state = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg: dict) -> None:
        sent.append(msg)

    return SDKBridge(config=cfg, state=state, broadcaster=bcast), state, sent


def test_slash_effort_takes_a_level_it_has_no_list_for():
    assert classify("/effort xhigh") == ("effort", "xhigh")
    assert classify("/effort Ultra") == ("effort", "ultra")


def test_slash_effort_takes_one_level():
    kind, _ = classify("/effort high please")
    assert kind == "error"


def test_the_command_line_takes_a_level_it_has_no_list_for():
    assert parse_args(["--effort", "xhigh"]).effort == "xhigh"
    assert parse_args(["--effort", "XHigh"]).effort == "xhigh"


def test_the_level_reaches_the_cli():
    br, _state, _sent = _bridge(["--effort", "xhigh"])
    assert br._make_options().effort == "xhigh"


def test_the_list_marks_a_level_it_does_not_offer():
    _br, state, _sent = _bridge()
    state.effort = "ultra"
    data = _cmd_effort_show("", state, None).messages[0]["data"]
    current = [lv["name"] for lv in data["levels"] if lv["current"]]
    assert current == ["ultra"]
    assert "xhigh" in [lv["name"] for lv in data["levels"]]


def test_a_level_the_cli_refuses_is_shown_and_dropped():
    async def run():
        br, state, sent = _bridge(["--effort", "xhihg"])
        br._on_sdk_stderr(WARNING)
        for _ in range(3):
            await asyncio.sleep(0)
        return state, sent

    state, sent = asyncio.run(run())
    assert state.effort is None
    errors = [m["data"]["message"] for m in sent
              if m.get("type") == "system_msg" and m.get("subtype") == "error"]
    assert len(errors) == 1
    assert "Valid values: low, medium, high, xhigh, max" in errors[0]
    assert "now auto" in errors[0]
    shown = [m["status"]["effort"] for m in sent if m.get("type") == "status_update"]
    assert shown == ["auto"]


def test_a_refusal_of_an_older_level_leaves_the_new_one():
    """The warning came from a CLI started before the user picked another level."""
    async def run():
        br, state, sent = _bridge(["--effort", "xhihg"])
        state.effort = "high"
        br._on_sdk_stderr(WARNING)
        for _ in range(3):
            await asyncio.sleep(0)
        return state, sent

    state, sent = asyncio.run(run())
    assert state.effort == "high"
    errors = [m["data"]["message"] for m in sent if m.get("subtype") == "error"]
    assert len(errors) == 1 and "now auto" not in errors[0]


def test_other_stderr_leaves_the_level_alone():
    async def run():
        br, state, sent = _bridge(["--effort", "xhigh"])
        br._on_sdk_stderr("some unrelated warning")
        for _ in range(3):
            await asyncio.sleep(0)
        return state, sent

    state, sent = asyncio.run(run())
    assert state.effort == "xhigh"
    assert not [m for m in sent if m.get("subtype") == "error"]
