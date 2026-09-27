"""/usage: the account's plan limits, fetched as Claude Code fetches them.

Asked 2026-09-27: "the claude code TUI has a /usage command that tells you in
ascii all about your usage, including what percentage of the 5-hr limit and
7-day limit's been used and when they reset. can you implement /usage in
orchestrator2?"

These cover the fetch (plan_usage.py) and the command.  The server's handler
is tests/test_usage_hub.py.  The drawing is static/usage.js:
tests/usage.test.js, and tests/reconnect_on_show.test.js for the page wiring.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands                                          # noqa: E402
import config                                            # noqa: E402
import plan_usage                                        # noqa: E402

TOKEN = "sk-ant-oat01-TEST-TOKEN-never-shown"

# The shape the endpoint answered with on 2026-09-27 (trimmed).
USAGE = {
    "five_hour": {"utilization": 18.0,
                  "resets_at": "2026-09-27T21:10:00.132120+00:00"},
    "seven_day": {"utilization": 53.0,
                  "resets_at": "2026-10-03T00:00:00.132145+00:00"},
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": False, "monthly_limit": None,
                    "used_credits": None, "utilization": None},
    "limits": [{"kind": "weekly_scoped", "percent": 0,
                "resets_at": "2026-10-03T00:00:00+00:00",
                "scope": {"model": {"id": None, "display_name": "Fable"}}}],
}


def _account(tmp_path: Path, *, oauth=None, email="someone@example.com",
             raw: str | None = None) -> str:
    """A config dir holding a login record and the account's identity."""
    d = tmp_path / "acct"
    d.mkdir(exist_ok=True)
    if raw is not None:
        (d / ".credentials.json").write_text(raw, encoding="utf-8")
    elif oauth is not None:
        (d / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    if email:
        (d / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": email}}),
            encoding="utf-8")
    return str(d)


def _login(**over):
    rec = {
        "accessToken": TOKEN,
        "refreshToken": "sk-ant-ort01-REFRESH-never-used",
        "expiresAt": int((time.time() + 3600) * 1000),
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x",
    }
    rec.update(over)
    return rec


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Endpoint:
    """Stands in for urlopen: records each request, answers as told."""

    def __init__(self, body=None, error: Exception | None = None):
        self.body = json.dumps(USAGE if body is None else body).encode() \
            if not isinstance(body, bytes) else body
        self.error = error
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.error is not None:
            raise self.error
        return _Resp(self.body)


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        plan_usage.USAGE_URL, code, "err", {}, io.BytesIO(body))


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

def test_usage_is_a_command_of_its_own():
    assert commands.classify("/usage") == ("usage", "")
    assert commands.classify("/USAGE") == ("usage", "")


def test_it_is_not_answered_by_the_synchronous_table():
    # It fetches over the network: answering it there would block the loop.
    assert "usage" not in commands._IMMEDIATE_HANDLERS


def test_it_is_offered_and_listed():
    assert "/usage" in config.SLASH_COMMANDS
    helped = commands._cmd_help("", None, None).messages[0]["content"]
    assert "/usage" in helped


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------

def test_it_asks_the_usage_endpoint_as_claude_code_does(tmp_path):
    cdir = _account(tmp_path, oauth=_login())
    api = _Endpoint()
    out = plan_usage.fetch_usage(cdir, urlopen=api)
    (req, timeout), = api.requests
    assert req.full_url == "https://api.anthropic.com/api/oauth/usage"
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    # urllib stores header names capitalised.
    assert req.get_header("Anthropic-beta") == "oauth-2025-04-20"
    assert timeout == plan_usage.TIMEOUT_S
    assert out["usage"] == USAGE


def test_it_reports_the_plan_from_the_login_record(tmp_path):
    # The endpoint doesn't say which plan; the TUI reads it from the login too
    # (it decides whether there is a Sonnet-only limit).
    cdir = _account(tmp_path, oauth=_login(subscriptionType="pro",
                                           rateLimitTier="default_claude_pro"))
    out = plan_usage.fetch_usage(cdir, urlopen=_Endpoint())
    assert out["subscription_type"] == "pro"
    assert out["rate_limit_tier"] == "default_claude_pro"


def test_a_login_record_without_a_plan_says_so(tmp_path):
    rec = _login()
    del rec["subscriptionType"], rec["rateLimitTier"]
    out = plan_usage.fetch_usage(_account(tmp_path, oauth=rec),
                                 urlopen=_Endpoint())
    assert out["subscription_type"] is None and out["rate_limit_tier"] is None


