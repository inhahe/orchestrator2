"""The account's plan limits: what ``/usage`` shows.

Asked 2026-09-27: "the claude code TUI has a /usage command that tells you in
ascii all about your usage, including what percentage of the 5-hr limit and
7-day limit's been used and when they reset. can you implement /usage in
orchestrator2?"

Claude Code reads these from ``GET /api/oauth/usage``. The endpoint reports
how much of the five-hour session limit and of the weekly limits the account
has used, and when each resets. The hub asks the same endpoint as the
*session's* account, so a session on another account (``--config-dir``,
``/move``) shows that account's limits rather than the hub's. The browser
draws the answer (``static/usage.js``), because reset times are shown in the
viewer's time zone.

Only a subscription login (Pro, Max, Team, Enterprise) has plan limits, and
the endpoint accepts only that login's OAuth token. The token is read from the
account's ``.credentials.json`` and is used only as that request's
``Authorization`` header. It is never logged, never put in an error message,
and never sent to the browser.

**The hub never renews the token.** An access token lasts about eight hours,
and the CLI renews it whenever it calls the API and finds it expired. Renewal
rotates the refresh token, and the CLI serialises renewals across its
processes with a lock file in the config dir. A renewal from the hub would not
take that lock, so it could race a session's renewal and sign the account out.
An expired token is therefore reported, together with what renews it.

Why not ask the session's CLI instead? It has a ``get_usage`` control request
that renews the token itself. But on this machine the request did not answer
within 170 s, twice. Alongside the limits it scans the local session
transcripts for its "what's contributing" breakdown, and the caller cannot
turn that part off. The endpoint answers in well under a second.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from state import config_dir_path, detect_account_info

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The subscription API answers an OAuth token only with this beta, as for
# /v1/models (config.fetch_available_models).
OAUTH_BETA = "oauth-2025-04-20"
# Claude Code skips the request for a token without this scope.
PROFILE_SCOPE = "user:profile"
TIMEOUT_S = 10.0

# Shown as the reason, in place of anything from the reply.
_RATE_LIMITED = "Usage endpoint is rate limited. Please try again in a moment."


class UsageError(Exception):
    """What to tell the user instead of their limits."""


def read_oauth(config_dir: str | None) -> dict[str, Any] | None:
    """The account's ``claudeAiOauth`` record, or None when it has none."""
    path = config_dir_path(config_dir) / ".credentials.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else None


def fetch_usage(
    config_dir: str | None,
    *,
    now: float | None = None,
    timeout: float = TIMEOUT_S,
    urlopen: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Return the account's plan limits. This blocks on the network.

    Returns ``{"usage": <the endpoint's JSON>, "subscription_type": ...,
    "rate_limit_tier": ...}``. The last two come from the login record,
    because the endpoint does not report the plan. Raises ``UsageError``
    carrying the message to show instead.
    """
    oauth = read_oauth(config_dir)
    token = oauth.get("accessToken") if oauth else None
    if not isinstance(token, str) or not token:
        raise UsageError(
            "This account has no Claude subscription login. Plan limits exist "
            "only for one (Pro, Max, Team or Enterprise); sign in with /login.")
    scopes = oauth.get("scopes")
    if isinstance(scopes, list) and PROFILE_SCOPE not in scopes:
        raise UsageError(
            "This account's login cannot read usage: it was granted without "
            f"the {PROFILE_SCOPE} scope. Sign in again with /login.")
    expires_ms = oauth.get("expiresAt")
    now_s = time.time() if now is None else now
    if isinstance(expires_ms, (int, float)) and expires_ms / 1000 <= now_s:
        raise UsageError(
            "This account's sign-in token has expired. Claude renews it the "
            "next time a session on this account talks to the API, so send a "
            "message in one and run /usage again. /login renews it too.")

    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Content-Type": "application/json",
        "User-Agent": "orchestrator2",
    })
    opener = urlopen or urllib.request.urlopen
    # ``from None`` on each: these exceptions carry nothing the user needs,
    # and the request they would chain back to holds the token.
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise UsageError(_scrub(_http_error_text(exc), token)) from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # URLError, timeouts, resets, a reply cut short.
        reason = getattr(exc, "reason", None) or exc
        raise UsageError(
            _scrub(f"Failed to load usage data: {reason}", token)) from None
    try:
        body = json.loads(raw)
    except ValueError:
        raise UsageError(
            "Failed to load usage data: the reply was not JSON.") from None
    if not isinstance(body, dict):
        raise UsageError("Failed to load usage data: unexpected reply.")
    return {
        "usage": body,
        "subscription_type": _text(oauth.get("subscriptionType")),
        "rate_limit_tier": _text(oauth.get("rateLimitTier")),
    }


def usage_report(config_dir: str | None) -> dict[str, Any]:
    """Build what ``/usage`` sends the browser.

    The report is the limits or an ``error``, plus which account they are for.
    The account is included either way, because a report without it is
    ambiguous on a hub whose sessions use several accounts. Only a
    ``UsageError`` becomes an ``error`` here; anything else is a bug, and it
    propagates for the caller to log.
    """
    info = detect_account_info(config_dir)
    account = {"email": info.get("email"), "config_dir": info.get("config_dir")}
    try:
        report = fetch_usage(config_dir)
    except UsageError as exc:
        return {"error": str(exc), "account": account}
    report["account"] = account
    return report


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    code = exc.code
    if code == 429:
        return _RATE_LIMITED
    if code == 401:
        return (
            "The API turned down this account's sign-in token (HTTP 401). "
            "Claude renews it the next time a session on this account talks "
            "to the API; if this keeps happening, sign in again with /login.")
    detail = _error_detail(exc)
    return f"Failed to load usage data (HTTP {code})" + (
        f": {detail}" if detail else ".")


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Get the API's own explanation of an error, trimmed to one short line.

    Only the message field of an API error body is used. The body itself
    could be anything, even an HTML page from a proxy.
    """
    try:
        body = json.loads(exc.read() or b"{}")
    except (OSError, ValueError, http.client.HTTPException):
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else None
    if not isinstance(msg, str):
        return ""
    msg = " ".join(msg.split())
    return msg[:200] + ("..." if len(msg) > 200 else "")


def _scrub(text: str, token: str) -> str:
    """Remove the token from text that repeats something the server said."""
    return text.replace(token, "[token]")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
