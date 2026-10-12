"""A launch's ``--compact-at``, for the session it opens or lands on.

Asked for 2026-10-12, to compare one Claude account's sessions compacting
at 300k context tokens against the rest compacting at the 1M models'
default (950k), for a week. ``--compact-at`` was a hub flag only: a launch
that joined a running hub dropped it, as it once dropped ``--cli-path``,
and ``/autocompact N`` set it on a live session but did not survive a
restart. ``go.bat`` restarts every session, so the threshold has to come
with each launch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                        # noqa: E402
from config import default_compact_at, parse_args    # noqa: E402

MODEL = "claude-opus-5-5"


def test_an_explicit_threshold_is_handed_over():
    cfg = parse_args(["--model", MODEL, "--compact-at", "300000"])

    assert server._hub_launch_kwargs(cfg, None)["compact_at"] == 300000


def test_the_models_default_is_not_handed_over():
    """The hub applies its own default; sending it would overwrite a
    session's own /autocompact value with the default on every launch."""
    cfg = parse_args(["--model", MODEL])
    assert cfg.compact_at == default_compact_at(MODEL)

    assert server._hub_launch_kwargs(cfg, None)["compact_at"] is None


def test_it_goes_on_the_wire(monkeypatch):
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

    server._launch_into_hub(8787, cwd="C:\\w", resume=None, no_continue=False,
                            compact_at=300000)

    assert seen["compact_at"] == 300000


def test_a_new_session_is_opened_with_it(monkeypatch, tmp_path):
    got = {}

    class _RT:
        rid = "R2"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))

    res = asyncio.run(server.api_session_launch({
        "cwd": str(tmp_path), "no_continue": True, "compact_at": 300000}))

    assert res == {"ok": True, "rid": "R2"}
    assert got["compact_at"] == 300000


def test_nonsense_is_ignored(monkeypatch, tmp_path):
    got = {}

    class _RT:
        rid = "R3"

    async def fake_create_runtime(**kw):
        got.update(kw)
        return _RT()

    monkeypatch.setattr(server, "_create_runtime", fake_create_runtime)
    monkeypatch.setattr(server, "config", parse_args([]))

    for bad in (0, -5, "300000", True, 3.5):
        got.clear()
        asyncio.run(server.api_session_launch({
            "cwd": str(tmp_path), "no_continue": True, "compact_at": bad}))
        assert got["compact_at"] is None, bad


def test_a_session_already_open_takes_it(monkeypatch, tmp_path):
    """The usual case with go.bat: the hub is up, the session is live."""
    cfg = parse_args(["--model", MODEL])

    class _State:
        session_id = "S1"
        model = MODEL
        effort = None
        bell_events = set()
        queued_prompts = []

    class _RT:
        rid = "R4"
        state = _State()
        config = cfg
        bridge = None

    rt = _RT()
    monkeypatch.setattr(server, "config", cfg)
    monkeypatch.setattr(server, "runtimes", {"R4": rt})
    monkeypatch.setattr(server, "_session_to_open", lambda *a, **k: "S1")

    res = asyncio.run(server.api_session_launch({
        "cwd": str(tmp_path), "compact_at": 300000}))

    assert res.get("rid") == "R4"
    assert rt.config.compact_at == 300000
    assert rt.state._compact_at == 300000
