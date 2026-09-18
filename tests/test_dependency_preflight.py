"""The startup dependency pre-flight (``server._require_dependencies``).

Reported 2026-08-28:

    "when i ran server.py from wsl the first time, it worked, then after i
     shut the server down and ran server.py from wsl again, it says
     'ModuleNotFoundError: No module named 'uvicorn''"

Both halves of that are explained by the same thing.  The first run did not
"work" in the sense of serving anything: WSL2 shares ``localhost`` with
Windows, so it found the *Windows* hub on the port, opened a session there and
``sys.exit(0)``'d at server.py:3983 — hundreds of lines above ``import
uvicorn``, which lives inside ``main()`` so that a ``--detach`` parent doesn't
pay ~1.5s to import a stack it will never use.  The second run, with that hub
shut down, fell through to actually serving and met a WSL ``python3`` that
simply has none of the dependencies installed.

The path-namespace fix (see test_cwd_namespace.py) makes that the *permanent*
WSL behaviour — a WSL launch now always declines to join a Windows hub — so a
bare ``ModuleNotFoundError`` traceback, with nothing in it to say which of the
several Pythons on this machine lacked the package, would be the standard WSL
experience from here on.  These tests pin the pre-flight that replaces it.
"""

import builtins
import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


REQ_TXT = Path(server.__file__).resolve().parent / "requirements.txt"


