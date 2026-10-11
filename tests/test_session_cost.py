"""The cost figure across a session's CLI being replaced.

``total_cost_usd`` in a result is the CLI's running total for the session.  A
CLI resuming a session starts from the total its predecessor saved in the
transcript (a ``cost-state`` record) when it exited cleanly -- measured with
CLI 2.1.280, a stand-in API and a fake key: $0.021, resumed, $0.0315, resumed,
$0.042.  So a CLI replaced cleanly is carried on by the CLI itself.  One that
was killed saved nothing: its successor starts from the last clean save, and
what the killed one spent after it is gone from the CLI's figure.  The hub
adds that back (``state.carry_cost``), reading what the successor restores
from the same record (``session.saved_cost``).
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
from session import _sanitize_cwd, saved_cost            # noqa: E402
from state import (State, carry_cost,                    # noqa: E402
                   init_state_from_config, take_cost)

SID = "c0570000-0000-0000-0000-000000000001"


def _record(total, sid=SID):
    return {"type": "cost-state", "sessionId": sid, "totalCostUSD": total,
            "totalAPIDuration": 1, "totalAPIDurationWithoutRetries": 1,
            "totalToolDuration": 0, "totalLinesAdded": 0, "totalLinesRemoved": 0,
            "totalDuration": 1, "startTime": 1, "modelUsage": {}}


def _transcript(path: Path, records) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _turn(n):
    return {"type": "user", "sessionId": SID, "uuid": f"u{n}",
            "message": {"role": "user", "content": "x" * 50}}


def _result(cost):
    return types.SimpleNamespace(total_cost_usd=cost)


# --------------------------------------------------------------------------
# What a CLI resuming the session restores
# --------------------------------------------------------------------------

def test_the_last_saved_total_is_restored(tmp_path):
    jsonl = _transcript(tmp_path / f"{SID}.jsonl",
                        [_turn(1), _record(0.021), _turn(2), _record(0.0315), _turn(3)])
    assert saved_cost(jsonl, SID) == 0.0315


def test_a_session_that_saved_none_restores_nothing(tmp_path):
    jsonl = _transcript(tmp_path / f"{SID}.jsonl", [_turn(1), _turn(2)])
    assert saved_cost(jsonl, SID) == 0.0


def test_another_sessions_saved_total_is_not_this_ones(tmp_path):
    # A transcript /move copied still carries the original's records.
    jsonl = _transcript(tmp_path / f"{SID}.jsonl",
                        [_record(0.5), _record(9.0, sid="someone-else")])
    assert saved_cost(jsonl, SID) == 0.5


@pytest.mark.parametrize("junk", [-1.0, "3", None, True, float("nan"), 2e9])
def test_a_saved_total_the_cli_would_refuse_is_passed_over(tmp_path, junk):
    jsonl = _transcript(tmp_path / f"{SID}.jsonl", [_record(0.25), _record(junk)])
    assert saved_cost(jsonl, SID) == 0.25


def test_a_line_still_being_written_is_passed_over(tmp_path):
    jsonl = _transcript(tmp_path / f"{SID}.jsonl", [_record(0.25)])
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write('{"type": "cost-state", "sessionId": "' + SID + '", "totalCo')
    assert saved_cost(jsonl, SID) == 0.25


def test_it_is_found_far_back_across_reads(tmp_path):
    # Read backwards a block at a time: a record split across two blocks.
    jsonl = _transcript(tmp_path / f"{SID}.jsonl",
                        [_record(0.75)] + [_turn(n) for n in range(40)])
    assert saved_cost(jsonl, SID, chunk=97) == 0.75


def test_a_transcript_that_cannot_be_read_restores_an_unknown_total(tmp_path):
    assert saved_cost(tmp_path / "missing.jsonl", SID) is None


# --------------------------------------------------------------------------
# What the hub carries across a CLI being replaced
# --------------------------------------------------------------------------

def test_the_figure_is_the_clis_running_total():
    st = State()
    take_cost(st, _result(0.0105))
    take_cost(st, _result(0.021))
    assert st.total_cost_usd == 0.021


def test_a_cli_replaced_cleanly_carries_nothing_over():
    # It saved its total, and its successor starts from it.
    st = State()
    take_cost(st, _result(0.021))
    carry_cost(st, 0.021)
    take_cost(st, _result(0.0315))
    assert st.total_cost_usd == pytest.approx(0.0315)


def test_what_a_killed_cli_spent_is_carried_over():
    # Measured: killed at $0.042, its successor restored $0.0315 and reported
    # $0.042 after one more $0.0105 turn.  The session has cost $0.0525.
    st = State()
    take_cost(st, _result(0.042))
    carry_cost(st, 0.0315)
    take_cost(st, _result(0.042))
    assert st.total_cost_usd == pytest.approx(0.0525)


def test_more_restored_than_last_reported_carries_nothing():
    # Spent after its last result (an interrupted turn), saved at a clean exit.
    st = State()
    take_cost(st, _result(0.02))
    carry_cost(st, 0.03)
    assert st.cost_carried == 0.0


def test_a_restored_total_that_could_not_be_read_carries_nothing():
    st = State()
    take_cost(st, _result(0.02))
    carry_cost(st, None)
    assert st.cost_carried == 0.0


def test_a_cli_that_reported_nothing_carries_nothing():
    st = State()
    carry_cost(st, 0.0)
    assert st.cost_carried == 0.0


def test_carrying_twice_for_one_replacement_counts_once():
    # A connect that fails is tried again.
    st = State()
    take_cost(st, _result(0.042))
    carry_cost(st, 0.0315)
    carry_cost(st, 0.0315)
    assert st.cost_carried == pytest.approx(0.0105)


def test_the_figure_keeps_what_was_carried_until_the_next_result():
    st = State()
    take_cost(st, _result(0.042))
    carry_cost(st, 0.0315)
    assert st.total_cost_usd == pytest.approx(0.042)


# --------------------------------------------------------------------------
# A CLI's connect carries it
# --------------------------------------------------------------------------

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


def _resumable(tmp_path, records):
    """A bridge on a session whose transcript holds *records*."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    cfg_dir = tmp_path / "acct"
    project = cfg_dir / "projects" / _sanitize_cwd(str(cwd.resolve()))
    project.mkdir(parents=True)
    _transcript(project / f"{SID}.jsonl", records)
    # allow_duplicate_session: no scan of every process for another CLI on it.
    cfg = dataclasses.replace(parse_args([]), cwd=str(cwd), config_dir=str(cfg_dir),
                              allow_duplicate_session=True)
    st = init_state_from_config(cfg)
    st.session_id = SID

    async def bc(_m):
        pass

    return sdk_bridge.SDKBridge(config=cfg, state=st, broadcaster=bc), st


