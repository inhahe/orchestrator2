"""Claude account authentication helpers.

Thin wrappers around the ``claude auth`` CLI so the orchestrator can detect
whether the active config dir is signed in and kick off the same OAuth login
flows Claude Code uses: a Claude subscription, or an Anthropic Console
account billed per token.  All calls honour the ``CLAUDE_CONFIG_DIR`` that
``server.main()`` has already pinned, so login lands in the same session store
the SDK reads from.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("orchestrator2.auth")


def find_claude_cli() -> str | None:
    """Locate the ``claude`` CLI executable.

    Prefers whatever is on PATH; falls back to the common per-user install
    location (``~/.local/bin/claude[.exe]``).  Returns None if not found.
    """
    exe = shutil.which("claude")
    if exe:
        return exe
    name = "claude.exe" if sys.platform == "win32" else "claude"
    candidate = Path.home() / ".local" / "bin" / name
    if candidate.exists():
        return str(candidate)
    return None


def _auth_env(config_dir: str | None) -> dict | None:
    """Build a subprocess env pinned to ``config_dir``'s Claude account.

    Returns ``None`` (inherit the current environment) when no override is
    given, else a copy of ``os.environ`` with ``CLAUDE_CONFIG_DIR`` set — so a
    cross-account hub runtime checks / logs in the *right* account instead of
    the hub process's own env account.
    """
    if not config_dir:
        return None
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).resolve())
    return env


def auth_status(config_dir: str | None = None) -> dict:
    """Return ``claude auth status --json`` as a dict (empty on failure).

    Runs under ``config_dir``'s account when given, else the current process
    environment's ``CLAUDE_CONFIG_DIR``.  Keys include ``loggedIn`` (bool),
    ``email``, ``authMethod``, ``subscriptionType``.
    """
    cli = find_claude_cli()
    if not cli:
        return {}
    try:
        out = subprocess.run(
            [cli, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=20,
            env=_auth_env(config_dir),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("auth status failed: %s", exc)
        return {}
    body = (out.stdout or "").strip()
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _has_credentials_file(config_dir: str | None = None) -> bool:
    """Fast check: an OAuth access token in ``<config-dir>/.credentials.json``.

    An expired token is fine — the CLI refreshes it via the stored refresh
    token — so a present token means "effectively signed in".  ``config_dir``
    (a cross-account runtime's dir) overrides the process env.
    """
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(base if base else Path.home() / ".claude") / ".credentials.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return bool(isinstance(oauth, dict) and oauth.get("accessToken"))


def _global_config_path(config_dir: str | None) -> Path:
    """The CLI's ``.claude.json`` for ``config_dir``'s account.

    Inside the config dir when one is named; otherwise in the home directory,
    beside ``~/.claude`` rather than in it, as the CLI keeps it.
    """
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def login_fingerprint(config_dir: str | None = None) -> tuple[float | None, str | None]:
    """What a completed sign-in changes, for watching one finish.

    A subscription sign-in rewrites ``.credentials.json``.  A Console sign-in
    writes no subscription token: the CLI creates an API key and keeps it in
    ``.claude.json`` as ``primaryApiKey``.  Returns that file's mtime and a
    digest of that key, each None when absent.  The key itself is read only
    to be digested.
    """
    try:
        with open(_global_config_path(config_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    key = data.get("primaryApiKey") if isinstance(data, dict) else None
    digest = (hashlib.sha256(key.encode("utf-8")).hexdigest()
              if isinstance(key, str) and key else None)
    return credentials_mtime(config_dir), digest


def login_completed(before: tuple[float | None, str | None],
                    now: tuple[float | None, str | None]) -> bool:
    """Whether a sign-in finished between two ``login_fingerprint`` readings.

    Either kind writes something new: a subscription token file newer than
    before, or a Console API key that differs from before.  What a sign-in
    removes on its way (it signs the old login out first) is not completion.
    """
    cred_before, key_before = before
    cred_now, key_now = now
    if cred_now is not None and (cred_before is None or cred_now > cred_before):
        return True
    return key_now is not None and key_now != key_before


def credentials_mtime(config_dir: str | None = None) -> float | None:
    """Return the mtime of ``<config-dir>/.credentials.json``, or None.

    The interactive login flow rewrites this file when OAuth completes, so a
    watcher can poll for the mtime advancing to know sign-in finished — more
    reliable than ``claude auth status``, which reports ``loggedIn:true`` even
    for a dead session.
    """
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(base if base else Path.home() / ".claude") / ".credentials.json"
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def is_logged_in(config_dir: str | None = None) -> bool:
    """True when ``config_dir``'s account has valid Claude credentials.

    Fast path first: the credentials file is present in the vast majority of
    (signed-in) startups, so we avoid spawning a ``claude auth status``
    subprocess on the hot path.  Only when no token is on disk do we ask the
    CLI authoritatively (handles unusual auth setups, e.g. apiKeyHelper).
    """
    if _has_credentials_file(config_dir):
        return True
    st = auth_status(config_dir)
    if isinstance(st.get("loggedIn"), bool):
        return st["loggedIn"]
    return False


def account_email(config_dir: str | None = None) -> str | None:
    """Return the signed-in account email for ``config_dir``, or None."""
    st = auth_status(config_dir)
    email = st.get("email")
    return email if isinstance(email, str) and email else None


LOGIN_METHODS = ("claudeai", "console")


def launch_login(*, method: str = "claudeai", new_window: bool = True,
                 config_dir: str | None = None) -> tuple[bool, str]:
    """Start ``claude auth login``, the browser sign-in Claude Code uses.

    ``method`` is the kind of account: ``"claudeai"``, a Claude subscription,
    or ``"console"``, an Anthropic Console account, for which the CLI creates
    an API key billed per token.  Either replaces the account's current login,
    once the browser sign-in completes.  On Windows the CLI gets a visible
    console of its own (``new_window``) for any prompt it shows.
    ``config_dir`` pins the login to a specific account (a cross-account
    runtime's dir).  Returns ``(ok, message)``.
    """
    if method not in LOGIN_METHODS:
        raise ValueError(f"unknown login method {method!r}")
    cli = find_claude_cli()
    if not cli:
        return False, (
            "could not find the `claude` CLI to start login. Install Claude "
            f"Code or add it to PATH, then run `claude auth login --{method}` "
            "manually."
        )
    args = [cli, "auth", "login", f"--{method}"]
    kwargs: dict = {}
    env = _auth_env(config_dir)
    if env is not None:
        kwargs["env"] = env
    try:
        if sys.platform == "win32" and new_window:
            # New visible console so the interactive login is usable even when
            # the orchestrator itself runs windowless (--detach child).
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            subprocess.Popen(args, **kwargs)
        else:
            subprocess.Popen(args, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"failed to launch login: {exc}"
    if method == "console":
        return True, ("an Anthropic Console login window has opened — complete "
                      "sign-in there.")
    return True, "a Claude login window has opened — complete sign-in there."
