"""``--resume`` is settled before a launch starts or joins anything.

Reported 2026-09-27:

    i tried to --resume "OS F" and it said there were two of that name. so i
    picked the old one and renamed it "OS F old". then i tried to --resume
    "OS F" again and i got the same message again. so i checked sessions
    again. and it lists two "OS F"s again

    i tried `D:\\visual studio projects\\orchestrator2>py server.py --resume
    "OS F old 2"`, it said `Joined running orchestrator2 hub on port 8420
    (session s12).`, and nothing ever loaded in a new tab.

There were *three* sessions called "OS F": copies of one transcript, two on the
default account and one on account-c.  ``--resume "OS F"`` resolves in the
launch directory on the launch's account only, so it meant two of them, and
nothing said which.  The one renamed first was the third.

The second launch ran from the orchestrator2 checkout.  "OS F old 2" is in the
Slate OS directory, and a title resolves only in the launch's own directory,
so the hub opened a session that could never connect.  It said so only inside
that session, and without ``--open`` no tab was opened; the launch printed
"(session s12)" and nothing that led to it.

Now the launch settles ``--resume`` first (``server._settle_launch_resume``).
It follows a session it can find to its directory and account, whatever
``--cwd`` or account it was given; between several of one name, its account
and then its directory choose.  Otherwise it stops with an error naming each
candidate: id, last activity, directory, account.  A launch that joins a hub
prints the session's address.
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

import copy_session                                      # noqa: E402
import server                                            # noqa: E402
from config import parse_args                            # noqa: E402
from session import _sanitize_cwd                        # noqa: E402
from state import State                                  # noqa: E402

LIVE = "13d470f2-3b2b-4ced-b1f8-70c2dcf29025"
IDLE = "c2b5d5b6-cc09-41fd-b177-ddb97accdb09"
MOVED = "87a51d5c-e28b-44ca-9dbf-9b60adee3a7c"
OTHER = "796d6df2-0000-4000-8000-000000000004"


def _session(account: Path, cwd: str, sid: str, title=None, mtime=None) -> Path:
    project = account / "projects" / _sanitize_cwd(str(Path(cwd).resolve(strict=False)))
    project.mkdir(parents=True, exist_ok=True)
    recs = [{"type": "user", "sessionId": sid, "cwd": str(cwd),
             "message": {"role": "user", "content": "hi"}}]
    if title:
        recs.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    path = project / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Two accounts and two directories, and nothing else on this "machine"."""
    home = tmp_path / "home"
    default = home / ".claude"
    other = home / ".claude-work"
    for acct in (default, other):
        (acct / "projects").mkdir(parents=True)
    os_dir = tmp_path / "os"
    orch = tmp_path / "orchestrator2"
    os_dir.mkdir()
    orch.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(copy_session, "discover_claude_dirs",
                        lambda: [default, other])
    # conftest puts the variable back after a main() pops it.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return types.SimpleNamespace(default=default, other=other,
                                 os=str(os_dir), orch=str(orch))


def _cfg(m, cwd, resume, *, cwd_given=False, config_dir=None):
    return dataclasses.replace(parse_args([]), cwd=cwd, cwd_given=cwd_given,
                               resume=resume, config_dir=config_dir)


def _settle(cfg):
    return server._settle_launch_resume(cfg)


# --------------------------------------------------------------------------
# The flag itself
# --------------------------------------------------------------------------

def test_a_launch_knows_whether_it_was_given_a_directory():
    assert parse_args([]).cwd_given is False
    assert parse_args(["--cwd", "."]).cwd_given is True
    assert parse_args(["--cwd=."]).cwd_given is True
    assert parse_args([]).cwd == parse_args(["--cwd", "."]).cwd


# --------------------------------------------------------------------------
# A title that names one session
# --------------------------------------------------------------------------

def test_a_title_in_the_launch_directory_is_that_session(machine):
    _session(machine.default, machine.os, LIVE, "OS F")

    cfg, note, err = _settle(_cfg(machine, machine.os, "OS F"))

    assert err is None and note is None
    assert cfg.resume == LIVE and cfg.cwd == machine.os


def test_the_one_in_the_launch_directory_wins(machine):
    """The CLI's rule: a title means the session *here* that has it, whatever
    else carries the same name."""
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.other, machine.orch, MOVED, "OS F")

    cfg, note, err = _settle(_cfg(machine, machine.os, "OS F"))

    assert (cfg.resume, cfg.cwd, note, err) == (LIVE, machine.os, None, None)


def test_a_title_in_the_launch_directory_searches_no_further(machine, monkeypatch):
    """Every account's sessions are scanned only when the launch's own
    directory, on its own account, has none of that name (half a second)."""
    _session(machine.default, machine.os, LIVE, "OS F")

    def scanned():
        raise AssertionError("searched the whole machine")

    monkeypatch.setattr(server, "sessions_anywhere", scanned)
    cfg, _note, err = _settle(_cfg(machine, machine.os, "OS F"))

    assert err is None and cfg.resume == LIVE


