"""The CLI does the compacting, at the session's threshold.

The threshold goes to the CLI as ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (and
``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` below 67k), and the CLI counts the
context from each API response itself, before every call; orchestrator2
never sends ``/compact`` by itself.  The CLI reads its environment once,
when it starts: a session's kept threshold must be known before its CLI
starts, and a change restarts the CLI -- only when nothing is running, not
a ghost turn either.  The count orchestrator2 shows is the input of the
latest main-thread API call, never a ``ResultMessage``'s, which is the sum
of every call of the turn.  The rest of a session's compaction -- per
session, kept by its id, handed over by a launch -- is
tests/test_compact_at_per_session.py.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                                       # noqa: E402
import server                                           # noqa: E402
from claude_agent_sdk import (                          # noqa: E402
    AssistantMessage,
    ResultMessage,
    SystemMessage,
)
from config import compaction_env, parse_args           # noqa: E402
from sdk_bridge import SDKBridge                        # noqa: E402
from session import save_session_compaction             # noqa: E402
from state import State, init_state_from_config         # noqa: E402

WINDOW = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
PCT = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"


def cli_threshold(env: dict[str, str], model_window: int = 1_000_000) -> int:
    """What Claude Code 2.1.280 compacts at under *env*, for a model whose
    window is *model_window* and whose output limit is at least 20k: the
    window from ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (refused outside
    100k-1M, capped at the model's), less 20k kept for the summary, less a
    13k buffer -- lowered by ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` to that
    percentage of the window less 20k."""
    window = model_window
    if WINDOW in env:
        asked = int(env[WINDOW])
        assert 100_000 <= asked <= 1_000_000, "the CLI would refuse it"
        window = min(model_window, asked)
    effective = window - 20_000
    threshold = effective - 13_000
    if PCT in env:
        pct = float(env[PCT])
        assert 0 < pct <= 100, "the CLI would ignore it"
        threshold = min(math.floor(effective * (pct / 100)), threshold)
    return threshold


# -- The threshold the CLI is given ------------------------------------------

@pytest.mark.parametrize("at", [
    1, 999, 12_345, 50_000, 66_999, 67_000, 67_001,
    160_000, 300_000, 950_000, 967_000,
])
def test_the_cli_compacts_exactly_where_asked(at):
    assert cli_threshold(compaction_env(at, True)) == at


def test_no_percentage_rounds_a_low_threshold_off_by_one():
    for at in range(1, 67_000, 7):
        assert cli_threshold(compaction_env(at, True)) == at, at


def test_the_percentage_is_only_used_below_what_the_window_reaches():
    assert PCT not in compaction_env(300_000, True)
    assert compaction_env(300_000, True)[WINDOW] == "333000"


def test_what_turns_the_clis_auto_compact_off_elsewhere_is_undone():
    """Inherited by the hub, they would silently make --auto-compact do
    nothing; empty, the CLI reads them as unset (checked against 2.1.280)."""
    env = compaction_env(300_000, True)

    assert env["DISABLE_AUTO_COMPACT"] == "" and env["DISABLE_COMPACT"] == ""


@pytest.mark.parametrize("at", [967_001, 5_000_000])
def test_above_the_largest_window_it_compacts_as_late_as_the_cli_allows(at):
    assert cli_threshold(compaction_env(at, True)) == 967_000


def test_a_smaller_model_compacts_at_its_own_windows_threshold():
    """As it would without orchestrator2: the CLI caps the window."""
    assert cli_threshold(compaction_env(300_000, True), model_window=200_000) == 167_000


def test_off_leaves_the_cli_to_its_default():
    assert compaction_env(300_000, False) == {}


# -- The CLI is started with it ----------------------------------------------

def _bridge(argv=()):
    """A bridge whose CLI is never started: its connects are recorded."""
    cfg = parse_args(list(argv))
    st = init_state_from_config(cfg)
    sent: list[dict] = []

    async def bcast(msg):
        sent.append(msg)

    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)
    connects: list[str | None] = []

    async def fake_disconnect():
        pass

    async def fake_connect(resume_id=None, *, recovering=False):
        connects.append(resume_id)

    br.disconnect = fake_disconnect
    br.connect = fake_connect
    return br, st, sent, connects


def test_a_session_with_auto_compact_starts_its_cli_with_its_threshold():
    br, *_ = _bridge(["--auto-compact", "--compact-at", "300000"])

    options = br._make_options()

    assert options.env[WINDOW] == "333000"
    # A flag setting, which outranks the user's, project's and local ones.
    assert json.loads(options.settings) == {"autoCompactEnabled": True}


def test_a_session_left_to_the_default_overrides_no_setting():
    br, *_ = _bridge([])

    assert br._make_options().settings is None


def test_the_sessions_own_threshold_outranks_its_configs():
    br, st, *_ = _bridge([])
    st._compact_at, st._auto_compact = 200_000, True

    assert br._make_options().env[WINDOW] == "233000"


def test_a_session_without_it_neither_sets_nor_inherits_the_variables(monkeypatch):
    """A hub started from a session's Bash tool would otherwise give that
    session's threshold to every session it runs."""
    monkeypatch.setenv(WINDOW, "500000")
    monkeypatch.setenv(PCT, "50")
    br, *_ = _bridge([])

    env = br._make_options().env

    assert WINDOW not in env and PCT not in env
    assert WINDOW not in os.environ and PCT not in os.environ


class _CLI:
    """The SDK client, never started: the options it was made with are kept."""
    made: list = []

    def __init__(self, options=None):
        _CLI.made.append(options)

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def receive_messages(self):
        while True:
            await asyncio.sleep(3600)
            yield None      # pragma: no cover


def _connect_hub_bridge(monkeypatch, cfg, st, resume_id):
    """Connect a bridge the hub made, as the hub makes it, to *resume_id*."""
    _CLI.made = []
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _CLI)

    async def bcast(_msg):
        pass

    br = server._new_bridge(cfg, st, bcast)

    async def go():
        await br.connect(resume_id=resume_id)
        if br._dispatcher_task is not None:
            br._dispatcher_task.cancel()
            await asyncio.gather(br._dispatcher_task, return_exceptions=True)

    asyncio.run(go())
    return br


SID = "0d19661f-62b7-47b2-a94f-d7537aec0fa5"


def test_a_reopened_session_starts_its_cli_with_the_threshold_it_kept(monkeypatch):
    """Reopened without the launch that set it -- from the session list,
    after a hub restart.  The CLI names the session only once it has started,
    too late for its environment."""
    save_session_compaction(SID, 300000, True)
    st = State()

    _connect_hub_bridge(monkeypatch, parse_args([]), st, SID)

    assert _CLI.made[0].env[WINDOW] == "333000"
    assert st.cli_compaction == (300_000, True)


def test_a_session_left_to_the_default_starts_its_cli_without_them(monkeypatch):
    st = State()
    cfg = parse_args([])

    _connect_hub_bridge(monkeypatch, cfg, st, "1e2d3c4b-0000-4000-8000-000000000002")

    assert WINDOW not in _CLI.made[0].env
    assert st.cli_compaction == (cfg.compact_at, False)


# -- A change reaches the CLI before the session's next turn -----------------

class _NoQuery:
    """A client the turn gets as far as: query() is where it stops."""


def _turn(br):
    """Start a turn up to the point it sends its prompt."""
    br.client = _NoQuery()
    with pytest.raises(AttributeError):
        asyncio.run(br.run_turn("next prompt"))


def _changed(argv=()):
    """A bridge whose CLI started at the CLI's default, on a session since
    given auto-compact at 300k (/autocompact, or a launch onto it)."""
    br, st, sent, connects = _bridge(argv)
    st.session_id = "S1"
    st.cli_compaction = (br.config.compact_at, False)
    st._compact_at, st._auto_compact = 300_000, True
    return br, st, sent, connects


def _shown(sent):
    return " | ".join(str(m.get("data", {}).get("message", "")) for m in sent)


def test_the_next_turn_restarts_the_cli_with_a_changed_threshold():
    br, st, sent, connects = _changed()

    _turn(br)

    assert connects == ["S1"]
    assert "Restarted the CLI to compact at ~300k tok" in _shown(sent)


def test_an_unchanged_one_leaves_the_cli_alone():
    br, st, _sent, connects = _bridge(["--auto-compact", "--compact-at", "300000"])
    st.session_id = "S1"
    st.cli_compaction = (300_000, True)
    st._compact_at = 300_000            # /autocompact 300000, as it was

    _turn(br)

    assert connects == []


def test_before_the_first_cli_there_is_nothing_to_restart():
    br, st, _sent, connects = _bridge()
    st._compact_at, st._auto_compact = 300_000, True

    _turn(br)

    assert connects == []


def test_a_change_undone_before_the_turn_costs_nothing():
    br, st, _sent, connects = _changed()
    st._auto_compact = False            # /autocompact off: as the CLI has it

    _turn(br)

    assert connects == []


def test_it_waits_for_another_turn_while_a_background_task_runs():
    """Restarting the CLI would orphan the task's notice, as a /model switch
    would."""
    br, st, _sent, connects = _changed()
    st.background_tasks["t1"] = {"name": "build", "seq": 1}

    _turn(br)

    assert connects == []


def test_it_waits_while_the_cli_says_it_is_running():
    """A turn the CLI has begun by itself -- to deliver a finished task's
    notice, say -- says "running" before its first output marks the session
    busy; a restart then would throw that turn, and the notice, away."""
    br, st, _sent, connects = _changed()
    br._cli_state = "running"

    _turn(br)

    assert connects == []


def test_it_waits_while_a_compaction_closed_turn_may_go_on():
    """After compacting, the CLI carries on with the turn, as one of its
    own, whose first output may not have come yet."""
    br, st, _sent, connects = _changed()
    st.pending_compact_turn_end = {"subtype": "success", "started_at": 0.0}

    _turn(br)

    assert connects == []


def test_it_waits_on_a_cli_that_is_already_gone():
    """The recovery that follows starts the new CLI with it anyway."""
    br, st, _sent, connects = _changed()
    br._transport_dead = True

    _turn(br)

    assert connects == []


def test_the_turn_after_does_it():
    br, st, _sent, connects = _changed()
    br._cli_state = "running"
    _turn(br)
    br._cli_state = "idle"

    _turn(br)

    assert connects == ["S1"]


def test_nothing_restarts_the_cli_while_the_session_is_parked():
    """Parked is not quiet: the CLI may be in a turn of its own."""
    br, st, _sent, connects = _changed()
    for event in (("wakeup", "bg-all-done"), ("wakeup", "queue-edit-done"),
                  ("quit", "")):
        br.event_queue.put_nowait(event)

    assert asyncio.run(br._await_next_prompt()) is None
    assert connects == []


_PARKED = "<parked>"


def _looped(argv=()):
    br, st, sent, connects = _bridge(argv)

    async def parked():
        return _PARKED

    async def no_recycle(*, force=False):
        return False

    br._await_next_prompt = parked
    br._maybe_recycle_cli = no_recycle
    return br, st, sent, connects


def test_nor_does_the_end_of_a_turn():
    """The CLI may begin a turn of its own straight after ours."""
    br, st, _sent, connects = _looped()
    st.session_id = "S1"
    st.cli_compaction = (300_000, True)
    st._auto_compact = False

    asyncio.run(br._between_turns("done", False))

    assert connects == []


def test_orchestrator2_never_compacts_by_itself():
    """The count it once compacted by is not the CLI's, and the CLI does it."""
    br, st, sent, _connects = _looped(["--auto-compact", "--compact-at", "100000"])
    st.context_tokens = 5_000_000
    st.turns = 50

    assert asyncio.run(br._between_turns("done", False)) == _PARKED
    assert not [m for m in sent if "ompact" in str(m.get("data", {}).get("message", ""))]


def test_autocompact_says_when_the_cli_has_yet_to_take_it():
    import commands
    st = State()
    cfg = parse_args([])
    st.cli_compaction = (cfg.compact_at, False)

    msg = commands._cmd_autocompact("300000", st, cfg).messages[0]["data"]["message"]

    assert msg == ("auto-compact ON: the CLI compacts at ~300000 tok -- from the "
                   "session's next turn, which restarts the CLI to take it; "
                   "until then, at its own default")


def test_autocompact_says_nothing_more_once_it_has():
    import commands
    st = State()
    cfg = parse_args([])
    st.cli_compaction = (300_000, True)
    st._compact_at, st._auto_compact = 300_000, True

    msg = commands._cmd_autocompact("", st, cfg).messages[0]["data"]["message"]

    assert msg == "auto-compact ON: the CLI compacts at ~300000 tok"


# -- The count shown is the latest call's ------------------------------------

def _assistant(input_tokens, *, parent=None, model="claude-opus-5-5"):
    return AssistantMessage(
        content=[], model=model, parent_tool_use_id=parent,
        usage={"input_tokens": 3, "cache_read_input_tokens": input_tokens - 1_203,
               "cache_creation_input_tokens": 1_200, "output_tokens": 900})


class _Stream:
    def __init__(self, messages):
        self.messages = messages

    async def receive_messages(self):
        for m in self.messages:
            yield m


def _dispatch(messages):
    """Run *messages* through the bridge's dispatcher, as from the CLI."""
    br, st, *_ = _bridge()

    async def rendered(_msg):
        pass

    br._handle_async_message = rendered
    br.client = _Stream(messages)
    asyncio.run(br._message_dispatcher(gen=br._dispatcher_gen))
    return st


def test_a_turn_of_many_calls_counts_the_last_not_their_sum():
    st = _dispatch([_assistant(100_000), _assistant(110_000), _assistant(120_000)])

    assert st.context_tokens == 120_000


def test_a_subagents_context_is_not_the_sessions():
    st = _dispatch([_assistant(120_000), _assistant(30_000, parent="toolu_1")])

    assert st.context_tokens == 120_000


def test_the_clis_own_messages_carry_no_count():
    st = _dispatch([_assistant(120_000),
                    AssistantMessage(content=[], model="<synthetic>",
                                     usage={"input_tokens": 0, "output_tokens": 0})])

    assert st.context_tokens == 120_000


def _boundary():
    return SystemMessage(subtype="compact_boundary", data={
        "type": "system", "subtype": "compact_boundary",
        "compact_metadata": {"trigger": "auto", "pre_tokens": 312_345}})


def test_a_compaction_empties_it_until_the_next_call():
    st = _dispatch([_assistant(300_000), _boundary()])

    assert st.context_tokens == 0


def test_rendering_a_compaction_late_does_not_wipe_the_next_calls_count():
    """The dispatcher can be ahead of the path that renders the boundary."""
    br, st, *_ = _bridge()
    st.context_tokens = 45_000          # the call after the compaction

    asyncio.run(br._handle_system_message(_boundary(), during_turn=True))

    assert st.context_tokens == 45_000


def test_the_turns_result_does_not_overwrite_it():
    """It is the sum of every call of the turn."""
    br, st, *_ = _bridge()
    st.context_tokens = 120_000
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=3, session_id="S1",
        usage={"input_tokens": 9, "cache_read_input_tokens": 3_000_000,
               "cache_creation_input_tokens": 5_000})

    asyncio.run(br._handle_async_message(result))

    assert st.context_tokens == 120_000


# -- A compaction, as the CLI reports it -------------------------------------

def test_a_compaction_is_reported_with_the_clis_trigger_and_count():
    br, st, sent, _ = _bridge()
    st.context_tokens = 300_500         # orchestrator2's own, not shown

    asyncio.run(br._handle_system_message(_boundary(), during_turn=True))

    shown = [m["data"]["message"] for m in sent
             if isinstance(m.get("data"), dict) and "message" in m["data"]]
    assert "[compacted -- auto -- was ~312k tok]" in shown
