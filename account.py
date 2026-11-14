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
    """Account *config_dir* as a CLI should be given it: as given, made
    absolute (a child may run elsewhere), or None for the default account --
    unset, or ``~/.claude`` by name."""
    if not config_dir or not str(config_dir).strip():
        return None
    path = Path(config_dir).expanduser()
    if _same_path(path, default_dir()):
        return None
    return str(path if path.is_absolute() else path.absolute())


def same_account(a: str | None, b: str | None) -> bool:
    """Whether accounts *a* and *b* (None: the hub's) are one: the default
    by name and unset are."""
    return _same_path(account_dir(a), account_dir(b))


def adopt_from_environment(given: str | None = None) -> str | None:
    """Make this process's account its own, out of its environment.

    *given* is ``--config-dir``, which wins over ``CLAUDE_CONFIG_DIR``.  The
    variable is removed either way, so no CLI inherits it.  Returns the
    account as it was given, resolved -- the default named outright included
    -- or None when none was.  Called once, at startup, before any CLI or
    session lookup.

    Raises ValueError, saying why, for the account chosen given as a
    relative path, or wrapped in quotes.  A relative one is read against the
    directory the launch ran in, where ``.claude`` is the project's own
    settings folder, which would quietly become an account.  A hub's CLIs
    run in many directories, so a relative account is never what was meant.
    """
    inherited = (os.environ.pop("CLAUDE_CONFIG_DIR", None) or "").strip() or None
    chosen = given or inherited
    if not chosen:
        set_hub_account(None)
        return None
    source = "--config-dir" if given else "CLAUDE_CONFIG_DIR"
    if chosen[0] in "\"'" or chosen[-1] in "\"'":
        raise ValueError(
            f"{source} is {chosen}, quotes included, so it names no folder. "
            "Give it without the quotes"
            + (' (in cmd, put them around the whole assignment: '
               'set "CLAUDE_CONFIG_DIR=C:\\path").'
               if source == "CLAUDE_CONFIG_DIR" else "."))
    path = Path(chosen).expanduser()
    if not path.is_absolute():
        raise ValueError(_relative_account_message(chosen, path, source))
    try:
        path = path.resolve()
    except OSError:
        pass
    set_hub_account(str(path))
    return str(path)


def _is_account(path: Path) -> bool:
    """Whether *path* holds a Claude account: sessions, or a sign-in."""
    return (path / "projects").is_dir() or (path / ".credentials.json").is_file()


def _relative_account_message(chosen: str, path: Path, source: str) -> str:
    """Why the relative account *chosen* (*path*, expanded), from *source*
    (``--config-dir`` or ``CLAUDE_CONFIG_DIR``), is refused, and what was
    probably meant."""
    meant = path.absolute()
    lines = [
        f"{source} is {chosen}, a relative path. Read against the directory "
        f"this launch ran in, that is {meant}, "
        + ("an account, but from another directory the same path would name "
           "another folder." if _is_account(meant) else
           "which is not a Claude account and would become a new, empty one.")
        + " Give the account's full path."]
    # The same name in the home directory: the anchor of a root-relative
    # path (\.claude) would put it at the drive's root instead.
    in_home = Path.home().joinpath(*path.parts[1:] if path.anchor else path.parts)
    if in_home.is_dir() and not _same_path(in_home, meant):
        lines.append(f"Did you mean {in_home}?")
    return "\n".join(lines) + _clear_hint(source)


def _clear_hint(source: str) -> str:
    """For an account that came from the environment: how to take it out."""
    if source != "CLAUDE_CONFIG_DIR":
        return ""
    return ("\nCLAUDE_CONFIG_DIR came from the environment orchestrator2 was "
            "started with. If it should not be set, remove it there: "
            "'set CLAUDE_CONFIG_DIR=' in cmd, "
            "'Remove-Item Env:CLAUDE_CONFIG_DIR' in PowerShell, or, if it is "
            "set for your user, under Environment Variables in the System "
            "settings.")


def account_label(config_dir: str | None = None) -> str:
    """Account *config_dir* (None: the hub's) as a person is shown it: its
    folder's name when it is in the home directory, and its whole path
    anywhere else, where it may well be called ``.claude`` too."""
    d = account_dir(config_dir)
    return d.name if _same_path(d.parent, Path.home()) else str(d)


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
    return Path(base).expanduser() if base else default_dir()


def credentials_path(config_dir: str | None = None) -> Path:
    """Account *config_dir*'s (None: the hub's) subscription login file."""
    return account_dir(config_dir) / ".credentials.json"


def cli_config_dir(config_dir: str | None = None) -> str | None:
    """``CLAUDE_CONFIG_DIR`` for a CLI on account *config_dir* (None: the
    hub's); None means the CLI must not be given one."""
    if not config_dir and _adopted:
        return _hub             # normalized when adopted
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