def test_an_empty_plan_is_no_plan(tmp_path):
    # The page reads "no plan" as "unknown", which is what an empty one is.
    rec = _login(subscriptionType="", rateLimitTier="")
    out = plan_usage.fetch_usage(_account(tmp_path, oauth=rec),
                                 urlopen=_Endpoint())
    assert out["subscription_type"] is None and out["rate_limit_tier"] is None


def test_it_uses_the_sessions_account_not_the_hubs(tmp_path, monkeypatch):
    # The hub's own account is somewhere else entirely.
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": _login(accessToken="HUB-TOKEN")}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(hub))
    cdir = _account(tmp_path, oauth=_login())
    api = _Endpoint()
    plan_usage.fetch_usage(cdir, urlopen=api)
    assert api.requests[0][0].get_header("Authorization") == f"Bearer {TOKEN}"


def test_without_a_session_account_it_uses_the_hubs(tmp_path, monkeypatch):
    cdir = _account(tmp_path, oauth=_login())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", cdir)
    api = _Endpoint()
    plan_usage.fetch_usage(None, urlopen=api)
    assert api.requests, "did not ask"


# --------------------------------------------------------------------------
# What it says instead
# --------------------------------------------------------------------------

@pytest.mark.parametrize("oauth, raw", [
    (None, None),                                    # no credentials file
    (None, "{not json"),                             # unreadable
    (None, json.dumps({"somethingElse": {}})),       # an API-key account
    (None, "[]"),                                    # not an object
    (None, json.dumps({"claudeAiOauth": "not a record"})),
    (_login(accessToken=""), None),
    (_login(accessToken=None), None),
])
def test_no_subscription_login_is_said_without_asking(tmp_path, oauth, raw):
    cdir = _account(tmp_path, oauth=oauth, raw=raw)
    api = _Endpoint()
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=api)
    assert "subscription" in str(err.value) and "/login" in str(err.value)
    assert not api.requests


def test_an_expired_token_is_said_without_asking(tmp_path):
    cdir = _account(tmp_path, oauth=_login(
        expiresAt=int((time.time() - 60) * 1000)))
    api = _Endpoint()
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=api)
    msg = str(err.value)
    assert "expired" in msg
    # ...and what renews it, since the hub will not.
    assert "send a message" in msg and "/login" in msg
    assert not api.requests


def test_a_token_that_expires_later_is_used(tmp_path):
    now = 1_800_000_000.0
    cdir = _account(tmp_path, oauth=_login(expiresAt=int(now * 1000) + 1))
    api = _Endpoint()
    plan_usage.fetch_usage(cdir, now=now, urlopen=api)
    assert api.requests


def test_expiry_is_the_moment_itself(tmp_path):
    now = 1_800_000_000.0
    cdir = _account(tmp_path, oauth=_login(expiresAt=int(now * 1000)))
    with pytest.raises(plan_usage.UsageError):
        plan_usage.fetch_usage(cdir, now=now, urlopen=_Endpoint())


def test_a_record_without_an_expiry_is_tried(tmp_path):
    rec = _login()
    del rec["expiresAt"]
    api = _Endpoint()
    plan_usage.fetch_usage(_account(tmp_path, oauth=rec), urlopen=api)
    assert api.requests


def test_a_login_without_the_profile_scope_is_said_without_asking(tmp_path):
    # Claude Code skips the request for such a token.
    cdir = _account(tmp_path, oauth=_login(scopes=["user:inference"]))
    api = _Endpoint()
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=api)
    assert "user:profile" in str(err.value)
    assert not api.requests


def test_a_record_that_lists_no_scopes_is_tried(tmp_path):
    rec = _login()
    del rec["scopes"]
    api = _Endpoint()
    plan_usage.fetch_usage(_account(tmp_path, oauth=rec), urlopen=api)
    assert api.requests


def test_a_rejected_token_points_at_what_renews_it(tmp_path):
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=_http_error(401)))
    assert "401" in str(err.value) and "/login" in str(err.value)


def test_rate_limiting_says_try_again(tmp_path):
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=_http_error(429)))
    assert str(err.value) == ("Usage endpoint is rate limited. "
                              "Please try again in a moment.")


