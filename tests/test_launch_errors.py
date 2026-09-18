"""A mistyped launch flag must not fail in silence.

``orch2.bat`` starts the server through ``start /MIN`` + ``tray_minimizer``,
which gives the process a real console and then hides the window.  argparse
writes "unrecognized arguments" to *that* console and exits 2 -- before
``--log-file`` has been read, so there is no log line either.  A typo therefore
made the launch do nothing at all, invisibly.

Reported 2026-09-03: ``orch2c --noresume`` (the real flag is ``--no-continue``)
looked like "it started but didn't resume".  It had not started.  The session
the user was actually looking at came from a *different* launch, under an
account with no session for that directory -- so the silent failure got
attributed to a flag that never ran, and the genuine bug next to it (a fresh
session inheriting a stale queued prompt) was harder to see for it.

An invisible failure is worse than a loud one precisely because it gets
attributed to whatever happened next.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def _run(args, env_extra=None):
    import os
    env = dict(os.environ)
    # Never let a test pop a modal dialog on the developer's desktop.
    env["ORCH2_NO_DIALOG"] = "1"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(ROOT / "server.py")] + args,
                          capture_output=True, text=True, timeout=180,
                          cwd=str(ROOT), env=env)


@pytest.fixture(autouse=True)
def _clean_log():
    log = server.LAUNCH_ERROR_LOG
    if log.exists():
        log.unlink()
    yield
    if log.exists():
        log.unlink()


def test_a_bad_flag_exits_rather_than_starting():
    """The precondition for everything else: it really did not start."""
    p = _run(["--noresume"])
    assert p.returncode == 2


def test_a_bad_flag_still_prints_to_stderr():
    """A visible console must keep behaving exactly as it always did."""
    p = _run(["--noresume"])
    assert "unrecognized arguments: --noresume" in p.stderr


def test_a_bad_flag_leaves_a_file_behind():
    """The whole point: somewhere a human can look afterwards, when the
    console that got the message was hidden and is now gone."""
    _run(["--noresume"])
    assert server.LAUNCH_ERROR_LOG.exists(), "the failure left no trace at all"
    text = server.LAUNCH_ERROR_LOG.read_text(encoding="utf-8")
    assert "unrecognized arguments: --noresume" in text
    assert "--noresume" in text.split("Command line:")[1], (
        "the record does not show what was actually run")


def test_the_record_does_not_hang_the_process():
    """Written after the first version of this fix popped a *blocking* modal
    dialog on a piped run and pinned the process until it was killed."""
    p = _run(["--noresume"])          # _run would raise TimeoutExpired on a hang
    assert p.returncode == 2


def test_help_is_not_a_failure():
    """``--help`` exits 0; reporting it would cry wolf on every use."""
    p = _run(["--help"])
    assert p.returncode == 0
    assert not server.LAUNCH_ERROR_LOG.exists()


# ---------------------------------------------------------------------------
# The dialog gate
# ---------------------------------------------------------------------------

def test_no_console_is_not_the_tray_case():
    """``_console_is_hidden`` must be narrower than ``not _console_is_visible``.

    "No console at all" -- a piped run, a test, CI -- also has no *visible*
    console, but there is no human at a launcher there, and a modal dialog
    hangs until someone clicks it.  That is not hypothetical: the first version
    of this fix used the looser test and wedged a piped run.
    """
    assert server._console_is_hidden() is False


def test_the_dialog_can_be_disabled_outright():
    """An escape hatch for automation, independent of console detection."""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert 'os.environ.get("ORCH2_NO_DIALOG")' in src


def test_the_dialog_cannot_block_forever():
    """A timeout is what keeps an unattended launch from pinning a process on
    a dialog nobody can see."""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "MessageBoxTimeoutW" in src, "no bounded dialog call"
    assert "timeout_ms" in src
