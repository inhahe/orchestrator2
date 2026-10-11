"""The cost figure across a session's CLI being replaced, and its hub too.

``total_cost_usd`` in a result is the CLI's running total for the session.
What a new CLI starts from depends on which CLI it is.  Measured where the SDK
runs them, with a stand-in API and a fake key, at $0.0105 a turn:

* 2.1.280 saves its total in the transcript (a ``cost-state`` record) when it
  exits cleanly, and one resuming the session starts from the last saved:
  $0.021, resumed, $0.0315, resumed, $0.042.  Killed outright, it saves
  nothing, and its successor starts from the last save before it.
* 2.1.259, the SDK's own, neither saves nor restores: every new process starts
  from $0.  Resumed after a 2.1.280 that saved $0.021, it still started from
  $0, and a 2.1.280 after it restored that $0.021 again.

So when a new CLI's first init says which CLI it is, the hub carries over what
the CLI before it spent that the new one will not count (``state.carry_cost``,
in ``SDKBridge._settle_cost_sync``), and keeps what it knows per session
(``session.save_session_cost``), for a state made from scratch later.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdk_bridge                                        # noqa: E402
from config import parse_args                            # noqa: E402
from session import (_sanitize_cwd, load_session_cost,   # noqa: E402
                     save_session_cost, saved_cost)
from state import (State, carry_cost, init_state_from_config,  # noqa: E402
                   restores_saved_cost, take_cost)

SID = "c0570000-0000-0000-0000-000000000001"
OLD, NEW = "2.1.259", "2.1.280"


def _record(total, sid=SID):
    return {"type": "cost-state", "sessionId": sid, "totalCostUSD": total,
            "totalAPIDuration": 1, "totalAPIDurationWithoutRetries": 1,
            "totalToolDuration": 0, "totalLinesAdded": 0, "totalLinesRemoved": 0,
            "totalDuration": 1, "startTime": 1, "modelUsage": {}}


def _turn(n):
    return {"type": "user", "sessionId": SID, "uuid": f"u{n}",
            "message": {"role": "user", "content": "x" * 50}}


def _write(path: Path, records, mode="w") -> Path:
    with open(path, mode, encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in records))
    return path


def _result(cost):
    return types.SimpleNamespace(total_cost_usd=cost)


# --------------------------------------------------------------------------
# What a transcript saved
# --------------------------------------------------------------------------

def test_the_last_saved_total_is_found(tmp_path):
    jsonl = _write(tmp_path / f"{SID}.jsonl",
                   [_turn(1), _record(0.021), _turn(2), _record(0.0315), _turn(3)])
    assert saved_cost(jsonl, SID) == 0.0315


def test_a_range_without_one_has_none(tmp_path):
    jsonl = _write(tmp_path / f"{SID}.jsonl", [_record(0.021), _turn(1)])
    start = len(json.dumps(_record(0.021))) + 1
    assert saved_cost(jsonl, SID, start) is None
    assert saved_cost(jsonl, SID, 0, start) == 0.021


def test_another_sessions_saved_total_is_not_this_ones(tmp_path):
    # A transcript /move copied still carries the original's records.
    jsonl = _write(tmp_path / f"{SID}.jsonl",
                   [_record(0.5), _record(9.0, sid="someone-else")])
    assert saved_cost(jsonl, SID) == 0.5


@pytest.mark.parametrize("junk", [-1.0, "3", None, True, float("nan"), 2e9])
def test_a_saved_total_the_cli_would_refuse_is_passed_over(tmp_path, junk):
    jsonl = _write(tmp_path / f"{SID}.jsonl", [_record(0.25), _record(junk)])
    assert saved_cost(jsonl, SID) == 0.25


def test_a_line_still_being_written_is_passed_over(tmp_path):
    jsonl = _write(tmp_path / f"{SID}.jsonl", [_record(0.25)])
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write('{"type": "cost-state", "sessionId": "' + SID + '", "totalCo')
    assert saved_cost(jsonl, SID) == 0.25


def test_it_is_found_far_back_across_reads(tmp_path):
    # Read backwards a block at a time: a record split across two blocks.
    jsonl = _write(tmp_path / f"{SID}.jsonl",
                   [_record(0.75)] + [_turn(n) for n in range(40)])
    assert saved_cost(jsonl, SID, chunk=97) == 0.75


def test_a_transcript_that_cannot_be_read_says_so(tmp_path):
    with pytest.raises(OSError):
        saved_cost(tmp_path / "missing.jsonl", SID)


@pytest.mark.parametrize("version, restores", [
    ("2.1.259", False), ("2.1.232", False), ("2.1.260", True), ("2.1.280", True),
    ("2.2.0", True), (None, True), ("not a version", True)])
def test_which_clis_start_from_the_saved_total(version, restores):
    assert restores_saved_cost(version) is restores


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------

def test_the_figure_is_the_clis_running_total():
    st = State()
    take_cost(st, _result(0.0105))
    take_cost(st, _result(0.021))
    assert st.total_cost_usd == 0.021


def test_what_the_new_cli_does_not_count_is_carried():
    st = State()
    take_cost(st, _result(0.042))
    carry_cost(st, 0.042, 0.0315)
    assert st.cost_reported is None
    assert st.total_cost_usd == pytest.approx(0.042), "the figure stands till it reports"
    take_cost(st, _result(0.042))
    assert st.total_cost_usd == pytest.approx(0.0525)


def test_a_carry_can_take_back_what_a_restore_counts_again():
    st = State()
    st.cost_carried = 0.021
    take_cost(st, _result(0.0105))
    carry_cost(st, 0.0105, 0.021)
    assert st.cost_carried == pytest.approx(0.0105)


# --------------------------------------------------------------------------
# Settling at a new CLI's first init
# --------------------------------------------------------------------------

def _bridge(tmp_path):
    """A bridge on a session, its transcript, and a way to drive its CLIs."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    cfg_dir = tmp_path / "acct"
    project = cfg_dir / "projects" / _sanitize_cwd(str(cwd.resolve()))
    project.mkdir(parents=True)
    jsonl = project / f"{SID}.jsonl"
    jsonl.write_text("", encoding="utf-8")
    cfg = dataclasses.replace(parse_args([]), cwd=str(cwd), config_dir=str(cfg_dir),
                              allow_duplicate_session=True)
    st = init_state_from_config(cfg)
    st.session_id = SID

    async def bc(_m):
        pass

    br = sdk_bridge.SDKBridge(config=cfg, state=st, broadcaster=bc)
    return types.SimpleNamespace(br=br, st=st, jsonl=jsonl, cfg_dir=str(cfg_dir))


