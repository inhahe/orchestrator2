"""What became of a launch is in the ``--log-file``.

Found 2026-10-20.  Six SlateOS lanes were launched one after another.  Four of
them were refused before they reached the hub -- ``--resume "OS A"`` named a
session on another account than the launcher's -- and the hub's log, where
everything else about that evening was, showed those four only as "log file
opened" and "server shut down", 0.5 to 6 seconds apart.  The reason was in
``launch-error.log`` and a dialog, neither of which anyone looking at the log
would know to check.  A launch that *joined* the hub left no more than that
either, and the hub said nothing about a launch that landed on a session
already open.

Now each launch writes what became of it -- failed and why, joined which
session, started which server -- to the ``--log-file`` too, and only there:
its console has said so already.  The hub's side is in
``test_agent_name_per_session.py``, beside the launches it describes.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                            # noqa: E402
from config import parse_args                            # noqa: E402


@pytest.fixture
def log_file(tmp_path):
    """A ``--log-file`` set up the way main() sets one up."""
    path = tmp_path / "orchestrator2.log"
    handler = logging.FileHandler(str(path), encoding="utf-8")
    handler.setFormatter(logging.Formatter(server._FILE_LOG_FMT))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield path
    finally:
        root.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "LAUNCH_ERROR_LOG", str(tmp_path / "launch-error.log"))
    monkeypatch.setenv("ORCH2_NO_DIALOG", "1")


def test_a_launch_that_failed_is_in_the_log_file(log_file):
    """The four refused lanes, which only launch-error.log knew about."""
    server._report_launch_failure(
        "No session in C:\\os is called 'OS A'.\nSessions called 'OS A': ...")

    text = log_file.read_text(encoding="utf-8")
    assert "launch failed: No session in C:\\os is called 'OS A'." in text
    assert "command line:" in text


def _no_log_file_yet(monkeypatch, argv):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [
        h for h in root.handlers if not isinstance(h, logging.FileHandler)])
    monkeypatch.setattr(sys, "argv", ["server.py", *argv])


def test_so_is_one_that_failed_before_reading_its_flags(monkeypatch, tmp_path):
    """argparse refuses the command line before main() has opened the log, so
    the --log-file on that command line is opened for the one record."""
    path = tmp_path / "given.log"
    _no_log_file_yet(monkeypatch, ["--log-file", str(path), "--noresume"])

    server._report_launch_failure("unrecognized arguments: --noresume")

    assert "unrecognized arguments: --noresume" in path.read_text(encoding="utf-8")


def test_even_into_a_directory_not_made_yet(monkeypatch, tmp_path):
    """As main() would have made it."""
    path = tmp_path / "logs" / "new" / "o2.log"
    _no_log_file_yet(monkeypatch, ["--log-file", str(path), "--noresume"])

    server._report_launch_failure("unrecognized arguments: --noresume")

    assert "--noresume" in path.read_text(encoding="utf-8")


def test_a_flag_after_log_file_is_not_taken_for_its_path(monkeypatch, tmp_path):
    """``--log-file --port 9000``: argparse refuses it for want of a path,
    and so does this, rather than leaving a file called --port."""
    work = tmp_path / "launched-from"
    work.mkdir()
    monkeypatch.chdir(work)
    _no_log_file_yet(monkeypatch, ["--log-file", "--port", "9000"])

    server._report_launch_failure("argument --log-file: expected one argument")

    assert list(work.iterdir()) == []


def test_the_console_is_not_told_twice(log_file):
    """It has printed the same thing in words already."""
    console = io.StringIO()
    stream = logging.StreamHandler(console)
    root = logging.getLogger()
    root.addHandler(stream)
    try:
        server._file_log(logging.INFO, "launch: %s", "joined")
    finally:
        root.removeHandler(stream)

    assert console.getvalue() == ""
    assert "launch: joined" in log_file.read_text(encoding="utf-8")


def test_a_launch_that_joined_the_hub_says_which_session(hub_answers, log_file):
    hub_answers({"ok": True, "rid": "s12", "reused": True})

    server._launch_into_hub(8420, cwd="C:\\os", resume=None, no_continue=False,
                            agent_name="Lane-A")

    text = log_file.read_text(encoding="utf-8")
    assert "joined the hub on port 8420, which already had it open as s12" in text
    assert "name=Lane-A" in text


def test_a_headless_server_that_died_starting_is_in_the_log_file(
        monkeypatch, tmp_path, log_file):
    """--detach: the child may die before it has opened the log itself."""
    import subprocess
    monkeypatch.setattr(server, "config", None)
    cfg = parse_args(["--cwd", str(tmp_path), "--detach", "--no-continue"])
    monkeypatch.setattr(server, "_parse_args_or_report", lambda: cfg)
    monkeypatch.setattr(server, "_probe_hub", lambda port: None)
    monkeypatch.setattr(server, "_ensure_logged_in", lambda **kw: None)
    monkeypatch.setattr(server, "_bind_port", lambda host, port, **kw: (
        types.SimpleNamespace(close=lambda: None), 8421))

    class _Child:
        pid = 4242

        def __init__(self, argv, **kw):
            kw["stderr"].write("ImportError: no module named fastapi\n")

        def poll(self):
            return 3

    monkeypatch.setattr(subprocess, "Popen", _Child)

    with pytest.raises(SystemExit) as ei:
        server.main()

    assert ei.value.code == 3
    text = log_file.read_text(encoding="utf-8")
    assert "headless server (pid 4242) exited with 3 while starting" in text
    assert "ImportError: no module named fastapi" in text
