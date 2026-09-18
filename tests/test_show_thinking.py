"""Tests for --show-thinking / /show-thinking, the thinking-block display gate.

This flag existed for a long time as *dead config*: it was parsed into
``Config``, copied into ``State``, and then read by nothing at all — so passing
it did exactly nothing, silently.  These tests pin the whole chain that makes
it real, because every link is individually easy to drop:

    --show-thinking -> Config -> State -> status dict -> WebSocket -> chat.js

The last hop is a string check against the frontend rather than a DOM test
(there's no JS test runner here), but a missing wire there is precisely how
this became dead config the first time, so it's worth asserting.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands import classify                      # noqa: E402
from config import SLASH_COMMANDS, parse_args      # noqa: E402
from state import init_state_from_config, state_to_status_dict  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "static"


def _state(argv):
    cfg = parse_args(argv)
    return cfg, init_state_from_config(cfg)


def test_flag_reaches_the_status_dict():
    """The end-to-end wire: CLI flag -> Config -> State -> status payload."""
    cfg, st = _state(["--show-thinking"])
    assert cfg.show_thinking is True
    assert st.show_thinking is True
    assert state_to_status_dict(st, cfg)["show_thinking"] is True


def test_default_is_collapsed():
    cfg, st = _state([])
    assert cfg.show_thinking is False
    assert state_to_status_dict(st, cfg)["show_thinking"] is False


def test_status_dict_always_carries_the_key():
    """status.js checks ``!= null``, so the key must never be omitted."""
    cfg, st = _state([])
    assert "show_thinking" in state_to_status_dict(st, cfg)


def test_classify_show_thinking():
    assert classify("/show-thinking") == ("show-thinking", "")
    assert classify("/show-thinking on") == ("show-thinking", "on")
    assert classify("/show-thinking off") == ("show-thinking", "off")
    assert classify("/showthinking on") == ("show-thinking", "on")


def test_show_thinking_is_not_the_thinking_command():
    """``/thinking`` gates the API; ``/show-thinking`` only gates display.

    Confusing them would be bad: ``/thinking off`` reconnects and disables
    reasoning entirely, which is not what someone toggling a display option
    expects.
    """
    assert classify("/thinking off") == ("thinking", "off")
    assert classify("/show-thinking off") == ("show-thinking", "off")


def test_command_toggles_state_and_pushes_the_update():
    from commands import _cmd_show_thinking

    cfg, st = _state([])

    res = _cmd_show_thinking("on", st, cfg)
    assert st.show_thinking is True
    assert res.state_updates == {"show_thinking": True}

    res = _cmd_show_thinking("off", st, cfg)
    assert st.show_thinking is False
    assert res.state_updates == {"show_thinking": False}


def test_command_with_no_arg_reports_without_changing():
    from commands import _cmd_show_thinking

    cfg, st = _state(["--show-thinking"])
    res = _cmd_show_thinking("", st, cfg)
    assert st.show_thinking is True, "query form must not mutate"
    assert not res.state_updates
    assert "ON" in res.messages[0]["data"]["message"]


def test_bad_arg_is_an_error_and_does_not_change_state():
    from commands import _cmd_show_thinking

    cfg, st = _state([])
    res = _cmd_show_thinking("maybe", st, cfg)
    assert st.show_thinking is False
    assert res.messages[0]["subtype"] == "error"


def test_command_is_tab_completable():
    assert "/show-thinking" in SLASH_COMMANDS


def test_frontend_consumes_the_flag():
    """The hop that made this dead config before: nothing read the value."""
    status_js = (STATIC / "status.js").read_text(encoding="utf-8")
    chat_js = (STATIC / "chat.js").read_text(encoding="utf-8")

    assert "status.show_thinking" in status_js, \
        "status.js ignores show_thinking — the flag is dead again"
    assert "setShowThinking" in status_js
    assert "setShowThinking" in chat_js, "chat.js exposes no setter to call"
    assert "_showThinking" in chat_js
    # The gate itself: it must actually open the block.
    assert "if (_showThinking)" in chat_js, \
        "chat.js stores the flag but never acts on it"
