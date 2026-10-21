"""``--cwd`` as a Windows command line delivers it.

In ``--cwd "%cd%"`` at the root of a drive, ``%cd%`` is ``C:\\``, and the
backslash before the closing quote escapes it: Python receives the value
``C:" --open --effort xhigh ...``, every argument after it swallowed.  No path
contains a quote, so that value is refused, saying why; ``--cwd .`` is the
form that cannot go wrong.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_args                       # noqa: E402

# What Python received from cmd for --cwd "%cd%" --open --effort xhigh in C:\
SWALLOWED = 'C:" --open --effort xhigh '


def test_a_value_that_swallowed_the_arguments_after_it_is_refused(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--cwd", SWALLOWED])

    err = capsys.readouterr().err
    assert "has a quote in it" in err
    assert "--cwd ." in err, "the refusal says what to use instead"


def test_dot_is_the_directory_the_launch_came_from(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    cfg = parse_args(["--cwd", "."])

    assert Path(cfg.cwd) == tmp_path.resolve()
    assert cfg.cwd_given, "given, as \"%cd%\" was: a resumed session stays here"
