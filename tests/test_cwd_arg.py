"""A path option as a Windows command line delivers it.

In ``--cwd "%cd%"`` at the root of a drive, ``%cd%`` is ``C:\\``, and the
backslash before the closing quote escapes it: Python receives the value
``C:" --open --effort xhigh ...``, the arguments after it swallowed.  No
Windows path contains a quote, so a path option's value with one is refused,
saying why, as it is read.  ``--cwd .`` is the form that cannot go wrong; a
restart passes it on as the directory it resolved to.  That ``--cwd .`` is
the launch directory, and counts as given, is tests/test_launch_resume.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                       # noqa: E402
from config import parse_args                       # noqa: E402

windows = pytest.mark.skipif(os.name != "nt", reason="a Windows command line")


@windows
def test_a_value_that_swallowed_the_arguments_after_it_is_refused(capsys):
    # What Python received from cmd for --cwd "%cd%" --open --effort xhigh in C:\
    with pytest.raises(SystemExit):
        parse_args(["--cwd", 'C:" --open --effort xhigh '])

    err = capsys.readouterr().err
    assert "has a quote in it" in err
    assert "--cwd ." in err, "the refusal says what to use instead"


@windows
def test_it_is_refused_before_the_leftover_words_are(capsys):
    """A quoted value with spaces after it leaves words argparse has no use
    for; the refusal must come first, or only "unrecognized arguments"."""
    # --cwd "C:\" --initial-prompt "fix the bug" --open
    with pytest.raises(SystemExit):
        parse_args(["--cwd", 'C:" --initial-prompt fix', "the", 'bug --open'])

    err = capsys.readouterr().err
    assert "has a quote in it" in err, err
    assert "unrecognized" not in err, err


@windows
@pytest.mark.parametrize("flag", ["--config-dir", "--log-file", "--cli-path"])
def test_every_path_option_is_read_the_same_way(flag, capsys):
    # --config-dir "%~dp0" --open: %~dp0 always ends in a backslash.
    with pytest.raises(SystemExit):
        parse_args([flag, 'D:\\utils" --open'])

    assert "has a quote in it" in capsys.readouterr().err


def test_elsewhere_a_quote_is_just_part_of_a_name(monkeypatch):
    monkeypatch.setattr(config.os, "name", "posix")

    assert config._unmangled_path('/home/u/my"proj') == '/home/u/my"proj'


def test_the_launch_command_keeps_the_directory_it_resolved_to():
    import server

    assert server._with_launch_cwd(["server.py", "--cwd", ".", "--open"], "D:/w") \
        == ["server.py", "--cwd", "D:/w", "--open"]
    assert server._with_launch_cwd(["server.py", "--cwd=.", "--open"], "D:/w") \
        == ["server.py", "--cwd=D:/w", "--open"]
    assert server._with_launch_cwd(["server.py", "--open"], "D:/w") \
        == ["server.py", "--open"]


def test_a_restart_passes_the_launch_directory_resolved(monkeypatch, tmp_path):
    """With no session to follow (a picker still to choose), the restart
    keeps the launch's directory -- as resolved, not as typed: '.' would be
    resolved again against wherever the hub is by then."""
    import subprocess
    import server
    from state import State
    seen = {}

    def fake_popen(args, **kw):
        seen["argv"] = list(args)
        raise OSError("not really")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)
    cfg = parse_args(["--cwd", "."])
    # What main() makes of the launch command, then a /cwd elsewhere.
    monkeypatch.setattr(sys, "argv", server._with_launch_cwd(
        ["server.py", "--cwd", ".", "--open"], cfg.cwd))
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setattr(server, "config", cfg)
    monkeypatch.setattr(server, "state", State())
    monkeypatch.setattr(server, "_picker_mode", True)

    asyncio.run(server.api_restart())

    argv = seen["argv"]
    assert argv.count("--cwd") == 1, argv
    assert argv[argv.index("--cwd") + 1] == str(tmp_path.resolve()), argv