def _connect(br):
    async def go():
        await br.connect(SID)
        br._dispatcher_task.cancel()
        await asyncio.gather(br._dispatcher_task, return_exceptions=True)
    asyncio.run(go())


def test_a_connect_after_a_killed_cli_carries_what_it_spent(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _CLI)
    br, st = _resumable(tmp_path, [_turn(1), _record(0.0315), _turn(2)])
    take_cost(st, _result(0.042))           # the killed CLI's last report
    _connect(br)
    assert st.cost_carried == pytest.approx(0.0105)


def test_a_connect_after_a_clean_stop_carries_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _CLI)
    br, st = _resumable(tmp_path, [_turn(1), _record(0.042)])
    take_cost(st, _result(0.042))
    _connect(br)
    assert st.cost_carried == 0.0


def test_clearing_the_session_drops_what_was_carried(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk_bridge, "ClaudeSDKClient", _CLI)
    br, st = _resumable(tmp_path, [_turn(1)])
    take_cost(st, _result(0.042))
    carry_cost(st, 0.0)

    async def go():
        await br._clear_context()
        if br._dispatcher_task is not None:
            br._dispatcher_task.cancel()
            await asyncio.gather(br._dispatcher_task, return_exceptions=True)
    asyncio.run(go())
    assert (st.total_cost_usd, st.cost_carried, st.cost_reported) == (0.0, 0.0, None)
