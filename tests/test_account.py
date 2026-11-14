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
    assert account.normalize(str(home / "acct-b")) == str(home / "acct-b")


def test_a_cli_on_the_default_account_finds_the_variable_unset(home):
    env = account.child_env(str(home / ".claude"),
                            base={"CLAUDE_CONFIG_DIR": str(home / ".claude"), "X": "1"})
    assert "CLAUDE_CONFIG_DIR" not in env and env["X"] == "1"


def test_a_cli_on_another_account_is_given_it(home):
    env = account.child_env(str(home / "acct-b"), base={})
    assert env["CLAUDE_CONFIG_DIR"] == str(home / "acct-b")


def test_before_adoption_the_environment_is_the_account(home, monkeypatch):
    # A tool, or copy_session run on its own.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    assert account.cli_config_dir(None) == str(home / "acct-b")
    assert account.account_dir() == home / "acct-b"


def test_a_session_without_its_own_account_has_the_hubs(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    account.adopt_from_environment()
    assert account.cli_config_dir(None) == str(home / "acct-b")
    assert account.child_env(None, base={})["CLAUDE_CONFIG_DIR"] == account.cli_config_dir(None)


# --------------------------------------------------------------------------
# The hub keeps none of its own
# --------------------------------------------------------------------------

def test_the_hub_takes_its_account_out_of_its_environment(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    given = account.adopt_from_environment()
    assert "CLAUDE_CONFIG_DIR" not in os.environ
    assert given == str(home / "acct-b")
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
    assert _bridge_env(str(home / "acct-b"))["CLAUDE_CONFIG_DIR"] == str(home / "acct-b")


def test_a_sessions_cli_on_the_hubs_account(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    account.adopt_from_environment()
    assert _bridge_env(None)["CLAUDE_CONFIG_DIR"] == str(home / "acct-b")


def test_a_side_questions_cli_like_its_sessions(home):
    from claude_agent_sdk import ClaudeAgentOptions
    account.adopt_from_environment()
    cfg = dataclasses.replace(parse_args([]), config_dir=str(home / ".claude"))
    opts = build_btw_options(ClaudeAgentOptions, config=cfg, state=State())
    assert "CLAUDE_CONFIG_DIR" not in opts.env
    cfg = dataclasses.replace(cfg, config_dir=str(home / "acct-b"))
    opts = build_btw_options(ClaudeAgentOptions, config=cfg, state=State())
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(home / "acct-b")


def _sign_in_env(monkeypatch, config_dir):
    """The environment ``claude auth status`` is run with for *config_dir*."""
    import subprocess
    seen = {}

    def fake_run(args, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(auth, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(subprocess, "run", fake_run)
    auth.auth_status(config_dir)
    return seen["env"]


def test_a_sign_in_on_the_default_account_named_outright(home, monkeypatch):
    account.adopt_from_environment()
    assert "CLAUDE_CONFIG_DIR" not in _sign_in_env(monkeypatch, str(home / ".claude"))
    assert _sign_in_env(monkeypatch, str(home / "acct-b"))["CLAUDE_CONFIG_DIR"] == str(
        home / "acct-b")


def test_a_sign_in_on_the_hubs_account(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    account.adopt_from_environment()
    assert _sign_in_env(monkeypatch, None)["CLAUDE_CONFIG_DIR"] == str(home / "acct-b")


def test_a_relative_session_account_reaches_a_child_whole(home, monkeypatch):
    """A child runs in another directory: a relative account is made absolute
    before it is handed on."""
    monkeypatch.chdir(home)
    assert account.normalize("acct-b") == str((home / "acct-b").absolute())


def test_an_account_written_with_a_tilde_is_the_home_one(home, monkeypatch):
    # expanduser reads the environment, not Path.home.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert account.account_dir("~/acct-b") == home / "acct-b"


# --------------------------------------------------------------------------
# A relative account is refused at launch
# --------------------------------------------------------------------------
#
# Read against the launch's directory, CLAUDE_CONFIG_DIR=.claude in a project
# names the project's own settings folder, which would become an account.  A
# hub's CLIs run in many directories, so no relative account is meant.

@pytest.fixture
def project(home, monkeypatch):
    """A project directory with its own settings folder, launched from."""
    proj = home / "work" / "forward raytracer"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(proj)
    return proj


@pytest.mark.parametrize("value", [".claude", r"sub\acct", r"\.claude"])
def test_a_relative_account_from_the_environment_is_refused(project, monkeypatch, value):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    with pytest.raises(ValueError) as exc:
        account.adopt_from_environment()
    message = str(exc.value)
    assert f"CLAUDE_CONFIG_DIR is {value}, a relative path" in message
    assert "'set CLAUDE_CONFIG_DIR=' in cmd" in message
    assert "'Remove-Item Env:CLAUDE_CONFIG_DIR' in PowerShell" in message
    assert "CLAUDE_CONFIG_DIR" not in os.environ, "a CLI could still inherit it"


def test_a_relative_config_dir_is_refused(project):
    with pytest.raises(ValueError) as exc:
        account.adopt_from_environment(".claude")
    message = str(exc.value)
    assert message.startswith("--config-dir is .claude, a relative path")
    assert "Remove-Item" not in message, "how to clear a variable nobody set"


def test_the_refusal_names_the_folder_and_the_account_meant(project, home):
    with pytest.raises(ValueError) as exc:
        account.adopt_from_environment(".claude")
    message = str(exc.value)
    assert (f"that is {project / '.claude'}, which is not a Claude account and "
            "would become a new, empty one.") in message
    assert f"Did you mean {home / '.claude'}?" in message


def test_a_root_relative_account_suggests_the_home_one(project, home):
    # \.claude is the drive's root, not the home directory.
    with pytest.raises(ValueError) as exc:
        account.adopt_from_environment(r"\.claude")
    assert f"Did you mean {home / '.claude'}?" in str(exc.value)


def test_a_relative_path_to_an_existing_account_says_it_is_one(home, monkeypatch):
    # From the home directory, .claude-account-b is the real account: the
    # refusal must not call it empty, nor suggest the same folder back.
    (home / "acct-b" / "projects").mkdir()
    monkeypatch.chdir(home)
    with pytest.raises(ValueError) as exc:
        account.adopt_from_environment("acct-b")
    message = str(exc.value)
    assert "an account, but from another directory" in message
    assert "new, empty" not in message
    assert "Did you mean" not in message


def test_no_account_is_suggested_when_home_has_none_of_that_name(project):
    with pytest.raises(ValueError) as exc:
        account.adopt_from_environment(".claude-account-z")
    assert "Did you mean" not in str(exc.value)


@pytest.mark.parametrize("value", ['"C:\\Users\\me\\.claude-b"', "'C:\\x'"])
def test_an_account_in_quotes_is_refused_for_the_quotes(project, monkeypatch, value):
    # cmd's  set CLAUDE_CONFIG_DIR="C:\..."  keeps the quotes.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    with pytest.raises(ValueError) as exc:
        account.adopt_from_environment()
    message = str(exc.value)
    assert "quotes included" in message and "relative" not in message
    assert 'set "CLAUDE_CONFIG_DIR=C:\\path"' in message


def test_an_account_given_whole_is_still_taken(project, home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    assert account.adopt_from_environment() == str((home / "acct-b").resolve())


def test_an_account_given_with_a_tilde_is_taken_from_home(project, home, monkeypatch):
    # expanduser reads the environment, not Path.home.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert account.adopt_from_environment("~/acct-b") == str((home / "acct-b").resolve())


def test_a_launch_given_a_relative_account_stops_before_anything_starts(project, monkeypatch):
    import server
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", ".claude")
    # main() takes these out of the environment, and rebinds its config.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(server.HANDOVER_KEYS_ENV, raising=False)
    monkeypatch.setattr(server, "config", None)
    monkeypatch.setattr(server, "_session_api_keys", {})
    stopped = []

    def fake_stop(message, *, dialog):
        stopped.append(message)
        raise SystemExit(2)

    def went_on(_cfg):
        # The next thing main() does.  Past it a launch joins whatever hub is
        # listening on 8420 -- the operator's own, when it is run there.
        raise AssertionError("the launch went on past its account")

    monkeypatch.setattr(server, "_stop_launch", fake_stop)
    monkeypatch.setattr(server, "_launch_picker_mode", went_on)
    monkeypatch.setattr(sys, "argv", ["server.py", "--cwd", str(project)])
    with pytest.raises(SystemExit):
        server.main()
    assert stopped and "a relative path" in stopped[0]


# --------------------------------------------------------------------------
# How an account is named to a person
# --------------------------------------------------------------------------

def test_an_account_in_home_is_named_by_its_folder(home):
    assert account.account_label(str(home / "acct-b")) == "acct-b"
    assert account.account_label(None) == ".claude"


def test_an_account_elsewhere_is_named_by_its_path(home):
    elsewhere = home / "work" / "proj" / ".claude"
    assert account.account_label(str(elsewhere)) == str(elsewhere)


def test_a_home_reached_another_way_is_still_home(home, monkeypatch):
    # The listing's paths are resolved and Path.home() may not be: a
    # junction, a short name.  Both sides are resolved before comparing.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home / "." / "work" / ".."))
    assert account.account_label(str(home.resolve() / "acct-b")) == "acct-b"


def test_where_a_move_went_names_an_account_elsewhere_by_its_path(home):
    import server
    elsewhere = home / "work" / "proj" / ".claude"
    assert server._move_where(None, str(elsewhere)) == f"into the {elsewhere} account"
    assert server._move_where(None, str(home / "acct-b")) == "into the acct-b account"


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


def _picker_env(monkeypatch):
    import subprocess
    import server
    seen = {}

    class _Proc:
        def wait(self):
            return 0

    def fake_popen(args, **kw):
        seen["env"] = kw.get("env")
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "_console_is_visible", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    server._run_launch_picker("resume")
    return seen["env"]


def test_the_launch_picker_is_handed_the_account(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    given = account.adopt_from_environment()
    assert _picker_env(monkeypatch)["CLAUDE_CONFIG_DIR"] == given


def _restart_env(monkeypatch, cfg):
    import asyncio
    import subprocess
    import server
    seen = {}

    def fake_popen(args, **kw):
        seen["env"] = kw.get("env")
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["server.py"])
    monkeypatch.setattr(server, "config", cfg)
    st = State()
    st.session_id = "sid-1"
    monkeypatch.setattr(server, "state", st)
    monkeypatch.setattr(server, "_picker_mode", False)
    asyncio.run(server.api_restart())
    return seen["env"]


def test_the_restart_replacement_is_handed_the_account(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "acct-b"))
    given = account.adopt_from_environment()
    cfg = dataclasses.replace(parse_args([]), config_dir=given)
    assert _restart_env(monkeypatch, cfg)["CLAUDE_CONFIG_DIR"] == given


def test_a_restart_hands_over_the_account_resolved_not_as_typed(home, monkeypatch):
    # A --config-dir typed relative before relative ones were refused would
    # stop the replacement; and the flag beats the environment the account
    # is handed over in.
    import asyncio
    import subprocess
    import server
    seen = {}

    def fake_popen(args, **kw):
        seen["args"], seen["env"] = args, kw.get("env")
        raise OSError("not really")

    given = str((home / "acct-b").resolve())
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", ["server.py", "--config-dir", "acct-b",
                                      "--config-dir=acct-b", "--effort", "high"])
    monkeypatch.setattr(server, "config",
                        dataclasses.replace(parse_args([]), config_dir=given))
    st = State()
    st.session_id = "sid-1"
    monkeypatch.setattr(server, "state", st)
    monkeypatch.setattr(server, "_picker_mode", False)
    account.adopt_from_environment(given)
    asyncio.run(server.api_restart())
    assert not any(a.startswith("--config-dir") for a in seen["args"]), seen["args"]
    assert "--effort" in seen["args"]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == given


def test_move_knows_the_default_account_by_either_name(home, monkeypatch):
    """A hub started plainly has no config_dir; /move lists ~/.claude by
    name.  They are one account."""
    import copy_session
    import server
    monkeypatch.setattr(copy_session, "discover_claude_dirs",
                        lambda: [home / ".claude", home / "acct-b"])
    monkeypatch.setattr(server, "_account_email_for", lambda _d: None)
    account.adopt_from_environment()
    current = {a["name"]: a["is_current"] for a in server._move_accounts_payload(None)}
    assert current == {".claude": True, "acct-b": False}
    assert account.same_account(None, str(home / ".claude"))
    assert not account.same_account(None, str(home / "acct-b"))


def test_move_tells_an_account_outside_home_from_the_one_of_its_name(home, monkeypatch):
    """The hub's own account, when it was started on one elsewhere, is
    offered too -- and may well be called .claude."""
    import copy_session
    import server
    elsewhere = home / "work" / "proj" / ".claude"
    monkeypatch.setattr(copy_session, "discover_claude_dirs",
                        lambda: [home / ".claude", elsewhere, home / "acct-b"])
    monkeypatch.setattr(server, "_account_email_for", lambda _d: None)
    names = [a["name"] for a in server._move_accounts_payload(None)]
    assert names == [".claude", str(elsewhere), "acct-b"]


def test_a_restart_on_the_default_account_hands_over_none(home, monkeypatch):
    given = account.adopt_from_environment(str(home / ".claude"))
    cfg = dataclasses.replace(parse_args([]), config_dir=given)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    assert "CLAUDE_CONFIG_DIR" not in _restart_env(monkeypatch, cfg)


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
    assert global_config_path(str(home / "acct-b")) == home / "acct-b" / ".claude.json"
