"""The suite's own guard: no test writes the hub's files into a real account.

``conftest._no_hub_state_in_a_real_account`` once took only ``~/.claude*``
for a real account.  Run from a session whose account lived elsewhere (one
named with a relative ``--config-dir``, under a project), the suite filed
limit-resume records, compaction settings and costs into that account: what a
real hub would reopen and read back.  The records the earlier tests left then
failed ``test_resume_after_limit``'s restore tests, which expect to find only
their own.  And the status ticker's peer mirror, unguarded, copied the
machine's live sessions into every real account under the test's own registry.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import limit_resume                               # noqa: E402
import session                                    # noqa: E402
import wakeup_store                               # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def account_outside_home(monkeypatch):
    # Not a tmp_path: those are where a test's own accounts live.
    real = Path(tempfile.mkdtemp(prefix="orch2-guard-account-"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(real))
    yield real
    shutil.rmtree(real, ignore_errors=True)


def test_an_account_outside_home_gets_none_of_the_hubs_files(account_outside_home):
    limit_resume.save("D:/somewhere", SID, due_at=1.0, resets_at=1.0)
    wakeup_store.save_wakeup("D:/somewhere", SID, due_at=1.0, prompt="go on")
    assert not session._orch2_state_dir().is_relative_to(account_outside_home)
    assert list(account_outside_home.rglob("*")) == []


def test_no_live_session_is_mirrored_into_a_real_account(account_outside_home,
                                                         tmp_path, monkeypatch):
    # The status ticker's mirror: its copies are recorded in the test's own
    # registry, so the real hub would never remove one.
    import agent_comms
    monkeypatch.setattr(agent_comms, "_pid_is_a_live_claude", lambda *_a: True)
    own = tmp_path / "acct"
    (own / "sessions").mkdir(parents=True)
    (own / "sessions" / "123.json").write_text('{"pid": 123}', encoding="utf-8")
    (account_outside_home / "sessions").mkdir()
    other = tmp_path / "other"
    (other / "sessions").mkdir(parents=True)
    agent_comms.mirror_peer_registries([own, account_outside_home, other])
    assert list((account_outside_home / "sessions").iterdir()) == []
    # Between a test's own accounts it still works.
    assert (other / "sessions" / "123.json").exists()


def test_a_tests_own_account_is_still_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    limit_resume.save("D:/somewhere", SID, due_at=1.0, resets_at=1.0)
    assert list((tmp_path / "orchestrator2" / "limit-resumes").iterdir())
    assert session._orch2_state_dir() == tmp_path / "orchestrator2"