def test_a_title_elsewhere_matches_in_any_case(machine):
    _session(machine.default, machine.os, IDLE, "OS F old 2")

    cfg, _note, err = _settle(_cfg(machine, machine.orch, "os f OLD 2"))

    assert err is None and cfg.resume == IDLE


def test_a_title_in_another_directory_is_followed_there(machine):
    """The report's second launch, which "joined" and then never loaded."""
    _session(machine.default, machine.os, IDLE, "OS F old 2")

    cfg, note, err = _settle(_cfg(machine, machine.orch, "OS F old 2"))

    assert err is None
    assert cfg.resume == IDLE
    assert cfg.cwd == str(Path(machine.os).resolve())
    assert "OS F old 2" in note and machine.os in note


def test_and_from_a_directory_given_too(machine):
    """A launcher that passes --cwd every time.  The CLI resumes a session
    only in its own directory, so the --cwd could only have refused it."""
    _session(machine.default, machine.os, IDLE, "OS F old 2")

    cfg, note, err = _settle(_cfg(machine, machine.orch, "OS F old 2", cwd_given=True))

    assert err is None
    assert cfg.resume == IDLE
    assert cfg.cwd == str(Path(machine.os).resolve())
    assert "OS F old 2" in note and machine.os in note


def test_a_title_on_another_account_is_followed_there(machine):
    _session(machine.other, machine.os, MOVED, "OS F old")

    cfg, note, err = _settle(_cfg(machine, machine.os, "OS F old"))

    assert err is None
    assert cfg.resume == MOVED and cfg.config_dir == str(machine.other)
    assert ".claude-work" in note


def test_and_from_an_account_chosen_too(machine):
    """Its transcript is in its own account's tree, so the one chosen could
    only have refused it."""
    _session(machine.other, machine.os, MOVED, "OS F old")

    cfg, note, err = _settle(_cfg(machine, machine.os, "OS F old",
                                  config_dir=str(machine.default)))

    assert err is None
    assert cfg.resume == MOVED and cfg.config_dir == str(machine.other)
    assert ".claude-work" in note


def test_and_from_an_account_the_environment_chose(machine, monkeypatch, tmp_path):
    """main() adopts the inherited account into the launch's Config, as if
    it had been given with --config-dir."""
    _session(machine.other, machine.os, MOVED, "OS F old")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(machine.default))

    reached, _log = _main(monkeypatch, _cfg(machine, machine.os, "OS F old"), tmp_path)

    assert not isinstance(reached, SystemExit), "it stopped"
    assert server.config.resume == MOVED
    assert server.config.config_dir == str(machine.other)


def test_the_environments_account_picks_between_several(machine, monkeypatch,
                                                         tmp_path):
    """Inherited, it is the launch's account all the same: a shell set up
    for one account resumes that account's session of a shared name."""
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(machine.other))

    reached, _log = _main(monkeypatch, _cfg(machine, machine.orch, "OS F"), tmp_path)

    assert not isinstance(reached, SystemExit), "it stopped"
    assert server.config.resume == MOVED


def test_one_session_under_two_accounts_is_still_one(machine, tmp_path):
    """The same transcript, id and all, in both accounts' trees (a file copied
    by hand) is one session to follow, not "several".  Launched on a third
    account, so neither copy is preferred as the launch's own."""
    _session(machine.default, machine.os, IDLE, "OS F old 2")
    _session(machine.other, machine.os, IDLE, "OS F old 2")
    third = tmp_path / "home" / ".claude-third"
    (third / "projects").mkdir(parents=True)

    cfg, _note, err = _settle(_cfg(machine, machine.orch, "OS F old 2",
                                   config_dir=str(third)))

    assert err is None and cfg.resume == IDLE


def test_a_session_whose_directory_is_gone_is_not_followed(machine, tmp_path):
    gone = str(tmp_path / "deleted")
    _session(machine.default, gone, IDLE, "OS F old 2")

    _cfg_, _note, err = _settle(_cfg(machine, machine.orch, "OS F old 2"))

    assert err is not None and "does not exist" in err


# --------------------------------------------------------------------------
# A title that names several
# --------------------------------------------------------------------------

def test_several_here_are_named_with_when_each_was_last_active(machine):
    """The report's first half: "it said there were two of that name"."""
    _session(machine.default, machine.os, LIVE, "OS F", mtime=1_790_000_000)
    _session(machine.default, machine.os, IDLE, "OS F", mtime=1_789_900_000)

    cfg, note, err = _settle(_cfg(machine, machine.os, "OS F"))

    assert note is None and cfg.resume == "OS F"
    assert "More than one session" in err and "so none was resumed" in err
    assert LIVE in err and IDLE in err
    assert err.count("last active") == 2
    assert err.index(LIVE) < err.index(IDLE), "not newest first"
    assert "--resume <id>" in err


