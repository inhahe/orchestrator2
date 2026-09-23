"""Running a Claude Code binary other than the one the SDK bundles.

Reported 2026-09-22, the day Opus 5.5 shipped::

    Claude Code 2.1.259 does not support this model;
    version 2.1.280 or newer is required.

The Agent SDK pins a CLI version and ships that binary inside the wheel, so a
model released after the pin is refused until the SDK catches up. "Upgrade the
SDK" was the obvious guess and was **not** enough: the newest SDK that day
(0.2.157) pinned ``__cli_version__ = "2.1.277"``, still short of 2.1.280, which
is why this option exists rather than a version bump.

``ClaudeAgentOptions.cli_path`` has always accepted an override; orchestrator2
simply never set it (``grep cli_path sdk_bridge.py config.py`` found nothing).

Verified end to end against both binaries before any of this was written:

    cli 2.1.280 + opus-5-5 : ('ok', 'OK')
    bundled     + opus-5-5 : ('ok', 'API Error: 400 Claude Code 2.1.259 ...')

The second line is the control -- without it the first proves only that a
session can start, not that the override is what made the model work.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import parse_args                       # noqa: E402
from sdk_bridge import SDKBridge                     # noqa: E402
from state import init_state_from_config             # noqa: E402


def _opts(argv=(), **cfg_over):
    cfg = parse_args(list(argv))
    for k, v in cfg_over.items():
        object.__setattr__(cfg, k, v)
    st = init_state_from_config(cfg)

    async def bcast(msg):
        pass

    br = SDKBridge(config=cfg, state=st, broadcaster=bcast)
    return br._make_options()


# --------------------------------------------------------------------------
# The option
# --------------------------------------------------------------------------

def test_the_flag_parses():
    assert parse_args(["--cli-path", r"C:\x\claude.exe"]).cli_path == r"C:\x\claude.exe"


def test_the_bundled_cli_is_the_default():
    """Unset must leave the key off entirely, not set it to None or "" -- the
    SDK's own resolution (bundled, then PATH) has to run untouched."""
    assert parse_args([]).cli_path is None

    opts = _opts()

    assert getattr(opts, "cli_path", None) is None


def test_an_existing_binary_is_used(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"not really a binary")

    opts = _opts(cli_path=str(exe))

    assert str(getattr(opts, "cli_path")) == str(exe)


def test_a_missing_binary_falls_back_instead_of_failing(tmp_path):
    """A typo in a path must not make a session unable to connect at all.
    Running the model an older CLI supports is a far better outcome than a
    session that cannot start."""
    opts = _opts(cli_path=str(tmp_path / "nope" / "claude.exe"))

    assert getattr(opts, "cli_path", None) is None


def test_a_missing_binary_is_logged_loudly(tmp_path, caplog):
    """Silently ignoring it would present as "my new model is still refused"
    with nothing to explain why."""
    import logging
    with caplog.at_level(logging.WARNING):
        _opts(cli_path=str(tmp_path / "gone.exe"))

    assert any("cli-path" in r.message.lower() or "cli_path" in r.message.lower()
               for r in caplog.records), [r.message for r in caplog.records]


def test_setting_it_does_not_disturb_the_rest_of_the_options(tmp_path):
    """The override is additive: cwd, permission mode and the session-state
    env var must survive it."""
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"x")

    opts = _opts(cli_path=str(exe))

    assert opts.cwd
    assert opts.env.get("CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS") == "1"


# --------------------------------------------------------------------------
# Per session, not just per hub
# --------------------------------------------------------------------------

def test_a_session_can_be_launched_on_its_own_cli(tmp_path):
    """The newer CLI is `latest`, not `stable`. Being able to put one session
    on it and leave the rest on the bundled binary is the point -- this project
    has been bitten by CLI-side behaviour more than once."""
    import inspect
    import server

    sig = inspect.signature(server._create_runtime)
    assert "cli_path" in sig.parameters


def test_the_launch_api_forwards_it():
    import inspect
    import server

    src = inspect.getsource(server)
    assert 'cli_path = (body.get("cli_path")' in src, (
        "the lobby can't launch a session on a different CLI"
    )


def test_a_session_override_beats_the_hub_default(tmp_path):
    """dataclasses.replace semantics: the per-session value must win, and the
    hub's must still apply when no override is given."""
    import dataclasses
    hub_exe = tmp_path / "hub.exe"
    ses_exe = tmp_path / "ses.exe"
    for p in (hub_exe, ses_exe):
        p.write_bytes(b"x")

    cfg = parse_args(["--cli-path", str(hub_exe)])
    inherited = dataclasses.replace(cfg)
    overridden = dataclasses.replace(cfg, cli_path=str(ses_exe))

    assert inherited.cli_path == str(hub_exe)
    assert overridden.cli_path == str(ses_exe)


# --------------------------------------------------------------------------
# Why this exists at all
# --------------------------------------------------------------------------

def test_the_help_explains_the_failure_it_solves():
    """Someone hitting "does not support this model" should find this option
    by searching the help for the error text they were given."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        parse_args(["--help"])
    # argparse hard-wraps help text, so the phrase spans a line break in the
    # rendered output -- collapse whitespace before looking for it.
    import re
    text = re.sub(r"\s+", " ", buf.getvalue().lower())

    assert "--cli-path" in text
    assert "does not support this model" in text