def test_another_http_error_gives_the_apis_own_reason(tmp_path):
    body = json.dumps({"type": "error", "error": {
        "type": "permission_error",
        "message": "OAuth token does not meet scope\n requirement"}}).encode()
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=_http_error(403, body)))
    assert str(err.value) == ("Failed to load usage data (HTTP 403): "
                              "OAuth token does not meet scope requirement")


def test_a_long_reason_is_cut_short(tmp_path):
    body = json.dumps({"error": {"message": "x" * 500}}).encode()
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=_http_error(500, body)))
    assert str(err.value).endswith(": " + "x" * 200 + "...")


@pytest.mark.parametrize("body", [b"<html>502 Bad Gateway</html>", b"",
                                  b'{"error": "flat"}', b"[1, 2]"])
def test_an_error_page_that_is_not_the_apis_is_not_repeated(tmp_path, body):
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=_http_error(502, body)))
    assert str(err.value) == "Failed to load usage data (HTTP 502)."


def test_an_error_whose_body_cannot_be_read_is_still_said(tmp_path):
    import http.client

    class _CutShort(io.BytesIO):
        def read(self, *a):
            raise http.client.IncompleteRead(b"{")

    err_in = urllib.error.HTTPError(plan_usage.USAGE_URL, 502, "err", {}, _CutShort())
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=err_in))
    assert str(err.value) == "Failed to load usage data (HTTP 502)."


def test_no_network_is_said(tmp_path):
    cdir = _account(tmp_path, oauth=_login())
    err_in = urllib.error.URLError("getaddrinfo failed")
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=err_in))
    assert str(err.value) == "Failed to load usage data: getaddrinfo failed"


def test_a_timeout_is_said(tmp_path):
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=TimeoutError("timed out")))
    assert str(err.value) == "Failed to load usage data: timed out"


def test_a_reply_cut_short_is_said(tmp_path):
    import http.client
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError):
        plan_usage.fetch_usage(
            cdir, urlopen=_Endpoint(error=http.client.IncompleteRead(b"{")))


@pytest.mark.parametrize("body, says", [
    (b"not json", "not JSON"),
    (b"[1, 2]", "unexpected reply"),
])
def test_a_reply_that_is_not_usage_is_said(tmp_path, body, says):
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(body=body))
    assert says in str(err.value)


# --------------------------------------------------------------------------
# The token goes nowhere but the request
# --------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    _http_error(401), _http_error(403, json.dumps(
        {"error": {"message": f"bad token {TOKEN}"}}).encode()),
    urllib.error.URLError("refused"), TimeoutError("timed out"),
    urllib.error.URLError(f"proxy refused Bearer {TOKEN}"),
])
def test_no_error_carries_the_token(tmp_path, error):
    cdir = _account(tmp_path, oauth=_login())
    with pytest.raises(plan_usage.UsageError) as err:
        plan_usage.fetch_usage(cdir, urlopen=_Endpoint(error=error))
    exc = err.value
    # Not in the message -- not even when the server's own words repeat it --
    # and not reachable through a chained exception either: the chain would
    # lead back to the request, which holds it.
    assert TOKEN not in str(exc), str(exc)
    assert exc.__cause__ is None and exc.__suppress_context__


def test_the_report_carries_no_token(tmp_path, monkeypatch):
    cdir = _account(tmp_path, oauth=_login())
    monkeypatch.setattr(plan_usage.urllib.request, "urlopen", _Endpoint())
    report = plan_usage.usage_report(cdir)
    text = json.dumps(report)
    assert TOKEN not in text and "REFRESH" not in text


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def test_the_report_says_whose_limits_they_are(tmp_path, monkeypatch):
    cdir = _account(tmp_path, oauth=_login(), email="someone@example.com")
    monkeypatch.setattr(plan_usage.urllib.request, "urlopen", _Endpoint())
    report = plan_usage.usage_report(cdir)
    assert report["account"] == {"email": "someone@example.com",
                                 "config_dir": cdir}
    assert report["usage"] == USAGE
    assert report["subscription_type"] == "max"


def test_a_failed_report_still_says_whose(tmp_path):
    cdir = _account(tmp_path, oauth=None, email="someone@example.com")
    report = plan_usage.usage_report(cdir)
    assert "subscription" in report["error"]
    assert report["account"]["email"] == "someone@example.com"
    assert "usage" not in report