def test_and_one_on_another_account_is_said_not_to_be_one_of_them(machine):
    """The one the user renamed first: a third "OS F", on account-c, that the
    launch's "two of that name" never meant."""
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.default, machine.os, IDLE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")

    _cfg_, _note, err = _settle(_cfg(machine, machine.os, "OS F"))

    head, _sep, tail = err.partition("not one of those")
    assert LIVE in head and IDLE in head and MOVED not in head
    assert MOVED in tail and ".claude-work" in tail


def test_several_elsewhere_are_listed_not_guessed_between(machine):
    """Neither on the launch's account nor in its directory."""
    _session(machine.other, machine.os, LIVE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")

    cfg, _note, err = _settle(_cfg(machine, machine.orch, "OS F"))

    assert cfg.resume == "OS F"
    assert LIVE in err and MOVED in err and "--resume <id>" in err
    assert "--cwd" not in err and "--config-dir" not in err, \
        "an id is followed anywhere, so neither is needed"


def test_several_elsewhere_the_launchs_account_picks_one(machine):
    """Given or not: the account a launch runs on is the one it means."""
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")

    cfg, _note, err = _settle(_cfg(machine, machine.orch, "OS F"))

    assert err is None and cfg.resume == LIVE


@pytest.mark.parametrize("given", [True, False], ids=["--cwd", "no --cwd"])
def test_several_elsewhere_the_launchs_directory_picks_one(machine, tmp_path, given):
    """`--cwd .` and no --cwd are the same directory, and choose alike."""
    third = tmp_path / "third"
    third.mkdir()
    _session(machine.other, machine.os, LIVE, "OS F")
    _session(machine.other, str(third), MOVED, "OS F")

    cfg, _note, err = _settle(_cfg(machine, str(third), "OS F", cwd_given=given))

    assert err is None and cfg.resume == MOVED
    assert cfg.config_dir == str(machine.other)


def test_the_launchs_account_outranks_its_directory(machine, tmp_path):
    """A --cwd is often only where a launcher ran; an account is chosen."""
    third = tmp_path / "third"
    third.mkdir()
    _session(machine.default, str(third), LIVE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")

    cfg, _note, err = _settle(_cfg(machine, str(third), "OS F", cwd_given=True,
                                   config_dir=str(machine.other)))

    assert err is None and cfg.resume == MOVED


def test_a_list_of_several_names_only_those_still_in_play(machine, tmp_path):
    third = tmp_path / "third"
    third.mkdir()
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.default, str(third), IDLE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")

    _cfg_, _note, err = _settle(_cfg(machine, machine.orch, "OS F"))

    assert LIVE in err and IDLE in err and MOVED not in err
    assert "1 more on other accounts or in other directories" in err


def test_a_directory_given_with_none_of_them_leaves_the_account_to_pick(machine):
    """A launcher's --cwd is usually just where it was run: holding none of
    them, it must not keep the account from choosing."""
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")

    cfg, _note, err = _settle(_cfg(machine, machine.orch, "OS F", cwd_given=True,
                                   config_dir=str(machine.other)))

    assert err is None and cfg.resume == MOVED


def test_several_elsewhere_the_account_given_picks_one(machine):
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")

    cfg, _note, err = _settle(_cfg(machine, machine.orch, "OS F",
                                   config_dir=str(machine.other)))

    assert err is None and cfg.resume == MOVED
    assert cfg.cwd == str(Path(machine.os).resolve())


# --------------------------------------------------------------------------
# A title that names nothing
# --------------------------------------------------------------------------

def test_nothing_by_that_name_offers_titles_like_it(machine):
    """After the renames, "OS F" named nothing at all."""
    _session(machine.default, machine.os, LIVE, "OS F old 3")
    _session(machine.other, machine.os, MOVED, "OS F old")

    _cfg_, _note, err = _settle(_cfg(machine, machine.orch, "OS F"))

    assert "No session in" in err
    assert "'OS F old 3'" in err and "'OS F old'" in err
    assert LIVE in err and MOVED in err


def test_or_what_the_directory_has(machine):
    _session(machine.default, machine.os, OTHER, "OS E")

    _cfg_, _note, err = _settle(_cfg(machine, machine.os, "Lane Q"))

    assert "Sessions here: 'OS E' (796d6df2)" in err


def test_or_that_it_has_nothing(machine):
    _cfg_, _note, err = _settle(_cfg(machine, machine.os, "Lane Q"))

    assert "no sessions in that directory" in err


# --------------------------------------------------------------------------
# An id
# --------------------------------------------------------------------------

def test_an_id_in_another_directory_is_followed_there(machine):
    _session(machine.default, machine.os, IDLE, "OS F old 2")

    cfg, note, err = _settle(_cfg(machine, machine.orch, IDLE))

    assert err is None and cfg.resume == IDLE
    assert cfg.cwd == str(Path(machine.os).resolve()) and machine.os in note


def test_an_id_in_its_own_directory_is_left_alone(machine):
    _session(machine.default, machine.os, IDLE)

    cfg, note, err = _settle(_cfg(machine, machine.os, IDLE))

    assert (cfg.resume, cfg.cwd, note, err) == (IDLE, machine.os, None, None)


def test_an_id_is_followed_from_a_directory_given_too(machine):
    """The CLI looks for the transcript only under the launch directory, so
    the launch goes to the session's."""
    _session(machine.default, machine.os, IDLE)

    cfg, note, err = _settle(_cfg(machine, machine.orch, IDLE, cwd_given=True))

    assert err is None and cfg.resume == IDLE
    assert cfg.cwd == str(Path(machine.os).resolve()) and machine.os in note


def test_an_id_on_another_account_is_followed_there(machine):
    _session(machine.other, machine.os, MOVED)

    cfg, note, err = _settle(_cfg(machine, machine.os, MOVED))

    assert err is None and cfg.config_dir == str(machine.other)


def test_a_transcript_copied_into_the_launchs_directory_resumes_there(machine):
    """It still records the directory it was copied from, where the CLI
    would not find it."""
    copy = _session(machine.default, machine.orch, IDLE)
    copy.write_text(copy.read_text(encoding="utf-8").replace(
        json.dumps(str(machine.orch))[1:-1], json.dumps(machine.os)[1:-1]),
        encoding="utf-8")

    cfg, note, err = _settle(_cfg(machine, machine.orch, IDLE, cwd_given=True))

    assert (cfg.resume, cfg.cwd, note, err) == (IDLE, machine.orch, None, None)


def test_an_id_is_followed_from_an_account_chosen_too(machine):
    _session(machine.other, machine.os, MOVED)

    cfg, note, err = _settle(_cfg(machine, machine.os, MOVED,
                                  config_dir=str(machine.default)))

    assert err is None and cfg.config_dir == str(machine.other)
    assert ".claude-work" in note


def test_an_id_nobody_has_says_so(machine):
    _cfg_, _note, err = _settle(_cfg(machine, machine.os, MOVED))

    assert err == f"There is no session {MOVED} on this machine."


@pytest.mark.parametrize("resume", [None, "", server._PICKER_SENTINEL])
def test_nothing_to_resume_is_left_alone(machine, resume):
    cfg = _cfg(machine, machine.os, resume)

    assert _settle(cfg) == (cfg, None, None)


# --------------------------------------------------------------------------
# The launch
# --------------------------------------------------------------------------

def _main(monkeypatch, cfg, tmp_path):
    """Run main() up to where it would probe the hub."""
    log = tmp_path / "launch-error.log"
    monkeypatch.setattr(server, "config", None)       # main() rebinds it
    monkeypatch.setattr(server, "_parse_args_or_report", lambda: cfg)
    monkeypatch.setattr(server, "LAUNCH_ERROR_LOG", str(log))
    monkeypatch.setenv("ORCH2_NO_DIALOG", "1")

    class _Probed(Exception):
        pass

    def probe(port):
        raise _Probed(port)

    monkeypatch.setattr(server, "_probe_hub", probe)
    with pytest.raises((SystemExit, _Probed)) as ei:
        server.main()
    return ei.value, log


def test_a_launch_that_cannot_resume_stops_before_the_hub(machine, monkeypatch,
                                                          tmp_path, capsys):
    """It used to join the hub and report success."""
    stopped, log = _main(monkeypatch, _cfg(machine, machine.orch, "OS F"), tmp_path)

    assert isinstance(stopped, SystemExit) and stopped.code == 2
    assert "No session in" in capsys.readouterr().err
    assert "No session in" in log.read_text(encoding="utf-8")


def test_a_launch_that_follows_a_session_says_so_and_goes_on(machine, monkeypatch,
                                                             tmp_path, capsys):
    _session(machine.default, machine.os, IDLE, "OS F old 2")

    reached, _log = _main(monkeypatch, _cfg(machine, machine.orch, "OS F old 2"),
                          tmp_path)

    assert not isinstance(reached, SystemExit), "it stopped"
    assert "resuming it there" in capsys.readouterr().out
    assert server.config.resume == IDLE
    assert server.config.cwd == str(Path(machine.os).resolve())


def test_following_an_account_pins_it_for_the_launch(machine, monkeypatch, tmp_path):
    _session(machine.other, machine.os, MOVED, "OS F old")

    _main(monkeypatch, _cfg(machine, machine.os, "OS F old"), tmp_path)

    import account
    assert account.hub_account() == str(machine.other.resolve())
    assert server.config.config_dir == str(machine.other)
    # ...and the environment carries none, for no CLI to inherit.
    assert "CLAUDE_CONFIG_DIR" not in os.environ


def test_a_launch_joining_a_hub_hands_over_the_account_it_inherited(
        machine, monkeypatch, tmp_path):
    """From a shell set up for one account, the session opens on that account
    in the hub it joins, whatever account that hub works in."""
    _session(machine.other, machine.os, MOVED, "OS F old")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(machine.other))
    handed = []

    class _Joined(Exception):
        pass

    def launch(_port, **kw):
        handed.append(kw)
        raise _Joined

    monkeypatch.setattr(server, "config", None)       # main() rebinds it
    monkeypatch.setattr(server, "_parse_args_or_report",
                        lambda: _cfg(machine, machine.os, "OS F old"))
    monkeypatch.setattr(server, "LAUNCH_ERROR_LOG", str(tmp_path / "e.log"))
    monkeypatch.setenv("ORCH2_NO_DIALOG", "1")
    monkeypatch.setattr(server, "_probe_hub", lambda _port: {"pid": 1})
    monkeypatch.setattr(server, "_launch_into_hub", launch)
    with pytest.raises(_Joined):
        server.main()

    assert handed[0]["config_dir"] == str(machine.other)
    assert handed[0]["resume"] == MOVED


def test_joining_says_where_the_session_is(monkeypatch, capsys):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    server._announce_joined(8420, "s12", False)

    out = capsys.readouterr().out
    assert "http://localhost:8420/?rid=s12" in out
    assert "--open" in out and not opened


def test_and_opens_it_when_asked(monkeypatch, capsys):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    server._announce_joined(8420, "s12", True)

    assert len(opened) == 1 and opened[0].startswith("http://localhost:8420/?rid=s12")
    assert "--open" not in capsys.readouterr().out


def test_a_detached_child_is_given_the_account_it_followed():
    cfg = dataclasses.replace(parse_args([]), resume=MOVED,
                              config_dir=r"C:\h\.claude-work")
    argv = server._detach_child_argv(
        ["server.py", "--config-dir", r"C:\h\.claude", "--detach"], cfg, 8421)

    assert argv.count("--config-dir") == 1
    assert argv[argv.index("--config-dir") + 1] == r"C:\h\.claude-work"


def _restart_argv(monkeypatch, argv, sid, *, live=True, cwd=None):
    """The replacement's argv.  *live*: whether the primary session is still
    open (it may have been closed from the lobby); *cwd*: where it is now."""
    import subprocess
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", argv)
    cfg = parse_args([])
    monkeypatch.setattr(server, "config",
                        dataclasses.replace(cfg, cwd=cwd) if cwd else cfg)
    st = State()
    st.session_id = sid
    monkeypatch.setattr(server, "state", st if live else None)
    monkeypatch.setattr(server, "_picker_mode", False)
    asyncio.run(server.api_restart())
    return seen["argv"]


def test_a_restart_lets_the_session_it_resumes_choose_the_directory(monkeypatch):
    """After /cwd or /resume the launch's --cwd names the wrong directory, and
    a session is resumed only from its own."""
    argv = _restart_argv(monkeypatch, ["server.py", "--cwd", r"D:\a", "--open"], IDLE)

    assert "--cwd" not in argv and r"D:\a" not in argv
    assert argv[argv.index("--resume") + 1] == IDLE


def test_a_restart_with_no_session_keeps_the_directory(monkeypatch):
    # The primary was closed: the replacement continues where the launch did.
    argv = _restart_argv(monkeypatch, ["server.py", "--cwd", r"D:\a"], None,
                         live=False)

    assert argv[argv.index("--cwd") + 1] == r"D:\a"
    assert "--no-continue" not in argv


def test_a_replacement_does_not_pass_its_own_fresh_start_on(monkeypatch):
    # It was for that replacement's startup; the next restart decides anew.
    argv = _restart_argv(monkeypatch, ["server.py", "--start-fresh"], IDLE)

    assert "--start-fresh" not in argv
    assert argv[argv.index("--resume") + 1] == IDLE


def test_a_restart_of_a_new_conversation_starts_a_new_one_where_it_is(monkeypatch):
    """Not the most recent session there, which would swap the conversation
    for another, and not the launch's directory after /cwd moved it."""
    argv = _restart_argv(monkeypatch, ["server.py", "--cwd", r"D:\a"], None,
                         cwd=r"D:\b")

    assert "--start-fresh" in argv and "--resume" not in argv
    assert "--no-continue" not in argv
    assert argv.count("--cwd") == 1 and argv[argv.index("--cwd") + 1] == r"D:\b"


class _QuietBridge:
    def __init__(self, config, state, broadcaster):
        self.config = config
        self._initial_resume_id = None

    async def start(self):
        pass

    async def stop(self, **_kw):
        pass


def _resumed_from_the_orchestrator(machine, monkeypatch, sid, cwd=None):
    """The primary tab, in machine.orch, after /resume *sid* (and a cwd)."""
    import sdk_bridge
    monkeypatch.setattr(sdk_bridge, "SDKBridge", _QuietBridge)
    monkeypatch.setattr(server, "_attach_queue_persistence",
                        lambda st, cwd, config_dir=None: 0)
    monkeypatch.setattr(server, "config", dataclasses.replace(
        parse_args([]), cwd=machine.orch, config_dir=str(machine.default)))
    monkeypatch.setattr(server, "state", State())
    monkeypatch.setattr(server, "bridge", None)
    monkeypatch.setattr(server, "_default_runtime", None)
    monkeypatch.setattr(server, "_picker_mode", False)
    monkeypatch.chdir(machine.orch)
    ok, err = asyncio.run(server._reconfigure(resume=sid, cwd=cwd))
    assert ok, err
    return server.config


def test_a_session_resumed_from_another_directory_runs_there(machine, monkeypatch):
    """The CLI looks for a session only in its own directory's project, and
    is started in the session's Config cwd: chdir alone left it where it was."""
    _session(machine.default, machine.os, LIVE, "OS F")
    cfg = _resumed_from_the_orchestrator(machine, monkeypatch, LIVE)
    assert cfg.cwd == str(Path(machine.os).resolve())


def test_a_session_whose_directory_is_gone_resumes_where_the_tab_is(machine, monkeypatch,
                                                                     tmp_path):
    _session(machine.default, str(tmp_path / "deleted"), LIVE, "OS F")
    cfg = _resumed_from_the_orchestrator(machine, monkeypatch, LIVE)
    assert cfg.cwd == str(Path(machine.orch).resolve())


class _RunningBridge(_QuietBridge):
    stopped = False

    async def stop(self, **_kw):
        _RunningBridge.stopped = True


def _switched_from_a_tab_in(machine, monkeypatch, tab_cwd, **where):
    """``_reconfigure(**where)`` from a primary tab working in *tab_cwd*."""
    import sdk_bridge
    monkeypatch.setattr(sdk_bridge, "SDKBridge", _QuietBridge)
    monkeypatch.setattr(server, "_attach_queue_persistence",
                        lambda st, cwd, config_dir=None: 0)
    monkeypatch.setattr(server, "config", dataclasses.replace(
        parse_args([]), cwd=tab_cwd, config_dir=str(machine.default)))
    monkeypatch.setattr(server, "state", State())
    _RunningBridge.stopped = False
    monkeypatch.setattr(server, "bridge", _RunningBridge(None, None, None))
    monkeypatch.setattr(server, "_default_runtime", None)
    monkeypatch.setattr(server, "_picker_mode", False)
    monkeypatch.chdir(machine.orch)
    return asyncio.run(server._reconfigure(**where))


def test_a_resume_with_nowhere_to_run_leaves_the_tab_working(machine, monkeypatch,
                                                            tmp_path):
    """Neither the session's directory nor the tab's exists any more."""
    _session(machine.default, str(tmp_path / "deleted"), LIVE, "OS F")
    ok, err = _switched_from_a_tab_in(machine, monkeypatch,
                                      str(tmp_path / "also-deleted"), resume=LIVE)
    assert not ok and "also-deleted" in err
    assert not _RunningBridge.stopped


def test_cwd_to_a_file_is_refused(machine, monkeypatch, tmp_path):
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")
    ok, err = _switched_from_a_tab_in(machine, monkeypatch, machine.orch,
                                      cwd=str(a_file))
    assert not ok and "directory not found" in err
    assert not _RunningBridge.stopped


def test_a_directory_given_with_the_resume_is_kept(machine, monkeypatch):
    _session(machine.default, machine.os, LIVE, "OS F")
    cfg = _resumed_from_the_orchestrator(machine, monkeypatch, LIVE, cwd=machine.orch)
    assert cfg.cwd == str(Path(machine.orch).resolve())


def test_a_session_picked_from_another_directory_runs_there(machine, monkeypatch):
    # The picker lists every project's sessions, and picks by id.
    from session_runtime import SessionRuntime
    _session(machine.default, machine.os, LIVE, "OS F")

    class _Idle:
        def __init__(self):
            self.config = None
            self._initial_resume_id = None

        async def start(self):
            pass

    cfg = dataclasses.replace(parse_args([]), cwd=machine.orch,
                              config_dir=str(machine.default))
    st = State()
    rt = SessionRuntime(config=cfg, state=st, rid="R0")
    rt.bridge = _Idle()
    attached = []
    monkeypatch.setattr(server, "_attach_queue_persistence",
                        lambda s, cwd, config_dir=None: attached.append((s.session_id, cwd)) or 0)
    for name, value in (("config", cfg), ("state", st), ("bridge", rt.bridge),
                        ("_default_runtime", rt), ("_picker_mode", True)):
        monkeypatch.setattr(server, name, value)
    monkeypatch.chdir(machine.orch)
    out = asyncio.run(server.api_resume({"session_id": LIVE}))
    assert out["ok"], out
    here = str(Path(machine.os).resolve())
    assert server.config.cwd == here and rt.bridge.config.cwd == here
    # ...and its queue, kept for that session in that directory, comes back.
    assert attached == [(LIVE, here)]


def test_resume_inside_a_session_names_the_ones_that_share_a_title(machine, monkeypatch):
    """"session not found" was the answer to a title two sessions share."""
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.default, machine.os, IDLE, "OS F")
    monkeypatch.setattr(server, "config", dataclasses.replace(
        parse_args([]), cwd=machine.os, config_dir=str(machine.default)))
    monkeypatch.setattr(server, "bridge", None)

    ok, err = asyncio.run(server._reconfigure(resume="OS F"))

    assert not ok and "2 sessions here are called 'OS F'" in err
    assert LIVE in err and IDLE in err


