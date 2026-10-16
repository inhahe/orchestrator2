"""A launch's ``--compact-at`` and ``--auto-compact``, for its session alone.

Each launch's compaction settings belong to the session it opens or lands
on, as its name does: handed to a running hub with the launch, set on that
session without touching the hub's own settings, never inherited by other
sessions from whichever launch started the hub, and carried by ``/move``.
What a launch was not given is not sent, so that relaunching a session does
not undo a ``/autocompact`` set on it since.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                        # noqa: E402
from config import default_compact_at, parse_args    # noqa: E402

MODEL = "claude-opus-5-5"


# -- What the launch hands over ----------------------------------------------

def test_what_the_launch_was_given_is_handed_over():
    cfg = parse_args(["--model", MODEL, "--auto-compact", "--compact-at", "300000"])
    kw = server._hub_launch_kwargs(cfg, None)

    assert kw["compact_at"] == 300000
    assert kw["auto_compact"] is True


def test_no_compact_is_handed_over_as_off():
    cfg = parse_args(["--no-compact"])

    assert server._hub_launch_kwargs(cfg, None)["auto_compact"] is False


def test_what_it_was_not_given_is_not():
    cfg = parse_args(["--model", MODEL])
    kw = server._hub_launch_kwargs(cfg, None)

    assert kw["compact_at"] is None
    assert kw["auto_compact"] is None


def test_the_models_default_given_explicitly_is_handed_over():
    """Given, it is meant: it overrides a /autocompact set on the session."""
    default = default_compact_at(MODEL)
    cfg = parse_args(["--model", MODEL, "--compact-at", str(default)])

    assert server._hub_launch_kwargs(cfg, None)["compact_at"] == default


@pytest.mark.parametrize("bad", ["0", "-5", "3.5", "lots"])
def test_a_threshold_that_is_not_a_positive_whole_number_is_refused(bad, capsys):
    with pytest.raises(SystemExit):
        parse_args(["--compact-at", bad])


def test_both_go_on_the_wire(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "rid": "R1"}'

    def fake_urlopen(req, timeout=None):
        seen.update(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    server._launch_into_hub(8787, cwd="C:/w", resume=None, no_continue=False,
                            compact_at=300000, auto_compact=True)

    assert seen["compact_at"] == 300000
    assert seen["auto_compact"] is True


# -- A session the hub opens ---------------------------------------------------

def _opened_with(monkeypatch, tmp_path, body):
    got = {}

    class _RT:
        rid = "R2"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))
    res = asyncio.run(server.api_session_launch(
        {"cwd": str(tmp_path), "no_continue": True, **body}))
    assert res == {"ok": True, "rid": "R2"}
    return got


def test_a_new_session_is_opened_with_them(monkeypatch, tmp_path):
    got = _opened_with(monkeypatch, tmp_path,
                       {"compact_at": 300000, "auto_compact": True})

    assert got["compact_at"] == 300000
    assert got["auto_compact"] is True


@pytest.mark.parametrize("bad", [0, -5, "300000", True, 3.5])
def test_a_threshold_that_makes_no_sense_is_ignored(monkeypatch, tmp_path, bad):
    assert _opened_with(monkeypatch, tmp_path, {"compact_at": bad})["compact_at"] is None


@pytest.mark.parametrize("bad", [1, "yes", None])
def test_a_switch_that_is_not_true_or_false_is_ignored(monkeypatch, tmp_path, bad):
    assert _opened_with(monkeypatch, tmp_path, {"auto_compact": bad})["auto_compact"] is None


def _config_of_new_runtime(monkeypatch, tmp_path, hub_argv, **kw):
    """The Config _create_runtime builds, caught before any bridge starts."""
    monkeypatch.setattr(server, "config", parse_args(hub_argv))
    seen = {}

    def stop(cfg):
        seen["cfg"] = cfg
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "init_state_from_config", stop)
    with pytest.raises(RuntimeError):
        asyncio.run(server._create_runtime(cwd=str(tmp_path), no_continue=True, **kw))
    return seen["cfg"]


def test_a_session_does_not_inherit_the_hubs_launch(monkeypatch, tmp_path):
    """The hub was started by a launch with a trial's settings: the sessions
    it opens for other launches, or from the lobby, are not in the trial."""
    cfg = _config_of_new_runtime(
        monkeypatch, tmp_path,
        ["--model", MODEL, "--auto-compact", "--compact-at", "300000"])

    assert cfg.compact_at == default_compact_at(MODEL)
    assert cfg.auto_compact is False
    assert cfg.no_compact is True


def test_a_session_gets_the_default_for_its_own_model(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                                 model="claude-sonnet-4-5")

    assert cfg.compact_at == default_compact_at("claude-sonnet-4-5")


def test_a_session_gets_what_its_launch_gave(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                                 compact_at=300000, auto_compact=True)

    assert cfg.compact_at == 300000
    assert cfg.auto_compact is True
    assert cfg.no_compact is False


# -- A session already open ----------------------------------------------------

def _land_on_open_session(monkeypatch, tmp_path, body):
    hub = parse_args(["--model", MODEL])
    hub_values = (hub.compact_at, hub.auto_compact, hub.no_compact)

    class _State:
        session_id = "S1"
        model = MODEL
        effort = None
        bell_events = set()
        queued_prompts = []

    class _RT:
        rid = "R4"
        state = _State()
        # The hub's own session runs on the hub's Config itself.
        config = hub
        bridge = None

    rt = _RT()
    monkeypatch.setattr(server, "config", hub)
    monkeypatch.setattr(server, "runtimes", {"R4": rt})
    monkeypatch.setattr(server, "_session_to_open", lambda *a, **k: "S1")
    res = asyncio.run(server.api_session_launch({"cwd": str(tmp_path), **body}))
    assert res.get("rid") == "R4"
    assert (hub.compact_at, hub.auto_compact, hub.no_compact) == hub_values, \
        "the hub's own settings changed"
    return rt


def test_a_session_already_open_takes_them(monkeypatch, tmp_path):
    """The usual case with go.bat: the hub is up and the session is live."""
    rt = _land_on_open_session(monkeypatch, tmp_path,
                               {"compact_at": 300000, "auto_compact": True})

    assert rt.state._compact_at == 300000
    assert rt.state._auto_compact is True


def test_a_launch_without_them_leaves_the_session_alone(monkeypatch, tmp_path):
    rt = _land_on_open_session(monkeypatch, tmp_path, {})

    assert not hasattr(rt.state, "_compact_at")
    assert not hasattr(rt.state, "_auto_compact")


def test_autocompact_reports_the_sessions_own_setting():
    import commands
    hub = parse_args(["--model", MODEL])
    st = types.SimpleNamespace(_compact_at=300000, _auto_compact=True)

    res = commands._cmd_autocompact("", st, hub)

    assert res.messages[0]["data"]["message"] == "auto-compact: ON (at ~300000 tok)"


# -- /move ---------------------------------------------------------------------

def _runtime(state_values, **config_values):
    st = types.SimpleNamespace(**state_values)
    cfg = types.SimpleNamespace(api_key=None, **config_values)
    return types.SimpleNamespace(state=st, config=cfg)


def test_a_move_carries_what_was_set_on_the_session(monkeypatch):
    monkeypatch.setattr(server.wakeup_store, "load_wakeup", lambda *a: None)
    rt = _runtime({"_compact_at": 300000, "_auto_compact": True},
                  compact_at=950000, auto_compact=False)

    carry = server._move_carry(rt, "C:/w", "S1")

    assert carry["compact_at"] == 300000
    assert carry["auto_compact"] is True


def test_a_move_carries_what_the_session_was_opened_with(monkeypatch):
    monkeypatch.setattr(server.wakeup_store, "load_wakeup", lambda *a: None)
    rt = _runtime({}, compact_at=300000, auto_compact=True)

    carry = server._move_carry(rt, "C:/w", "S1")

    assert carry["compact_at"] == 300000
    assert carry["auto_compact"] is True


# -- Remembered by session id ------------------------------------------------
#
# A session reopened without the launch that set its compaction -- from the
# session list, revived by a hub restart, after a reboot -- keeps it.

from session import load_session_compaction, save_session_compaction  # noqa: E402


def test_the_record_round_trips_and_a_none_keeps_the_old_value():
    save_session_compaction("S9", 300000, True)
    save_session_compaction("S9", None, False)

    assert load_session_compaction("S9") == (300000, False)
    assert load_session_compaction("never-seen") == (None, None)


def test_a_session_reopened_without_its_launch_keeps_its_compaction(monkeypatch, tmp_path):
    save_session_compaction("S1", 300000, True)

    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--model", MODEL], resume="S1")

    assert cfg.compact_at == 300000
    assert cfg.auto_compact is True


def test_what_a_launch_gives_beats_what_was_remembered_and_is_remembered(monkeypatch, tmp_path):
    save_session_compaction("S1", 300000, True)

    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                                 resume="S1", auto_compact=False)

    assert cfg.auto_compact is False
    assert cfg.compact_at == 300000
    assert load_session_compaction("S1") == (300000, False)


def test_a_new_session_is_remembered_once_it_has_an_id(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--model", MODEL],
                                 compact_at=300000, auto_compact=True)

    server._on_session_id("S7", cfg)

    assert load_session_compaction("S7") == (300000, True)


def test_a_session_that_never_set_it_leaves_no_record(monkeypatch, tmp_path):
    cfg = _config_of_new_runtime(monkeypatch, tmp_path, ["--model", MODEL])

    server._on_session_id("S8", cfg)

    assert load_session_compaction("S8") == (None, None)


def test_a_launch_onto_an_open_session_is_remembered(monkeypatch, tmp_path):
    _land_on_open_session(monkeypatch, tmp_path, {"compact_at": 300000, "auto_compact": True})

    assert load_session_compaction("S1") == (300000, True)


def test_autocompact_is_remembered_and_reports_the_sessions_threshold():
    import commands
    hub = parse_args(["--model", MODEL])
    st = types.SimpleNamespace(session_id="S5")

    commands._cmd_autocompact("300000", st, hub)
    assert load_session_compaction("S5") == (300000, True)

    commands._cmd_autocompact("off", st, hub)
    assert load_session_compaction("S5") == (300000, False)

    res = commands._cmd_autocompact("on", st, hub)
    assert res.messages[0]["data"]["message"] == "auto-compact ON (at ~300000 tok)"
