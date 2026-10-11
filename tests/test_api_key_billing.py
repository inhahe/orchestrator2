"""Sessions billed to an Anthropic API key, and ``/login console``.

Asked 2026-10-10: "Does orchestrator2 support using API keys? if so, tell me
how to use them. if not, implement it?"

It half did.  The SDK starts each session's CLI with the hub's environment,
and, measured against CLI 2.1.280 with fake credentials, a key wins over a
subscription login wherever it comes from::

    subscription login only                  -> OAuth   ("OAuth session expired")
    ANTHROPIC_API_KEY in the environment     -> the key ("Invalid API key")
    subscription login + key in environment  -> the key
    subscription login + key in settings.json "env" -> the key
    subscription login + apiKeyHelper        -> the key (init: apiKeySource)

But the hub judged billing from its own environment alone, so a session on an
account with a key in its settings.json was shown its subscription's plan and
limits, and ``/login`` could only start a subscription sign-in.  Now:

* The CLI's ``init`` message names the credential (``apiKeySource``:
  "ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key", or "none"), and
  that decides what the status bar, ``/status`` and ``/usage`` say.
* ``/login console`` signs the account in to an Anthropic Console account,
  for which the CLI creates an API key (on Windows, ``primaryApiKey`` in
  ``.claude.json``).
* The cost figure is the CLI's running total: ``total_cost_usd`` never resets
  between turns, so adding each turn's up counted the earlier turns again.

The hub's half (``/usage``, the login watcher) is in
tests/test_api_key_hub.py, because importing server.py is slow.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import ResultMessage, SystemMessage  # noqa: E402

import auth                                                 # noqa: E402
import commands                                             # noqa: E402
import plan_usage                                           # noqa: E402
import sdk_bridge                                           # noqa: E402
from config import parse_args                               # noqa: E402
from sdk_bridge import SDKBridge                            # noqa: E402
from state import (                                         # noqa: E402
    State,
    apply_api_key_source,
    apply_rate_limit_info,
    billed_to_api_key,
    detect_account_info,
    global_config_path,
    init_state_from_config,
    state_to_status_dict,
    take_cost,
)

SID = "a11-0000-key"
FAKE_KEY = "sk-ant-api03-not-a-real-key"


@pytest.fixture(autouse=True)
def _no_inherited_credentials(monkeypatch):
    """Keys in the developer's shell must not leak into the tests."""
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)


def _account(tmp_path, *, plan="max", tier="default_claude_max_20x",
             api_key=None):
    """A config dir with a subscription login, and a Console key if given."""
    d = tmp_path / "acct"
    d.mkdir(exist_ok=True)
    if plan:
        (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-fake", "subscriptionType": plan,
            "rateLimitTier": tier}}), encoding="utf-8")
    if api_key is not None:
        (d / ".claude.json").write_text(json.dumps({"primaryApiKey": api_key}),
                                        encoding="utf-8")
    return str(d)


def _subscriber():
    st = State()
    st.is_subscription = True
    st.subscription_plan = "max 20x"
    return st


# --------------------------------------------------------------------------
# What the CLI says it bills to
# --------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["ANTHROPIC_API_KEY", "apiKeyHelper",
                                    "/login managed key"])
def test_a_key_the_cli_names_makes_the_session_key_billed(source):
    st = _subscriber()
    apply_api_key_source(st, source, None)
    assert billed_to_api_key(st)
    assert st.is_subscription is False
    assert st.subscription_plan is None


def test_no_key_leaves_a_subscription_alone():
    st = _subscriber()
    apply_api_key_source(st, "none", None)
    assert not billed_to_api_key(st)
    assert st.is_subscription is True
    assert st.subscription_plan == "max 20x"


def test_a_key_that_goes_away_gives_the_session_its_plan_back(tmp_path, monkeypatch):
    # The plan is read from the session's own account, not the hub's (an
    # empty home here, so reading the wrong one finds no plan).
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    cfg_dir = _account(tmp_path, tier="default_claude_max_5x")
    st = _subscriber()
    apply_api_key_source(st, "ANTHROPIC_API_KEY", cfg_dir)
    apply_api_key_source(st, "none", cfg_dir)
    assert st.is_subscription is True
    assert st.subscription_plan == "max 5x"