# --------------------------------------------------------------------------
# /resume in a tab: any directory, any account
#
# Asked 2026-10-21: "add a command to resume a session from any directory, not
# just the one you're currently in. maybe also be able to resume from any
# claude account".  --resume already did both; /resume worked only in the
# hub's own session, only on its account, and found a title only in the
# directory it was in -- from any other tab it said "Use Sessions".
# --------------------------------------------------------------------------

def _tab(machine, monkeypatch, *, primary, cwd=None, live=(), account=None,
         holder=None):
    """A tab on a session in *cwd* (machine.orch), and what /resume does from
    it: messages sent, runtimes attached, opened, and in-place switches."""
    from session_runtime import SessionRuntime
    import proc_guard
    rt = SessionRuntime(config=dataclasses.replace(parse_args([]), cwd=cwd or machine.orch,
                                                   config_dir=account),
                        state=State(), rid="R1")
    rt.state.session_id = "s-current"
    got = types.SimpleNamespace(sent=[], attached=[], opened=[], switched=[])

    async def send_to(_ws, m):
        got.sent.append(m)

    async def attach(_ws, target):
        got.attached.append(target)
        got.sent.append({"type": "_attached"})

    async def create(**kw):
        got.opened.append(kw)
        new = SessionRuntime(config=parse_args([]), state=State(), rid="R9")
        new.state.session_id = kw.get("resume")
        return new

    async def reconfigure(**kw):
        got.switched.append(kw)
        return True, None

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(server, "send_to", send_to)
    monkeypatch.setattr(server, "_attach_ws", attach)
    monkeypatch.setattr(server, "_create_runtime", create)
    monkeypatch.setattr(server, "_reconfigure", reconfigure)
    monkeypatch.setattr(server, "_send_initial_state", nothing)
    monkeypatch.setattr(server, "runtimes",
                        {r.rid: r for r in (rt, *live)})
    monkeypatch.setattr(server, "_default_runtime", rt if primary else None)
    monkeypatch.setattr(proc_guard, "find_foreign_session_holder", lambda _sid: holder)
    return rt, got