def _cli(s, version, turns, *, saves_on_exit=None):
    """A CLI of *version* taking the session over and reporting *turns*
    (its running totals); then exiting, saving *saves_on_exit* if given."""
    s.br._settle_cost_sync(version, SID)
    for total in turns:
        _write(s.jsonl, [_turn(len(turns))], "a")
        take_cost(s.st, _result(total))
    if saves_on_exit is not None:
        _write(s.jsonl, [_record(saves_on_exit)], "a")


def test_a_cli_that_restores_nothing_has_its_predecessors_spend_carried(tmp_path):
    s = _bridge(tmp_path)
    _cli(s, OLD, [0.0105, 0.021])
    _cli(s, OLD, [0.0105])
    assert s.st.total_cost_usd == pytest.approx(0.0315)


def test_a_cli_that_restores_after_a_clean_stop_carries_nothing(tmp_path):
    s = _bridge(tmp_path)
    _cli(s, NEW, [0.0105, 0.021], saves_on_exit=0.021)
    _cli(s, NEW, [0.0315])
    assert s.st.cost_carried == pytest.approx(0.0)
    assert s.st.total_cost_usd == pytest.approx(0.0315)


def test_a_killed_clis_spend_is_carried(tmp_path):
    # Measured: killed at $0.042, its successor restored $0.0315.
    s = _bridge(tmp_path)
    _cli(s, NEW, [0.0105, 0.021], saves_on_exit=0.021)
    _cli(s, NEW, [0.0315], saves_on_exit=0.0315)
    _cli(s, NEW, [0.042])                                   # killed
    _cli(s, NEW, [0.042])
    assert s.st.total_cost_usd == pytest.approx(0.0525)


def test_what_was_spent_after_the_last_report_is_counted(tmp_path):
    # It saved more than it reported: an interrupted turn, say.
    s = _bridge(tmp_path)
    _cli(s, NEW, [0.02], saves_on_exit=0.03)
    _cli(s, NEW, [0.04])
    assert s.st.total_cost_usd == pytest.approx(0.04)


def test_switching_cli_versions_counts_each_turn_once(tmp_path):
    # Measured: 2.1.280 saved $0.021, 2.1.259 started from $0, and a 2.1.280
    # after it restored the $0.021 again.  Four turns at $0.0105.
    s = _bridge(tmp_path)
    _cli(s, NEW, [0.0105, 0.021], saves_on_exit=0.021)
    _cli(s, OLD, [0.0105])
    assert s.st.total_cost_usd == pytest.approx(0.0315)
    _cli(s, NEW, [0.0315])
    assert s.st.total_cost_usd == pytest.approx(0.042)


def test_a_restoring_cli_after_ones_that_saved_nothing_looks_back_once(tmp_path):
    # The figure is what the first CLI the hub saw counted, and all spent
    # since.  That one counted from nothing, so a total saved long before is
    # not counted when a later CLI restores it: two turns, $0.021.
    s = _bridge(tmp_path)
    _write(s.jsonl, [_record(0.5)], "a")         # saved long ago
    _cli(s, OLD, [0.0105])
    assert s.st.cost_mark_saved is None, "looked for before anything needed it"
    _cli(s, NEW, [0.5105])
    assert s.st.total_cost_usd == pytest.approx(0.021)
    assert s.st.cost_mark_saved == 0.5


def test_a_first_cli_carries_nothing_and_reads_nothing(tmp_path):
    s = _bridge(tmp_path)
    _write(s.jsonl, [_record(0.5)], "a")
    s.br._settle_cost_sync(NEW, SID)
    assert (s.st.cost_carried, s.st.cost_mark_saved) == (0.0, None)
    assert s.st.cost_mark_size == s.jsonl.stat().st_size