@pytest.mark.parametrize("junk", [None, "", 3, ["none"]])
def test_an_init_without_a_source_changes_nothing(junk):
    st = _subscriber()
    apply_api_key_source(st, junk, None)
    assert st.api_key_source is None
    assert st.is_subscription is True


def test_turning_to_a_key_clears_the_subscriptions_limits():
    st = _subscriber()
    st.rate_limit_utils = {"five_hour": 0.9}
    st.rate_limit_status = "rejected"
    apply_api_key_source(st, "ANTHROPIC_API_KEY", None)
    assert st.rate_limit_utils == {}
    assert st.rate_limit_status is None


def test_a_subscription_rate_limit_does_not_make_a_key_session_a_subscription():
    st = State()
    apply_api_key_source(st, "ANTHROPIC_API_KEY", None)
    apply_rate_limit_info(st, {"rate_limit_type": "five_hour", "utilization": 0.5})
    assert st.is_subscription is False


def test_without_a_key_a_subscription_rate_limit_still_marks_the_subscription():
    st = State()
    apply_rate_limit_info(st, {"rate_limit_type": "five_hour", "utilization": 0.5})
    assert st.is_subscription is True


# --------------------------------------------------------------------------
# What the status bar and /status say
# --------------------------------------------------------------------------

def _status(st):
    return state_to_status_dict(st, parse_args([]))


def test_the_status_bar_says_api_and_the_cost_for_a_key_session():
    st = State()
    apply_api_key_source(st, "/login managed key", None)
    st.total_cost_usd = 0.0123
    s = _status(st)
    assert s["plan_field"] == "API $0.0123"
    assert "API key (Anthropic Console sign-in)" in s["plan_title"]
    assert s["rate_field"] is None


def test_the_status_bar_shows_the_plan_for_a_subscription():
    s = _status(_subscriber())
    assert s["plan_field"] == "max 20x"
    assert s["plan_title"] == "Claude subscription plan"


def test_the_status_bar_shows_a_bare_cost_with_neither():
    # A cloud provider, or a token: no key and no subscription.
    st = State()
    st.api_key_source = "none"
    st.total_cost_usd = 1.5
    s = _status(st)
    assert s["plan_field"] == "$1.5000"
    assert s["plan_title"].startswith("Cost:")


def test_status_says_what_the_session_is_billed_to():
    st = State()
    apply_api_key_source(st, "apiKeyHelper", None)
    res = commands._cmd_status("", st, parse_args([]))
    info = res.messages[0]["data"]
    assert info["billing"] == "an API key (apiKeyHelper), per token"


def test_status_says_a_subscription_with_its_plan():
    res = commands._cmd_status("", _subscriber(), parse_args([]))
    assert res.messages[0]["data"]["billing"] == "a Claude subscription (max 20x)"


# --------------------------------------------------------------------------
# The bridge: init, and the cost each result reports
# --------------------------------------------------------------------------

def _bridge():
    cfg = parse_args([])
    st = init_state_from_config(cfg)

    async def bc(_m):
        pass

    return SDKBridge(config=cfg, state=st, broadcaster=bc), st


def _init(source, sid=SID):
    data = {"type": "system", "subtype": "init", "session_id": sid}
    if source is not None:
        data["apiKeySource"] = source
    return SystemMessage(subtype="init", data=data)


def test_the_init_message_sets_what_the_session_bills_to():
    br, st = _bridge()
    asyncio.run(br._handle_system_message(_init("ANTHROPIC_API_KEY"),
                                          during_turn=False))
    assert st.api_key_source == "ANTHROPIC_API_KEY"
    assert st.is_subscription is False


def test_every_init_is_read_not_only_the_first():
    # The CLI sends init every turn, and repeats of the first are otherwise
    # ignored: a key that appears later must still count.
    br, st = _bridge()
    asyncio.run(br._handle_system_message(_init("none"), during_turn=False))
    asyncio.run(br._handle_system_message(_init("apiKeyHelper"), during_turn=False))
    assert st.api_key_source == "apiKeyHelper"