def _resume(rt, ref):
    asyncio.run(server._handle_resume(object(), rt, ref))


def _said(got):
    return " | ".join(m.get("data", {}).get("message", "") for m in got.sent)


def test_resume_from_any_tab_finds_a_title_in_another_directory(machine, monkeypatch):
    _session(machine.default, machine.os, LIVE, "OS F")
    rt, got = _tab(machine, monkeypatch, primary=False)
    _resume(rt, "OS F")
    assert [(o["resume"], o["config_dir"]) for o in got.opened] ==         [(LIVE, str(machine.default))]
    assert Path(got.opened[0]["cwd"]).resolve() == Path(machine.os).resolve()
    assert [a.state.session_id for a in got.attached] == [LIVE]
    assert "Use Sessions" not in _said(got)


def test_resume_follows_a_session_into_another_account(machine, monkeypatch):
    _session(machine.other, machine.os, OTHER, "Work thing")
    rt, got = _tab(machine, monkeypatch, primary=False)
    _resume(rt, OTHER)
    assert [(o["resume"], o["config_dir"]) for o in got.opened] == \
        [(OTHER, str(machine.other))]
    assert "on account .claude-work" in _said(got)


def test_resume_in_the_hubs_own_session_still_switches_in_place(machine, monkeypatch):
    _session(machine.default, machine.os, LIVE, "OS F")
    rt, got = _tab(machine, monkeypatch, primary=True)
    _resume(rt, "OS F")
    assert got.switched == [{"resume": LIVE}] and got.opened == []


