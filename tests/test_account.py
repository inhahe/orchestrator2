"""A CLI on the default account runs with ``CLAUDE_CONFIG_DIR`` unset.

The CLI keeps its global config (user MCP servers, trusted projects, the
account's details) in ``$CLAUDE_CONFIG_DIR/.claude.json`` when the variable is
set, and in ``~/.claude.json`` when it is not, so ``~/.claude`` named outright
gives it a second global config.  The SDK can only add to a CLI's environment,
so the hub keeps no ``CLAUDE_CONFIG_DIR`` of its own (``account.py``).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account                                      # noqa: E402
import auth                                         # noqa: E402
from btw import build_btw_options                   # noqa: E402
from config import parse_args                       # noqa: E402
from state import (State, detect_account_info,      # noqa: E402
                   global_config_path, init_state_from_config)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A home directory with the default account and account b in it."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / "acct-b").mkdir()
    return tmp_path


# --------------------------------------------------------------------------
# The account a CLI is given
# --------------------------------------------------------------------------

def test_the_default_account_named_outright_is_given_as_none(home):
    assert account.normalize(str(home / ".claude")) is None
    assert account.normalize(None) is None
    assert account.normalize(str(home / "acct-b")) == str((home / "acct-b").resolve())


def test_a_cli_on_the_default_account_finds_the_variable_unset(home):
    env = account.child_env(str(home / ".claude"),
                            base={"CLAUDE_CONFIG_DIR": str(home / ".claude"), "X": "1"})
    assert "CLAUDE_CONFIG_DIR" not in env and env["X"] == "1"


def test_a_cli_on_another_account_is_given_it(home):
    env = account.child_env(str(home / "acct-b"), base={})
    assert env["CLAUDE_CONFIG_DIR"] == str((home / "acct-b").resolve())


def test_a_session_without_its_own_account_has_the_hubs(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    account.adopt_from_environment()
    assert account.cli_config_dir(None) == str((home / "acct-b").resolve())
    assert account.child_env(None, base={})["CLAUDE_CONFIG_DIR"] == account.cli_config_dir(None)


# --------------------------------------------------------------------------
# The hub keeps none of its own
# --------------------------------------------------------------------------

def test_the_hub_takes_its_account_out_of_its_environment(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    given = account.adopt_from_environment()
    assert "CLAUDE_CONFIG_DIR" not in os.environ
    assert given == str((home / "acct-b").resolve())
    assert account.hub_account() == given
    assert account.account_dir() == Path(given)


def test_config_dir_given_beats_the_environment(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    given = account.adopt_from_environment(str(home / ".claude"))
    assert given == str((home / ".claude").resolve())
    assert account.hub_account() is None
    assert "CLAUDE_CONFIG_DIR" not in os.environ


def test_a_hub_on_the_default_account_has_none(home):
    assert account.adopt_from_environment() is None
    assert account.hub_account() is None
    assert account.account_dir() == home / ".claude"


# --------------------------------------------------------------------------
# Every CLI the hub starts
# --------------------------------------------------------------------------

def _bridge_env(config_dir):
    import sdk_bridge

    async def bc(_m):
        pass

    cfg = dataclasses.replace(parse_args([]), config_dir=config_dir)
    br = sdk_bridge.SDKBridge(config=cfg, state=init_state_from_config(cfg),
                              broadcaster=bc)
    return br._make_options().env


def test_a_sessions_cli_on_the_default_account_named_outright(home):
    account.adopt_from_environment()
    assert "CLAUDE_CONFIG_DIR" not in _bridge_env(str(home / ".claude"))


def test_a_sessions_cli_on_another_account(home):
    account.adopt_from_environment()
    assert _bridge_env(str(home / "acct-b"))["CLAUDE_CONFIG_DIR"] == str(
        (home / "acct-b").resolve())


def test_a_sessions_cli_on_the_hubs_account(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    account.adopt_from_environment()
    assert _bridge_env(None)["CLAUDE_CONFIG_DIR"] == str((home / "acct-b").resolve())


def test_a_side_questions_cli_like_its_sessions(home):
    from claude_agent_sdk import ClaudeAgentOptions
    account.adopt_from_environment()
    cfg = dataclasses.replace(parse_args([]), config_dir=str(home / ".claude"))
    opts = build_btw_options(ClaudeAgentOptions, config=cfg, state=State())
    assert "CLAUDE_CONFIG_DIR" not in opts.env
    cfg = dataclasses.replace(cfg, config_dir=str(home / "acct-b"))
    opts = build_btw_options(ClaudeAgentOptions, config=cfg, state=State())
    assert opts.env["CLAUDE_CONFIG_DIR"] == str((home / "acct-b").resolve())


def test_a_sign_in_on_the_default_account_named_outright(home):
    account.adopt_from_environment()
    assert "CLAUDE_CONFIG_DIR" not in auth._auth_env(str(home / ".claude"))
    assert auth._auth_env(str(home / "acct-b"))["CLAUDE_CONFIG_DIR"] == str(
        (home / "acct-b").resolve())


def test_a_sign_in_on_the_hubs_account(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    account.adopt_from_environment()
    assert auth._auth_env(None)["CLAUDE_CONFIG_DIR"] == str((home / "acct-b").resolve())


# --------------------------------------------------------------------------
# Processes of ours that take the account over
# --------------------------------------------------------------------------

def test_a_detached_child_is_handed_the_account(home, monkeypatch):
    import server
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    given = account.adopt_from_environment()
    cfg = dataclasses.replace(parse_args([]), config_dir=given)
    assert server._detach_child_env(cfg)["CLAUDE_CONFIG_DIR"] == given


def test_a_detached_child_on_the_default_account_is_handed_none(home):
    import server
    account.adopt_from_environment()
    cfg = dataclasses.replace(parse_args([]), config_dir=str(home / ".claude"))
    assert "CLAUDE_CONFIG_DIR" not in server._detach_child_env(cfg)


# --------------------------------------------------------------------------
# Where the default account's details are read
# --------------------------------------------------------------------------

def test_the_default_account_named_outright_is_read_where_its_cli_keeps_it(home):
    """Beside ~/.claude, as for a plain ``claude``: a copy inside it is what
    a CLI told ``CLAUDE_CONFIG_DIR=~/.claude`` once wrote, and none is now."""
    (home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "me@example.com"}}), encoding="utf-8")
    (home / ".claude" / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "stale@example.com"}}), encoding="utf-8")
    assert global_config_path(str(home / ".claude")) == home / ".claude.json"
    assert detect_account_info(str(home / ".claude"))["email"] == "me@example.com"


def test_another_account_is_read_inside_its_dir(home):
    assert global_config_path(str(home / "acct-b")) == (home / "acct-b").resolve() / ".claude.json"