def _result(cost):
    return ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5,
                         is_error=False, num_turns=1, session_id=SID,
                         total_cost_usd=cost)


def test_the_cost_is_the_clis_running_total_not_a_sum():
    st = State()
    take_cost(st, _result(1.0))
    take_cost(st, _result(2.5))
    assert st.total_cost_usd == 2.5


@pytest.mark.parametrize("junk", [None, -1.0, True, "3"])
def test_a_result_without_a_usable_cost_keeps_the_last(junk):
    st = State()
    take_cost(st, _result(2.0))
    take_cost(st, _result(junk))
    assert st.total_cost_usd == 2.0


def test_results_between_turns_take_the_running_total():
    br, st = _bridge()
    asyncio.run(br._handle_async_message(_result(1.0)))
    asyncio.run(br._handle_async_message(_result(2.5)))
    assert st.total_cost_usd == 2.5


def test_a_turns_result_takes_the_running_total(monkeypatch):
    monkeypatch.setattr(sdk_bridge, "TURN_END_SETTLE_S", 0.01)
    br, st = _bridge()
    costs = iter([1.0, 2.5])

    class Client:
        async def query(self, _prompt):
            br.turn_msg_queue.put_nowait(_result(next(costs)))

        async def interrupt(self):  # pragma: no cover
            pass

    br.client = Client()
    asyncio.run(br.run_turn("first"))
    asyncio.run(br.run_turn("second"))
    assert st.total_cost_usd == 2.5


# --------------------------------------------------------------------------
# /usage for a key session
# --------------------------------------------------------------------------

def test_usage_for_a_key_session_is_a_note_naming_the_key(tmp_path):
    report = plan_usage.api_key_report(_account(tmp_path), "ANTHROPIC_API_KEY")
    assert "error" not in report
    assert "API key (ANTHROPIC_API_KEY)" in report["note"]
    assert "plan limits do not apply" in report["note"]
    assert report["account"]["config_dir"].endswith("acct")


# --------------------------------------------------------------------------
# Signing in: which flow, and seeing it finish
# --------------------------------------------------------------------------

def _launched(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(auth.subprocess, "Popen",
                        lambda args, **kw: calls.append(args))
    return calls


@pytest.mark.parametrize("method", ["claudeai", "console"])
def test_login_starts_the_flow_asked_for(monkeypatch, method):
    calls = _launched(monkeypatch)
    ok, _msg = auth.launch_login(method=method, new_window=False)
    assert ok
    assert calls == [["claude", "auth", "login", f"--{method}"]]


def test_login_refuses_a_flow_it_does_not_know(monkeypatch):
    calls = _launched(monkeypatch)
    with pytest.raises(ValueError):
        auth.launch_login(method="apikey")
    assert calls == []


def test_the_fingerprint_digests_the_console_key_never_holds_it(tmp_path):
    cfg_dir = _account(tmp_path, api_key=FAKE_KEY)
    fp = auth.login_fingerprint(cfg_dir)
    assert fp.credentials_mtime is not None
    assert fp.key_digest and FAKE_KEY not in fp.key_digest
    assert len(fp.key_digest) == 64


def test_the_fingerprint_of_an_account_with_no_login(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert auth.login_fingerprint(str(empty)) == (None, None, None)


def test_an_unchanged_config_file_is_not_read_again(monkeypatch, tmp_path):
    # It holds every project's history, and the watcher looks every 2 s.
    cfg_dir = _account(tmp_path, api_key=FAKE_KEY)
    reads = []
    real = auth._primary_key_digest
    monkeypatch.setattr(auth, "_primary_key_digest",
                        lambda p: reads.append(p) or real(p))
    first = auth.login_fingerprint(cfg_dir)
    again = auth.login_fingerprint(cfg_dir, first)
    assert len(reads) == 1 and again.key_digest == first.key_digest
    Path(cfg_dir, ".claude.json").write_text(
        json.dumps({"primaryApiKey": FAKE_KEY + "-new"}), encoding="utf-8")
    changed = auth.login_fingerprint(cfg_dir, again)
    assert len(reads) == 2 and changed.key_digest != first.key_digest


def test_without_a_config_dir_the_cli_keeps_its_config_in_the_home_dir(
        monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert global_config_path(None) == tmp_path / ".claude.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "acct"))
    assert global_config_path(None) == tmp_path / "acct" / ".claude.json"


def test_a_legacy_config_file_wins_as_it_does_for_the_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".config.json").write_text("{}", encoding="utf-8")
    assert global_config_path(None) == tmp_path / ".claude" / ".config.json"


def test_the_default_accounts_details_are_read_where_the_cli_keeps_them(
        monkeypatch, tmp_path):
    # Beside ~/.claude, not in it: a sign-in seen to finish names its account.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "me@example.com"}}), encoding="utf-8")
    info = detect_account_info(None)
    assert info["email"] == "me@example.com"
    assert info["config_dir"] == str(tmp_path / ".claude")


def test_another_account_is_not_given_the_default_ones(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "acct-b").mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "me@example.com"}}), encoding="utf-8")
    assert "email" not in detect_account_info(str(tmp_path / "acct-b"))


