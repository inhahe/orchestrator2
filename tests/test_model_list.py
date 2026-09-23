"""Tests for the live model list (`/model`) and its cache.

Background: ``/model`` runs on the event loop, so it can only ever read a
cache — it must never block on a network call.  That cache used to be filled
by a single fire-and-forget fetch at startup, and any transient failure (an
OAuth token that happened to be expired right then, a network blip) left the
process permanently serving the hardcoded ``KNOWN_MODELS`` fallback.  The
visible symptom was a model that genuinely exists — ``claude-fable-5`` —
missing from ``/model``, even though ``/model claude-fable-5`` worked fine
(the id isn't validated against the list).

These tests run offline: the network call is monkeypatched.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


class _FakeResponse:
    """Context-manager stand-in for ``urllib.request.urlopen``'s result."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _reset_cache() -> None:
    config._model_cache = None
    config._model_cache_at = 0.0


def _patch_fetch(monkeypatch, *, body: bytes | None = None,
                 error: Exception | None = None) -> None:
    """Make ``fetch_available_models`` hit a fake API with credentials present."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fake_urlopen(_req, timeout=None):
        if error is not None:
            raise error
        return _FakeResponse(body or b"{}")

    monkeypatch.setattr(config.urllib.request, "urlopen", fake_urlopen)


_MODELS_BODY = (
    b'{"data": ['
    b'{"id": "claude-opus-5", "display_name": "Claude Opus 5"},'
    b'{"id": "claude-fable-5", "display_name": "Claude Fable 5"}'
    b']}'
)


def test_fetch_caches_live_list(monkeypatch):
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    got = config.fetch_available_models()
    assert got is not None
    ids = [m for m, _ in got]
    assert ids == ["claude-opus-5", "claude-fable-5"]
    # Cached for the non-blocking readers, and considered fresh.
    assert [m for m, _ in config.get_known_models()] == ids
    assert config.model_cache_is_stale() is False
    _reset_cache()


def test_stale_until_first_successful_fetch(monkeypatch):
    """A never-loaded cache reports stale so /model can flag the fallback."""
    _reset_cache()
    assert config.model_cache_is_stale() is True
    # get_known_models still answers — with the hardcoded fallback.
    assert config.get_known_models() is config.KNOWN_MODELS
    _reset_cache()


def test_cache_expires_after_ttl(monkeypatch):
    """A long-lived hub must re-fetch, or a newly released model never appears."""
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    assert config.fetch_available_models() is not None
    assert config.model_cache_is_stale() is False
    # Pretend the fetch happened more than a TTL ago.
    config._model_cache_at -= (config.MODEL_CACHE_TTL + 1.0)
    assert config.model_cache_is_stale() is True
    # ...but the last good list is still what's served in the meantime.
    assert [m for m, _ in config.get_known_models()] == [
        "claude-opus-5", "claude-fable-5"]
    _reset_cache()


def test_http_error_returns_none_and_keeps_previous_list(monkeypatch):
    """A 401 (expired token) must not wipe a previously-good cache."""
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    assert config.fetch_available_models() is not None
    _patch_fetch(monkeypatch, error=urllib.error.HTTPError(
        "https://api.anthropic.com/v1/models", 401, "Unauthorized", {}, None))
    assert config.fetch_available_models() is None
    assert [m for m, _ in config.get_known_models()] == [
        "claude-opus-5", "claude-fable-5"]
    _reset_cache()


def test_network_error_falls_back_without_raising(monkeypatch):
    _reset_cache()
    _patch_fetch(monkeypatch, error=OSError("connection reset"))
    assert config.fetch_available_models() is None
    assert config.get_known_models() is config.KNOWN_MODELS
    _reset_cache()


def test_fallback_list_covers_current_models():
    """The fallback is what users see when the API is unreachable.

    It's hand-maintained and goes stale, which is exactly how the reported bug
    presented, so pin the models that must be in it.
    """
    ids = {m for m, _ in config.KNOWN_MODELS}
    for expected in ("claude-opus-5-5", "claude-fable-5-1",
                     "claude-opus-5", "claude-sonnet-5", "claude-fable-5"):
        assert expected in ids, f"{expected} missing from KNOWN_MODELS"


def test_the_fallback_leads_with_the_most_capable():
    """The picker renders in list order, so whatever is first is what a reader
    reaches for when the API is unreachable."""
    assert config.KNOWN_MODELS[0][0] == "claude-opus-5-5"

# ---------------------------------------------------------------------------
# Freshness: "the cache has not aged out" is not "this is what the API says"
#
# Reported 2026-09-22: "/model currently doesn't show opus 5.5, so it must be
# using an old cached /model. i want /model to always read the current list
# when possible."  Opus 5.5 had been live for forty minutes; the hourly
# background refresh had last run at 11:59 and cached eleven models, and the
# picker reported itself live because the cache was only forty minutes old.
# ---------------------------------------------------------------------------

def test_a_just_fetched_list_is_live(monkeypatch):
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    config.fetch_available_models()

    assert config.model_list_source() == "live"


def test_a_list_older_than_the_freshness_window_is_not_live(monkeypatch):
    """The exact case that misled: a real list from the API, well inside the
    one-hour cache TTL, but old enough to be missing a model released since."""
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    config.fetch_available_models()
    config._model_cache_at -= config.MODEL_LIST_FRESH_S + 1

    assert config.model_list_source() == "cache"


def test_the_freshness_window_is_far_shorter_than_the_cache_ttl():
    """If these were equal, "live" would go back to meaning "not aged out" and
    the whole distinction would collapse."""
    assert config.MODEL_LIST_FRESH_S < config.MODEL_CACHE_TTL / 10


def test_a_never_fetched_list_is_the_builtin_fallback():
    _reset_cache()

    assert config.model_list_source() == "builtin"
    assert config.model_cache_age() is None


def test_a_cached_list_and_a_builtin_list_are_told_apart(monkeypatch):
    """They need different warnings: "showing the built-in list" is alarming
    and true for one, and simply wrong for the other."""
    _reset_cache()
    assert config.model_list_source() == "builtin"

    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    config.fetch_available_models()
    config._model_cache_at -= config.MODEL_LIST_FRESH_S + 1

    assert config.model_list_source() == "cache"


def test_age_never_goes_negative(monkeypatch):
    """A monotonic stamp from the future (clock adjustment, test fixture) must
    not render as "fetched -3 minutes ago"."""
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    config.fetch_available_models()
    config._model_cache_at += 500

    assert config.model_cache_age() == 0.0


# ---------------------------------------------------------------------------
# What /model reports
# ---------------------------------------------------------------------------

def _show():
    import commands
    from config import parse_args
    from state import State
    res = commands.try_immediate_command("model-show", "", State(), parse_args([]))
    return res.messages[0]["data"]


def test_the_picker_reports_a_fresh_list_as_live(monkeypatch):
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    config.fetch_available_models()

    data = _show()

    assert data["live"] is True
    assert data["source"] == "live"


def test_the_picker_admits_when_the_list_is_merely_cached(monkeypatch):
    """This is the bug: the picker said nothing while showing an hour-old list
    that was missing a released model."""
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    config.fetch_available_models()
    config._model_cache_at -= 1800

    data = _show()

    assert data["live"] is False, "a 30-minute-old list claimed to be live"
    assert data["source"] == "cache"
    assert 1700 < data["age_s"] < 1900, data["age_s"]


def test_the_picker_still_flags_the_builtin_fallback():
    _reset_cache()

    data = _show()

    assert data["source"] == "builtin"
    assert data["age_s"] is None


def test_the_picker_lists_whatever_the_api_returned(monkeypatch):
    """The list itself must come from the fetch, not the hardcoded table."""
    _reset_cache()
    _patch_fetch(monkeypatch, body=_MODELS_BODY)
    config.fetch_available_models()

    ids = [m["id"] for m in _show()["models"]]

    assert ids == ["claude-opus-5", "claude-fable-5"]

# ---------------------------------------------------------------------------
# /model refreshes before it answers
#
# Driven through the real WS router rather than asserted against the source:
# a test that greps for the call survives the mutation that deletes it, which
# is exactly how four wakeup-restore tests passed while the guard they checked
# had been replaced by `if False`.
# ---------------------------------------------------------------------------

import asyncio                                   # noqa: E402
import json                                      # noqa: E402
import time                                      # noqa: E402

import pytest                                    # noqa: E402

import server                                    # noqa: E402
from session_runtime import SessionRuntime       # noqa: E402


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class _FakeBridge:
    def __init__(self):
        self.event_queue = asyncio.Queue()

    async def flush_pending_turn_end(self):
        pass

    async def stop(self):
        pass


@pytest.fixture
def hub():
    from config import parse_args
    from state import State
    cfg = parse_args([])
    st = State()
    rt = SessionRuntime(config=cfg, state=st, rid="R1")
    rt.bridge = _FakeBridge()
    saved = (dict(server.runtimes), server._default_runtime, server.config,
             server.state, server.bridge, dict(server._ws_runtime))
    server.runtimes.clear()
    server.runtimes[rt.rid] = rt
    server._default_runtime = rt
    server.config = cfg
    server.state = st
    server.bridge = rt.bridge
    server._model_refresh_lock = None
    yield rt
    server.runtimes.clear()
    server.runtimes.update(saved[0])
    server._default_runtime = saved[1]
    server.config = saved[2]
    server.state = saved[3]
    server.bridge = saved[4]
    server._ws_runtime.clear()
    server._ws_runtime.update(saved[5])


def _picker(ws):
    for m in ws.sent:
        if m.get("type") == "command_data" and m.get("label") == "model":
            return m["data"]
    return None


def _slash_model(ws):
    return server._handle_ws_message(ws, {"type": "message", "text": "/model"})


def test_slash_model_fetches_before_answering(hub, monkeypatch):
    """The reported request: "i want /model to always read the current list
    when possible."  Previously it rendered whatever the hourly background
    refresh had last cached."""
    _reset_cache()
    calls = []

    def fake_fetch(timeout=6.0):
        calls.append(timeout)
        config._model_cache = [("claude-opus-5-5", "Opus 5.5 — released mid-hour")]
        config._model_cache_at = config.time.monotonic()
        return config._model_cache

    monkeypatch.setattr(server, "fetch_available_models", fake_fetch)
    ws = _FakeWS()

    asyncio.run(_slash_model(ws))

    assert calls, "/model answered without asking the API"
    data = _picker(ws)
    assert [m["id"] for m in data["models"]] == ["claude-opus-5-5"], data
    assert data["live"] is True


def test_a_model_released_mid_hour_shows_up(hub, monkeypatch):
    """The exact scenario: a warm, un-aged cache that predates a release."""
    _reset_cache()
    config._model_cache = [("claude-opus-5", "Opus 5")]
    # 41 minutes old: exactly the reported case — well inside the one-hour
    # cache TTL, so nothing considered it stale, and missing a model that had
    # shipped since.
    config._model_cache_at = config.time.monotonic() - 41 * 60

    def fake_fetch(timeout=6.0):
        config._model_cache = [("claude-opus-5-5", "Opus 5.5"),
                               ("claude-opus-5", "Opus 5")]
        config._model_cache_at = config.time.monotonic()
        return config._model_cache

    monkeypatch.setattr(server, "fetch_available_models", fake_fetch)
    ws = _FakeWS()

    asyncio.run(_slash_model(ws))

    ids = [m["id"] for m in _picker(ws)["models"]]
    assert "claude-opus-5-5" in ids, ids


def test_a_failed_refresh_still_answers_from_the_cache(hub, monkeypatch):
    """Unreachable API must degrade to the previous list, not to an error or a
    hang — and must say which it is showing."""
    _reset_cache()
    config._model_cache = [("claude-opus-5", "Opus 5")]
    config._model_cache_at = config.time.monotonic() - 1800

    def fake_fetch(timeout=6.0):
        return None                      # what an HTTP error looks like

    monkeypatch.setattr(server, "fetch_available_models", fake_fetch)
    ws = _FakeWS()

    asyncio.run(_slash_model(ws))

    data = _picker(ws)
    assert [m["id"] for m in data["models"]] == ["claude-opus-5"]
    assert data["source"] == "cache"
    assert data["live"] is False


def test_a_hanging_api_does_not_hang_the_command(hub, monkeypatch):
    """A fetch that never returns must cost the budget, not the session.

    The fake blocks on an event rather than sleeping, and the event is set
    once the measurement is taken — ``asyncio.run`` joins executor threads on
    the way out, so a fixed sleep would be paid by the test teardown and hide
    what is being measured.
    """
    import threading
    _reset_cache()
    config._model_cache = [("claude-opus-5", "Opus 5")]
    config._model_cache_at = config.time.monotonic() - 600   # past coalescing
    gate = threading.Event()

    def fake_fetch(timeout=6.0):
        gate.wait(10)
        return None

    monkeypatch.setattr(server, "fetch_available_models", fake_fetch)
    monkeypatch.setattr(server, "MODEL_SHOW_FETCH_BUDGET", 0.05)
    ws = _FakeWS()

    async def go():
        started = time.monotonic()
        await _slash_model(ws)
        elapsed = time.monotonic() - started
        gate.set()
        return elapsed

    elapsed = asyncio.run(go())

    assert elapsed < 2.0, f"/model blocked for {elapsed:.1f}s on a hung API"
    assert _picker(ws) is not None, "no list was rendered at all"


def test_a_second_show_moments_later_does_not_refetch(hub, monkeypatch):
    """Coalescing, from the user's side: double-tapping /model is one request.
    The window is seconds, so it cannot hide a release."""
    _reset_cache()
    calls = []

    def fake_fetch(timeout=6.0):
        calls.append(1)
        config._model_cache = [("claude-opus-5", "Opus 5")]
        config._model_cache_at = config.time.monotonic()
        return config._model_cache

    monkeypatch.setattr(server, "fetch_available_models", fake_fetch)

    async def go():
        await _slash_model(_FakeWS())
        await _slash_model(_FakeWS())

    asyncio.run(go())

    assert len(calls) == 1, calls


def test_the_coalescing_window_cannot_hide_a_release():
    """Minutes here would reintroduce the reported bug in miniature."""
    assert server.MODEL_SHOW_COALESCE_S <= 5.0


def test_concurrent_shows_share_one_fetch(hub, monkeypatch):
    """Three tabs running /model at once should make one request, not three.

    A lock alone does not achieve this — it makes them sequential, and three
    sequential requests is barely better than three parallel ones. Whoever
    waits behind the fetch has to take its result.
    """
    _reset_cache()
    calls = []

    def fake_fetch(timeout=6.0):
        calls.append(1)
        time.sleep(0.05)
        config._model_cache = [("claude-opus-5", "Opus 5")]
        config._model_cache_at = config.time.monotonic()
        return config._model_cache

    monkeypatch.setattr(server, "fetch_available_models", fake_fetch)

    async def go():
        await asyncio.gather(*(_slash_model(_FakeWS()) for _ in range(3)))

    asyncio.run(go())

    assert len(calls) == 1, f"{len(calls)} fetches for three concurrent /model"