# --------------------------------------------------------------------------
# Kept across the hub
# --------------------------------------------------------------------------

def test_what_the_hub_knows_is_kept_with_each_result(tmp_path):
    s = _bridge(tmp_path)
    _cli(s, OLD, [0.0105])
    s.br._keep_cost()
    kept = load_session_cost(SID, s.cfg_dir)
    assert kept["reported"] == 0.0105 and kept["restores"] is False


def _sdk_result(cost):
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5,
                         is_error=False, num_turns=1, session_id=SID,
                         total_cost_usd=cost)


def test_a_result_between_turns_keeps_the_figure(tmp_path):
    s = _bridge(tmp_path)
    asyncio.run(s.br._handle_async_message(_sdk_result(0.03)))
    assert load_session_cost(SID, s.cfg_dir)["reported"] == 0.03


def test_a_turns_result_keeps_the_figure(tmp_path, monkeypatch):
    monkeypatch.setattr(sdk_bridge, "TURN_END_SETTLE_S", 0.01)
    s = _bridge(tmp_path)

    class Client:
        async def query(self, _prompt):
            s.br.turn_msg_queue.put_nowait(_sdk_result(0.05))

        async def interrupt(self):  # pragma: no cover
            pass

    s.br.client = Client()
    asyncio.run(s.br.run_turn("go"))
    assert load_session_cost(SID, s.cfg_dir)["reported"] == 0.05


def test_a_state_made_from_scratch_starts_from_what_was_kept(tmp_path):
    # Reopened after a clean close, on the SDK's CLI: it restores nothing,
    # but the session had cost $0.0315.
    s = _bridge(tmp_path)
    save_session_cost(SID, {"reported": 0.021, "carried": 0.0105,
                            "mark_size": 0, "mark_saved": None,
                            "restores": False}, s.cfg_dir)
    _cli(s, OLD, [0.0105])
    assert s.st.total_cost_usd == pytest.approx(0.042)


def test_kept_numbers_that_make_no_sense_are_not_taken(tmp_path):
    s = _bridge(tmp_path)
    save_session_cost(SID, {"reported": "lots", "carried": True,
                            "mark_size": -4, "restores": "yes"}, s.cfg_dir)
    s.br._settle_cost_sync(OLD, SID)
    assert (s.st.cost_reported, s.st.cost_carried, s.st.cost_restores) == (None, 0.0, False)


def test_a_settle_that_fails_carries_nothing_and_breaks_nothing(tmp_path, monkeypatch):
    s = _bridge(tmp_path)
    take_cost(s.st, _result(0.04))

    def broken(*_a, **_k):
        raise RecursionError("a line nested too deep")

    monkeypatch.setattr(sdk_bridge, "saved_cost", broken)
    s.st.cost_restores = True
    s.st.cost_mark_size = 0
    asyncio.run(s.br._settle_cost(NEW, SID))
    assert s.st.cost_reported is None and s.st.cost_carried == 0.0


# --------------------------------------------------------------------------
# The bridge: a connect's first init settles, /clear forgets
# --------------------------------------------------------------------------

def test_a_new_clis_first_init_settles_and_the_next_does_not(tmp_path, monkeypatch):
    s = _bridge(tmp_path)
    settled = []
    monkeypatch.setattr(s.br, "_settle_cost_sync",
                        lambda version, sid: settled.append((version, sid)))
    s.br._cost_settle_due = True        # as connect() leaves it
    init = types.SimpleNamespace(subtype="init", data={
        "session_id": SID, "claude_code_version": OLD, "apiKeySource": "none"})

    async def go():
        await s.br._handle_system_message(init, during_turn=True)
        await s.br._handle_system_message(init, during_turn=True)
    asyncio.run(go())
    assert settled == [(OLD, SID)]


class _CLI:
    def __init__(self, options=None):
        self.options = options

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def receive_messages(self):
        while True:
            await asyncio.sleep(3600)
            yield None      # pragma: no cover


def test_a_connect_leaves_the_settle_to_the_new_clis_first_init(tmp_path, monkeypatch):
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _CLI)
    s = _bridge(tmp_path)

    async def go():
        await s.br.connect(SID)
        s.br._dispatcher_task.cancel()
        await asyncio.gather(s.br._dispatcher_task, return_exceptions=True)
    asyncio.run(go())
    assert s.br._cost_settle_due is True


def test_clearing_the_session_forgets_its_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _CLI)
    s = _bridge(tmp_path)
    _cli(s, NEW, [0.042])
    s.st.cost_carried = 0.01

    async def go():
        await s.br._clear_context()
        if s.br._dispatcher_task is not None:
            s.br._dispatcher_task.cancel()
            await asyncio.gather(s.br._dispatcher_task, return_exceptions=True)
    asyncio.run(go())
    st = s.st
    assert (st.total_cost_usd, st.cost_reported, st.cost_carried, st.cost_mark_size,
            st.cost_mark_saved, st.cost_restores) == (0.0, None, 0.0, None, None, None)
