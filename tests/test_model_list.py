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
    for expected in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5"):
        assert expected in ids, f"{expected} missing from KNOWN_MODELS"