def _spec_for(monkeypatch, present):
    """Make ``find_spec`` report exactly the modules in *present* as installed.

    Everything not named in ``_REQUIRED_MODULES`` is passed through to the real
    ``find_spec`` so an unrelated import inside the call under test still works.
    """
    real = importlib.util.find_spec

    def fake(name, package=None):
        if name in server._REQUIRED_MODULES:
            return object() if name in present else None
        return real(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


# --- the check itself ------------------------------------------------------

def test_a_complete_environment_passes_silently(monkeypatch, capsys):
    _spec_for(monkeypatch, set(server._REQUIRED_MODULES))
    server._require_dependencies()  # must not raise SystemExit
    assert capsys.readouterr().out == ""


def test_a_missing_package_stops_the_launch(monkeypatch, capsys):
    present = set(server._REQUIRED_MODULES) - {"uvicorn"}
    _spec_for(monkeypatch, present)

    with pytest.raises(SystemExit) as exc:
        server._require_dependencies()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "uvicorn" in out
    # The four that ARE installed must not be blamed.
    assert "fastapi" not in out.split("Install with")[0]


def test_every_missing_package_is_named_at_once(monkeypatch, capsys):
    """Fixing five missing packages one traceback at a time is five restarts."""
    _spec_for(monkeypatch, set())

    with pytest.raises(SystemExit):
        server._require_dependencies()

    out = capsys.readouterr().out
    for dist in server._REQUIRED_MODULES.values():
        assert dist in out, f"{dist} not reported as missing"
    assert str(len(server._REQUIRED_MODULES)) in out


def test_the_message_names_the_interpreter(monkeypatch, capsys):
    """Which Python lacked it is the entire question when several can run this."""
    _spec_for(monkeypatch, set())

    with pytest.raises(SystemExit):
        server._require_dependencies()

    out = capsys.readouterr().out
    labelled = [ln for ln in out.splitlines() if "Interpreter:" in ln]
    assert labelled, "no interpreter line in:\n" + out
    assert sys.executable in labelled[0]


def test_the_message_gives_a_runnable_install_command(monkeypatch, capsys):
    _spec_for(monkeypatch, set())

    with pytest.raises(SystemExit):
        server._require_dependencies()

    out = capsys.readouterr().out
    assert "-m pip install -r" in out
    assert str(REQ_TXT) in out


def test_the_distribution_name_is_reported_not_the_module_name(monkeypatch, capsys):
    """``pip install claude_agent_sdk`` is not what the user should be told."""
    _spec_for(monkeypatch, set(server._REQUIRED_MODULES) - {"claude_agent_sdk"})

    with pytest.raises(SystemExit):
        server._require_dependencies()

    out = capsys.readouterr().out
    assert "claude-agent-sdk" in out


def test_a_non_windows_launch_is_told_it_is_not_the_windows_python(
        monkeypatch, capsys):
    """The reported case: WSL python3, with a working install on the Windows one."""
    _spec_for(monkeypatch, set())
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(SystemExit):
        server._require_dependencies()

    out = capsys.readouterr().out
    assert "Windows" in out


def test_a_windows_launch_is_not_given_the_wsl_hint(monkeypatch, capsys):
    _spec_for(monkeypatch, set())
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(SystemExit):
        server._require_dependencies()

    out = capsys.readouterr().out
    assert "not the Windows Python" not in out


def test_a_broken_spec_counts_as_missing(monkeypatch, capsys):
    """find_spec raises on a half-installed package; that is 'missing', not a crash."""
    real = importlib.util.find_spec

    def fake(name, package=None):
        if name == "psutil":
            raise ValueError("psutil.__spec__ is None")
        if name in server._REQUIRED_MODULES:
            return object()
        return real(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake)

    with pytest.raises(SystemExit):
        server._require_dependencies()

    assert "psutil" in capsys.readouterr().out


def test_checking_does_not_import_the_dependencies(monkeypatch):
    """The lazy imports are a startup optimisation; the check must not undo them."""
    _spec_for(monkeypatch, set(server._REQUIRED_MODULES))

    imported: list[str] = []
    real_import = builtins.__import__

    def spy(name, *a, **kw):
        imported.append(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", spy)
    server._require_dependencies()

    for mod in server._REQUIRED_MODULES:
        assert mod not in imported, f"{mod} was imported by the check"


# --- what is (and is not) required -----------------------------------------

def test_graphifyy_is_not_required():
    """It is named in a prompt for the agent to run, never imported by us."""
    assert "graphifyy" not in server._REQUIRED_MODULES
    assert "graphifyy" not in server._REQUIRED_MODULES.values()


def test_every_required_package_is_in_requirements_txt():
    """Otherwise the install command we print doesn't fix what we complained about."""
    text = REQ_TXT.read_text(encoding="utf-8").lower()
    for dist in server._REQUIRED_MODULES.values():
        assert dist.lower() in text, f"{dist} missing from requirements.txt"


def test_the_lazily_imported_serving_stack_is_covered():
    """A dependency imported inside main() but unlisted still yields a traceback.

    These two are the ones the lazy-import optimisation hides until the very
    end of startup, which is exactly why they need pre-flighting.
    """
    src = Path(server.__file__).read_text(encoding="utf-8")
    assert "from fastapi import" in src   # still lazily imported...
    assert "import uvicorn" in src
    assert "fastapi" in server._REQUIRED_MODULES   # ...and still pre-flighted
    assert "uvicorn" in server._REQUIRED_MODULES


# --- where the check is called from ----------------------------------------

class _Boom(Exception):
    """Sentinel: the pre-flight ran."""


class _FakeSock:
    def close(self):
        pass


def _no_hub(monkeypatch):
    monkeypatch.setattr(server, "_probe_hub", lambda port: None)


def test_joining_a_hub_does_not_run_the_check(monkeypatch, capsys):
    """A join never imports fastapi/uvicorn, so requiring them would reject
    launches that work fine today."""
    monkeypatch.setattr(sys, "argv", ["server.py"])
    monkeypatch.setattr(server, "_probe_hub", lambda port: {"app": "orchestrator2"})
    monkeypatch.setattr(server, "_launch_into_hub", lambda *a, **kw: "s7")

    def boom():
        raise _Boom()

    monkeypatch.setattr(server, "_require_dependencies", boom)

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code == 0
    assert "Joined running orchestrator2 hub" in capsys.readouterr().out


def test_the_serving_path_runs_the_check(monkeypatch):
    monkeypatch.setattr(
        sys, "argv",
        ["server.py", "--standalone", "--skip-auto-login", "--port", "8299"],
    )
    _no_hub(monkeypatch)
    monkeypatch.setattr(server, "_bind_port", lambda *a, **kw: (_FakeSock(), 8299))

    def boom():
        raise _Boom()

    monkeypatch.setattr(server, "_require_dependencies", boom)

    with pytest.raises(_Boom):
        server.main()


def test_the_check_runs_before_anything_starts_listening(monkeypatch):
    """A failed pre-flight must not leave a splash page serving on the port —
    the user would see a browser tab that never becomes an orchestrator."""
    monkeypatch.setattr(
        sys, "argv",
        ["server.py", "--standalone", "--skip-auto-login", "--port", "8299"],
    )
    _no_hub(monkeypatch)
    monkeypatch.setattr(server, "_bind_port", lambda *a, **kw: (_FakeSock(), 8299))

    started = []
    monkeypatch.setattr(
        server, "_serve_splash_until",
        lambda *a, **kw: started.append(True),
    )

    def boom():
        raise _Boom()

    monkeypatch.setattr(server, "_require_dependencies", boom)

    with pytest.raises(_Boom):
        server.main()

    assert started == [], "splash pre-server started before the pre-flight"
