"""Which Claude account -- config dir -- this process and its CLIs work in.

The CLI reads ``CLAUDE_CONFIG_DIR`` for two things: where an account's files
are (``projects/``, credentials, ``settings.json``), which is
``$CLAUDE_CONFIG_DIR`` or ``~/.claude``; and where its *global* config is
(user-scope MCP servers, trusted projects, ``oauthAccount``), which is
``$CLAUDE_CONFIG_DIR/.claude.json`` when the variable is set and
``~/.claude.json`` when it is not.  So the default account named outright,
``CLAUDE_CONFIG_DIR=~/.claude``, is the same account with a second, separate
global config -- not the one a plain ``claude`` uses.  A CLI for the default
account must be started with the variable unset.

The SDK starts a CLI with this process's environment plus what a session
adds; it cannot take a variable away.  So the hub keeps no
``CLAUDE_CONFIG_DIR`` of its own: ``adopt_from_environment`` takes it out at
startup and keeps it here, every lookup that means "the hub's account" asks
``hub_account()``, and each CLI is given the variable only for an account that
is not the default (``cli_config_dir``, ``child_env``).  A process of ours
that needs the account -- a picker, a ``--detach`` or restart child -- gets it
the same way, and adopts it in turn.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Set by ``adopt_from_environment``.  Until then -- a tool, a test -- the
#: environment is the account, as it is for the CLI.
_adopted = False
_hub: str | None = None


def default_dir() -> Path:
    """The default account's config dir, ``~/.claude``."""
    return Path.home() / ".claude"


def _same_path(a: Path, b: Path) -> bool:
    try:
        a, b = a.resolve(), b.resolve()
    except OSError:
        a, b = a.absolute(), b.absolute()
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def normalize(config_dir: str | None) -> str | None:
    """Account *config_dir* as a CLI should be given it: its resolved path,
    or None for the default account -- unset, or ``~/.claude`` by name."""
    if not config_dir or not str(config_dir).strip():
        return None
    path = Path(config_dir).expanduser()
    if _same_path(path, default_dir()):
        return None
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def adopt_from_environment(given: str | None = None) -> str | None:
    """Make this process's account its own, out of its environment.

    *given* is ``--config-dir``, which wins over ``CLAUDE_CONFIG_DIR``.  The
    variable is removed either way, so no CLI inherits it.  Returns the
    account as it was given, resolved -- the default named outright included
    -- or None when none was.  Called once, at startup, before any CLI or
    session lookup.
    """
    inherited = (os.environ.pop("CLAUDE_CONFIG_DIR", None) or "").strip() or None
    chosen = given or inherited
    set_hub_account(chosen)
    if not chosen:
        return None
    try:
        return str(Path(chosen).expanduser().resolve())
    except OSError:
        return str(Path(chosen).expanduser().absolute())


def set_hub_account(config_dir: str | None) -> None:
    """Make *config_dir* (None: the default) this process's account: a
    launch that follows its session to another account works there."""
    global _adopted, _hub
    _hub = normalize(config_dir)
    _adopted = True


def hub_account() -> str | None:
    """The account this process works in when no session's own is given:
    its config dir, or None for the default.  Before
    ``adopt_from_environment``, the environment's, as given."""
    if _adopted:
        return _hub
    return (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip() or None


def account_dir(config_dir: str | None = None) -> Path:
    """Where account *config_dir* (None: the hub's) keeps its files."""
    base = config_dir or hub_account()
    return Path(base) if base else default_dir()


def cli_config_dir(config_dir: str | None = None) -> str | None:
    """``CLAUDE_CONFIG_DIR`` for a CLI on account *config_dir* (None: the
    hub's); None means the CLI must not be given one."""
    return normalize(config_dir or hub_account())


def child_env(config_dir: str | None = None,
              base: dict[str, str] | None = None) -> dict[str, str]:
    """An environment for a child process on account *config_dir* (None: the
    hub's) -- a ``claude``, or one of ours: *base* (default: this process's)
    with ``CLAUDE_CONFIG_DIR`` set for that account, or removed for the
    default."""
    env = dict(os.environ if base is None else base)
    env.pop("CLAUDE_CONFIG_DIR", None)
    cli_dir = cli_config_dir(config_dir)
    if cli_dir:
        env["CLAUDE_CONFIG_DIR"] = cli_dir
    return env