def test_resume_in_the_hubs_session_of_another_accounts_opens_it(machine, monkeypatch):
    """The hub's own session runs on the hub's account; a session on another
    is opened alongside, as the session list would."""
    _session(machine.other, machine.os, OTHER, "Work thing")
    rt, got = _tab(machine, monkeypatch, primary=True)
    _resume(rt, "Work thing")
    assert got.switched == []
    assert [o["config_dir"] for o in got.opened] == [str(machine.other)]


def test_resume_prefers_the_tabs_account_among_several(machine, monkeypatch):
    """As a launch prefers its own."""
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.other, machine.os, OTHER, "OS F")
    rt, got = _tab(machine, monkeypatch, primary=False)
    _resume(rt, "OS F")
    assert [o["resume"] for o in got.opened] == [LIVE]


def test_resume_names_the_sessions_sharing_a_title(machine, monkeypatch):
    _session(machine.other, machine.os, LIVE, "OS F")
    _session(machine.other, machine.os, OTHER, "OS F")
    rt, got = _tab(machine, monkeypatch, primary=False)
    _resume(rt, "OS F")
    said = _said(got)
    assert got.opened == [] and got.attached == []
    assert LIVE in said and OTHER in said and "/resume <id>" in said
    assert "--cwd" not in said, "an id is followed anywhere; no flag is needed"


