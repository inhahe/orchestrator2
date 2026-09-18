"""API-error / auth-banner detection tests.

The Claude CLI surfaces a failed API call as an inline banner at the *start* of
the turn output ("API Error: 401 {...}", or a codeless "Not logged in · Please
run /login"), while still closing the turn ``subtype="success"``.  The bridge
detects that to (a) label the turn "error" and (b) set ``state.auth_error`` so
``/login`` knows to re-authenticate.

Regression (2026-07-21): the detection scanned the assistant text with a loose
substring ``search``, so a *debugging conversation that merely discusses* those
strings — "API Error: 401", "Not logged in", "Please run /login" — falsely
tripped the detector, labelling a perfectly good turn "error" and showing the
status as "not authed".  The fix anchors detection to the *start* of the turn
output: a real banner is the whole/leading response, whereas the model quoting
the phrase mid-prose never *begins* its entire response with it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk_bridge import detect_api_error, _AUTH_BANNER_RE


class _FakeResult:
    """Minimal stand-in for a ResultMessage (only ``.result`` is read)."""

    def __init__(self, result=None):
        self.result = result


# --- real API-error banners (leading content) are detected -----------------

def test_real_api_error_banner_detected():
    text = 'API Error: 401 {"type":"error","error":{"message":"Invalid authentication credentials"}}'
    code, sig = detect_api_error(_FakeResult(), text)
    assert code == "401"
    assert sig and sig.startswith("401")


def test_real_api_error_400_detected():
    text = 'API Error: 400 {"type":"error","error":{"message":"bad image"}}'
    code, _ = detect_api_error(_FakeResult(), text)
    assert code == "400"


def test_api_error_with_leading_whitespace_detected():
    text = '\n  API Error: 429 {"error":{"message":"rate limited"}}'
    code, _ = detect_api_error(_FakeResult(), text)
    assert code == "429"


def test_api_error_only_in_result_field():
    code, _ = detect_api_error(_FakeResult('API Error: 500 {"error":{}}'), "")
    assert code == "500"


# --- the model *discussing* the strings must NOT be detected ---------------

def test_quoted_api_error_midprose_not_detected():
    # Exactly the kind of sentence produced while debugging the auth code.
    text = (
        "The turn failed with API Error: 401 and my new code set auth_error. "
        "I fixed the detection so quoting the banner no longer trips it."
    )
    code, sig = detect_api_error(_FakeResult(), text)
    assert code is None and sig is None


def test_long_response_mentioning_banner_not_detected():
    text = (
        "Good — that confirms detection works. When the CLI hits API Error: 401 "
        "it emits the banner as the whole response, so anchoring to the start is "
        "the right discriminator."
    )
    code, _ = detect_api_error(_FakeResult(), text)
    assert code is None


# --- codeless auth banner (anchored) ---------------------------------------

def test_auth_banner_real_refusal_matches():
    assert _AUTH_BANNER_RE.match("Not logged in · Please run /login".lstrip())
    assert _AUTH_BANNER_RE.match("Please run /login".lstrip())
    assert _AUTH_BANNER_RE.match("  Failed to authenticate: OAuth session expired".lstrip())


def test_auth_banner_midprose_does_not_match():
    prose = (
        "I added 'Not logged in' and 'Please run /login' to the matcher, but only "
        "when the whole response begins with them."
    )
    assert not _AUTH_BANNER_RE.match(prose.lstrip())


def test_auth_banner_leading_prose_does_not_match():
    prose = "This turn ended fine even though it mentions Not logged in later on."
    assert not _AUTH_BANNER_RE.match(prose.lstrip())