def _fp(cred, digest):
    return auth.LoginFingerprint(cred, None, digest)


@pytest.mark.parametrize("before, now, done", [
    ((None, None), (None, None), False),           # nothing yet
    ((10.0, None), (10.0, None), False),           # still the old token file
    ((10.0, None), (20.0, None), True),            # subscription sign-in
    ((None, None), (5.0, None), True),             # first subscription sign-in
    ((10.0, None), (None, "k1"), True),            # Console sign-in
    ((None, "k1"), (None, "k2"), True),            # Console again: a new key
    ((None, "k1"), (None, "k1"), False),           # the same key
    ((10.0, None), (None, None), False),           # signed out on the way
    ((None, "k1"), (None, None), False),           # key removed on the way
])
def test_what_counts_as_a_finished_sign_in(before, now, done):
    assert auth.login_completed(_fp(*before), _fp(*now)) is done


# --------------------------------------------------------------------------
# /login console
# --------------------------------------------------------------------------

def _login(monkeypatch, payload, *, logged_in=True):
    launched = []
    monkeypatch.setattr(auth, "is_logged_in", lambda _d=None: logged_in)
    monkeypatch.setattr(auth, "account_email", lambda _d=None: "me@example.com")

    def launch(*, method="claudeai", new_window=True, config_dir=None):
        launched.append(method)
        return True, "a window has opened."

    monkeypatch.setattr(auth, "launch_login", launch)
    res = commands._cmd_login(payload, State(), parse_args([]))
    text = " ".join(m["data"]["message"] for m in res.messages)
    return res, launched, text


def test_login_console_starts_even_when_signed_in(monkeypatch):
    res, launched, text = _login(monkeypatch, "console")
    assert launched == ["console"]
    assert res.login_launched == "console"
    assert "billed per token" in text


def test_login_console_says_it_replaces_a_subscription(monkeypatch):
    _res, _launched_, text = _login(monkeypatch, "console")
    assert "a Claude subscription included" in text
    assert "nothing changes" in text


def test_plain_login_when_signed_in_starts_nothing(monkeypatch):
    res, launched, _text = _login(monkeypatch, "")
    assert launched == []
    assert res.login_launched is None


def test_login_force_starts_a_subscription_sign_in(monkeypatch):
    res, launched, _text = _login(monkeypatch, "force")
    assert launched == ["claudeai"]
    assert res.login_launched == "claudeai"


def test_a_mistyped_argument_starts_nothing(monkeypatch):
    # Ignored, "consol" would start a subscription sign-in: the opposite.
    res, launched, text = _login(monkeypatch, "consol")
    assert launched == []
    assert res.login_launched is None
    assert "/login console" in text


def test_a_refused_argument_is_not_repeated(monkeypatch):
    # It may be an API key, pasted in hope, and the refusal is drawn in the tab.
    _res, launched, text = _login(monkeypatch, "sk-ant-api03-pasted-in-hope")
    assert launched == []
    assert "sk-ant" not in text
