"""A session on another account is opened and kept in that account, with the
hub's environment left alone.

Every lookup takes the session's config dir explicitly.  Pointing the hub's
CLAUDE_CONFIG_DIR at the session's account for a moment instead would show
any worker thread reading it then -- the sign-in watcher, say -- another
account.  And what a session keeps on disk (its queued prompts, its record of
background tasks) is kept in its own account, where it is looked for.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                    # noqa: E402
from config import parse_args                    # noqa: E402
from session import (                            # noqa: E402
    load_persisted_bg_tasks,
    load_persisted_queue,
    save_persisted_bg_tasks,
)
from state import State                          # noqa: E402


@pytest.fixture
def accounts(monkeypatch, tmp_path):
    """The hub's account, in its environment, and another one."""
    hub = str(tmp_path / "acct-hub")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", hub)
    monkeypatch.setattr(server, "config", parse_args([]))
    return types.SimpleNamespace(hub=hub, other=str(tmp_path / "acct-b"),
                                 cwd=str(tmp_path))


def test_the_most_recent_session_is_found_in_the_account_given(accounts):
    from pathlib import Path
    from session import _sanitize_cwd, find_most_recent_session_for_cwd
    project = (Path(accounts.other) / "projects"
               / _sanitize_cwd(str(Path(accounts.cwd).resolve())))
    project.mkdir(parents=True)
    (project / "sid-b.jsonl").write_text("{}\n", encoding="utf-8")
    assert find_most_recent_session_for_cwd(accounts.cwd, accounts.other).stem == "sid-b"
    assert find_most_recent_session_for_cwd(accounts.cwd) is None


def test_opening_a_session_on_another_account_leaves_the_environment_alone(
        accounts, monkeypatch):
    seen = []

    def recent(cwd, config_dir=None):
        seen.append(("recent", config_dir, os.environ.get("CLAUDE_CONFIG_DIR")))
        return None

    def stop(cfg):
        seen.append(("state", cfg.config_dir, os.environ.get("CLAUDE_CONFIG_DIR")))
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "find_most_recent_session_for_cwd", recent)
    monkeypatch.setattr(server, "init_state_from_config", stop)
    with pytest.raises(RuntimeError):
        asyncio.run(server._create_runtime(cwd=accounts.cwd,
                                           config_dir=accounts.other))
    assert seen == [("recent", accounts.other, accounts.hub),
                    ("state", accounts.other, accounts.hub)]


def test_a_launch_on_another_account_looks_for_its_session_there(accounts, monkeypatch):
    seen = []

    def recent(cwd, config_dir=None):
        seen.append((config_dir, os.environ.get("CLAUDE_CONFIG_DIR")))
        return None

    async def create(**kw):
        return types.SimpleNamespace(rid="R1")

    monkeypatch.setattr(server, "find_most_recent_session_for_cwd", recent)
    monkeypatch.setattr(server, "_create_runtime", create)
    monkeypatch.setattr(server, "runtimes", {})
    out = asyncio.run(server.api_session_launch(
        {"cwd": accounts.cwd, "config_dir": accounts.other}))
    assert out["ok"], out
    assert seen == [(accounts.other, accounts.hub)]


def test_a_session_opened_on_another_account_keeps_its_queue_there(accounts, monkeypatch):
    import sdk_bridge
    attached = []

    class _Bridge:
        def __init__(self, config, state, broadcaster):
            self._initial_resume_id = None

        async def start(self):
            pass

    async def nothing(*a, **k):
        pass

    monkeypatch.setattr(sdk_bridge, "SDKBridge", _Bridge)
    monkeypatch.setattr(server, "_attach_queue_persistence",
                        lambda st, cwd, config_dir=None: attached.append(config_dir) or 0)
    monkeypatch.setattr(server, "_push_session_list", nothing)
    monkeypatch.setattr(server, "runtimes", {})
    asyncio.run(server._create_runtime(cwd=accounts.cwd, config_dir=accounts.other,
                                       no_continue=True))
    assert attached == [accounts.other]


def test_a_sessions_queue_is_kept_in_its_own_account(accounts):
    st = State()
    st.session_id = "sid-b"
    server._attach_queue_persistence(st, accounts.cwd, accounts.other)
    st.queued_prompts.append("later")
    assert load_persisted_queue(accounts.cwd, "sid-b",
                                config_dir=accounts.other) == ["later"]
    assert load_persisted_queue(accounts.cwd, "sid-b") == [], \
        "saved in the hub's account, where reopening it will not look"


def test_a_queue_kept_in_its_own_account_comes_back(accounts):
    first = State()
    first.session_id = "sid-b"
    server._attach_queue_persistence(first, accounts.cwd, accounts.other)
    first.queued_prompts.append("later")
    again = State()
    again.session_id = "sid-b"
    assert server._attach_queue_persistence(again, accounts.cwd, accounts.other) == 1
    assert list(again.queued_prompts) == ["later"]


def test_a_sessions_task_record_is_kept_in_its_own_account(accounts):
    import sdk_bridge
    cfg = parse_args(["--config-dir", accounts.other, "--cwd", accounts.cwd])
    st = State()
    st.session_id = "sid-b"
    st.background_tasks["t1"] = {"name": "build", "command": "make"}
    br = sdk_bridge.SDKBridge(config=cfg, state=st, broadcaster=None)
    br._save_bg_tasks()
    assert [i["task_id"] for i in load_persisted_bg_tasks(
        cfg.cwd, "sid-b", config_dir=accounts.other)] == ["t1"]
    assert load_persisted_bg_tasks(cfg.cwd, "sid-b") == []


def test_a_task_record_is_read_back_from_its_own_account(accounts):
    import sdk_bridge
    save_persisted_bg_tasks(accounts.cwd, {"t1": {"name": "build"}}, "sid-b",
                            accounts.other)
    cfg = parse_args(["--config-dir", accounts.other, "--cwd", accounts.cwd])

    async def nothing(_msg):
        pass

    br = sdk_bridge.SDKBridge(config=cfg, state=State(), broadcaster=nothing)
    assert asyncio.run(br._report_lost_bg_tasks("sid-b")) == 1
