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
It follows a session it can find to its directory and account, unless those
were asked for.  Otherwise it stops with an error naming each candidate: id,
last activity, directory, account.  A launch that joins a hub prints the
session's address.
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
    # Set, then delete: so the variable is put back as it was even when
    # main() assigns it directly, which monkeypatch does not see.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "placeholder")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
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


def test_not_when_the_directory_was_asked_for(machine):
    _session(machine.default, machine.os, IDLE, "OS F old 2")

    cfg, note, err = _settle(_cfg(machine, machine.orch, "OS F old 2", cwd_given=True))

    assert err is not None and note is None
    assert f"No session in {machine.orch} is called 'OS F old 2'" in err
    assert IDLE in err and machine.os in err, "it does not say where it is"
    assert f'Resume it with --cwd "{machine.os}".' in err
    assert cfg.resume == "OS F old 2"


def test_a_title_on_another_account_is_followed_there(machine):
    _session(machine.other, machine.os, MOVED, "OS F old")

    cfg, note, err = _settle(_cfg(machine, machine.os, "OS F old"))

    assert err is None
    assert cfg.resume == MOVED and cfg.config_dir == str(machine.other)
    assert ".claude-work" in note


def test_not_when_the_account_was_chosen(machine):
    _session(machine.other, machine.os, MOVED, "OS F old")

    _cfg_, _note, err = _settle(_cfg(machine, machine.os, "OS F old",
                                     config_dir=str(machine.default)))

    assert err is not None and MOVED in err and ".claude-work" in err
    assert f'Resume it with --config-dir "{machine.other}".' in err


def test_nor_when_the_account_came_from_the_environment(machine, monkeypatch):
    _session(machine.other, machine.os, MOVED, "OS F old")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(machine.default))

    _cfg_, _note, err = _settle(_cfg(machine, machine.os, "OS F old"))

    assert err is not None and MOVED in err


def test_one_session_under_two_accounts_is_still_one(machine):
    """The same transcript, id and all, in both accounts' trees (a file copied
    by hand) is one session to follow, not "several"."""
    _session(machine.default, machine.os, IDLE, "OS F old 2")
    _session(machine.other, machine.os, IDLE, "OS F old 2")

    cfg, _note, err = _settle(_cfg(machine, machine.orch, "OS F old 2"))

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
    _session(machine.default, machine.os, LIVE, "OS F")
    _session(machine.other, machine.os, MOVED, "OS F")

    cfg, _note, err = _settle(_cfg(machine, machine.orch, "OS F"))

    assert cfg.resume == "OS F"
    assert LIVE in err and MOVED in err and "--resume <id>" in err


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


def test_an_id_with_a_directory_it_is_not_in_is_refused(machine):
    """The CLI looks for the transcript only under the launch directory."""
    _session(machine.default, machine.os, IDLE)

    _cfg_, _note, err = _settle(_cfg(machine, machine.orch, IDLE, cwd_given=True))

    assert "resumed only from its own directory" in err
    assert f'--cwd "{machine.os}"' in err


def test_an_id_on_another_account_is_followed_there(machine):
    _session(machine.other, machine.os, MOVED)

    cfg, note, err = _settle(_cfg(machine, machine.os, MOVED))

    assert err is None and cfg.config_dir == str(machine.other)


def test_not_when_the_account_was_chosen_for_an_id(machine):
    _session(machine.other, machine.os, MOVED)

    _cfg_, _note, err = _settle(_cfg(machine, machine.os, MOVED,
                                     config_dir=str(machine.default)))

    assert f'--config-dir "{machine.other}"' in err


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

    assert os.environ["CLAUDE_CONFIG_DIR"] == str(machine.other.resolve())


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


def _restart_argv(monkeypatch, argv, sid):
    import subprocess
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server, "config", parse_args([]))
    st = State()
    st.session_id = sid
    monkeypatch.setattr(server, "state", st)
    asyncio.run(server.api_restart())
    return seen["argv"]


def test_a_restart_lets_the_session_it_resumes_choose_the_directory(monkeypatch):
    """After /cwd or /resume the launch's --cwd names the wrong directory, and
    a session is resumed only from its own."""
    argv = _restart_argv(monkeypatch, ["server.py", "--cwd", r"D:\a", "--open"], IDLE)

    assert "--cwd" not in argv and r"D:\a" not in argv
    assert argv[argv.index("--resume") + 1] == IDLE


def test_a_restart_with_no_session_keeps_the_directory(monkeypatch):
    argv = _restart_argv(monkeypatch, ["server.py", "--cwd", r"D:\a"], None)

    assert argv[argv.index("--cwd") + 1] == r"D:\a"


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