def test_resume_goes_to_a_session_that_is_open_already(machine, monkeypatch):
    from session_runtime import SessionRuntime
    _session(machine.default, machine.os, LIVE, "OS F")
    open_one = SessionRuntime(config=parse_args([]), state=State(), rid="R5")
    open_one.state.session_id = LIVE
    rt, got = _tab(machine, monkeypatch, primary=True, live=[open_one])
    _resume(rt, LIVE)
    assert got.attached == [open_one]
    assert got.opened == [] and got.switched == []


def test_resume_of_the_tabs_own_session_says_so(machine, monkeypatch):
    _session(machine.default, machine.orch, "s-current", "Here")
    rt, got = _tab(machine, monkeypatch, primary=False)
    _resume(rt, "Here")
    assert "this tab is on" in _said(got)
    assert got.opened == [] and got.attached == []


def test_resume_of_nothing_says_what_there_is(machine, monkeypatch):
    rt, got = _tab(machine, monkeypatch, primary=False)
    _resume(rt, "No such thing")
    assert "Resume failed" in _said(got) and got.opened == []


def test_the_lobby_opens_through_the_same_helper(machine, monkeypatch):
    """The session list's open and /resume do one thing, in one place."""
    _session(machine.other, machine.os, OTHER, "Work thing")
    rt, got = _tab(machine, monkeypatch, primary=False)
    asyncio.run(server._handle_lobby_message(
        object(), {"type": "open", "session_id": OTHER,
                   "account": str(machine.other)}))
    assert [(o["resume"], o["config_dir"]) for o in got.opened] == \
        [(OTHER, str(machine.other))]


def test_a_secondary_tabs_resume_reaches_the_command(machine, monkeypatch):
    """It used to stop at "Use Sessions to open a different folder or
    session." before looking for anything."""
    _session(machine.default, machine.os, LIVE, "OS F")
    rt, got = _tab(machine, monkeypatch, primary=False)
    rt.bridge = object()
    ws = object()
    monkeypatch.setitem(server._ws_runtime, ws, rt)

    async def ack(*_a, **_k):
        pass

    monkeypatch.setattr(server, "_ack_prompt", ack)
    asyncio.run(server._handle_ws_message(ws, {"type": "message",
                                               "text": "/resume OS F"}))
    assert [o["resume"] for o in got.opened] == [LIVE]


def test_where_it_was_found_is_said_once_the_tab_shows_it(machine, monkeypatch):
    """Review: said first, it was wiped by the clear that showing a session
    begins with."""
    _session(machine.other, machine.os, OTHER, "Work thing")
    rt, got = _tab(machine, monkeypatch, primary=False)
    _resume(rt, "Work thing")
    kinds = [m.get("type") for m in got.sent]
    note = next(i for i, m in enumerate(got.sent)
                if "on account .claude-work" in m.get("data", {}).get("message", ""))
    assert kinds.index("_attached") < note


def test_the_hubs_tab_never_switches_to_a_session_held_elsewhere(machine, monkeypatch):
    """Review: in place, the working session was stopped for one that could
    only park on DuplicateSessionError."""
    _session(machine.default, machine.os, LIVE, "OS F")
    held = types.SimpleNamespace(holder_pid=4242, started=0.0, port=None)
    rt, got = _tab(machine, monkeypatch, primary=True, holder=held)
    _resume(rt, "OS F")
    assert got.switched == [] and got.opened == []
    assert [m["session_id"] for m in got.sent if m.get("type") == "session_elsewhere"] == [LIVE]


def test_a_title_is_looked_for_in_the_tabs_own_account_first(machine, monkeypatch):
    """Review: the "here" lookup used the hub's account, so a tab on another
    account was answered with a list, or the hub's session of that name."""
    _session(machine.other, machine.os, OTHER, "Notes")
    _session(machine.default, machine.orch, LIVE, "Notes")
    rt, got = _tab(machine, monkeypatch, primary=False, cwd=machine.os,
                   account=str(machine.other))
    _resume(rt, "Notes")
    assert [(o["resume"], o["config_dir"]) for o in got.opened] ==         [(OTHER, str(machine.other))]
