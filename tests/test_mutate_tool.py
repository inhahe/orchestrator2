"""``tools/mutate.py`` sweeps a copy of the project, never the project.

Reported 2026-10-02: copy_session.py was deleted by accident and restored from
a backup that held a mutant.  The backup had been taken while a sweep had the
file mutated in place.  A live tree has other readers as well: the hub serves
static/ fresh to every tab that loads, and other agents edit it.  The sweep now
copies the project to a scratch directory and mutates only the copy.

These drive the real tool against small projects made for the purpose, so they
check the mechanism, not the targets.  Most put a recorder in place of ``run``
-- what the source looked like each time the suite would have run -- and a few
run real pytest in the copy.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "mutate.py"


def _load():
    """A fresh instance of the tool's module: the tests patch its globals."""
    spec = importlib.util.spec_from_file_location("mutate_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def scratches(tmp_path):
    return tmp_path / "scratches"


@pytest.fixture
def mutate(monkeypatch, scratches):
    mod = _load()
    # Every copy these tests make goes here, never into the real temp dir.
    monkeypatch.setenv("ORCH2_MUTATE_SCRATCH", str(scratches))
    return mod


# --- a project to sweep --------------------------------------------------------

CALC = (
    "LIMIT = 10\n"
    "FLAG_A = False\n"
    "FLAG_B = False\n"
    "\n"
    "\n"
    "def add(a, b):\n"
    "    return a + b\n"
)

TEST_CALC = (
    "import hashlib\n"
    "import os\n"
    "\n"
    "import calc\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert calc.add(2, 2) == 4\n"
    "\n"
    "\n"
    "def test_limit():\n"
    "    assert calc.LIMIT == 10\n"
    "\n"
    "\n"
    "def test_never_both_flags():\n"
    "    assert not (calc.FLAG_A and calc.FLAG_B)\n"
    "\n"
    "\n"
    "def test_the_project_itself_is_the_original():\n"
    "    # Runs inside every mutant's run: the file the sweep was asked about\n"
    "    # must be untouched while its copy is mutated.\n"
    "    live = os.environ.get('LIVE_CALC')\n"
    "    if live:\n"
    "        with open(live, 'rb') as f:\n"
    "            assert hashlib.sha256(f.read()).hexdigest() == os.environ['LIVE_SHA']\n"
)

MUTATIONS = [
    ("add subtracts", "    return a + b\n", "    return a - b\n"),
    ("a comment changes nothing", "LIMIT = 10\n", "LIMIT = 10  # same\n"),
    ("flag A alone is harmless", "FLAG_A = False\n", "FLAG_A = True\n"),
    ("flag B alone is harmless", "FLAG_B = False\n", "FLAG_B = True\n"),
    ("a different limit, the same size", "LIMIT = 10\n", "LIMIT = 11\n"),
    ("an anchor that is not there", "LIMIT = 99\n", "LIMIT = 98\n"),
]

SURVIVORS = [
    "calc: a comment changes nothing",
    "calc: flag A alone is harmless",
    # Survives only if flag A was put back first: both set is caught.
    "calc: flag B alone is harmless",
    "calc: an anchor that is not there  [anchor not unique]",
]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


def _project(root: Path, *, git: bool = True, crlf: bool = False) -> Path:
    """calc.py, a suite for it, and what a copy must leave out."""
    root.mkdir(parents=True)
    nl = "\r\n" if crlf else "\n"
    (root / "calc.py").write_bytes(CALC.replace("\n", nl).encode())
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_bytes(TEST_CALC.encode())
    (root / ".gitignore").write_bytes(b"*.log\n__pycache__/\nnode_modules/\n")
    (root / "notes.log").write_bytes(b"x" * 1000)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "calc.cpython-314.pyc").write_bytes(b"stale")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_bytes(b"module.exports = 1;\n")
    if git:
        if not shutil.which("git"):
            pytest.skip("git is not installed")
        _git(root, "init", "-q")
        _git(root, "add", "calc.py", "tests/test_calc.py", ".gitignore")
    # Untracked but not ignored: part of the project.
    (root / "helper.py").write_bytes(b"# not committed yet\n")
    return root


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under *root*, with its bytes and modification time."""
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def _copies(parent: Path) -> list[Path]:
    return sorted(parent.glob("orchestrator2-mutate-*")) if parent.exists() else []


def _target(mutate, monkeypatch, project: Path, targets=None) -> None:
    monkeypatch.setattr(mutate, "ROOT", project)
    monkeypatch.setattr(mutate, "TARGETS", targets or {
        "calc": ("calc.py", "tests/test_calc.py", MUTATIONS, "pytest")})


def _recorder(mutate, monkeypatch, src_rel="calc.py", *, interrupt_at=None):
    """Put a recorder in place of ``run``: (root, source bytes) at each call.
    Every run passes, unless *interrupt_at* names the call that is Ctrl-C."""
    seen: list[tuple[Path, bytes]] = []

    def fake(runner, test_rel, root, timeout=300.0, *, show_failure=False):
        if interrupt_at is not None and len(seen) == interrupt_at:
            raise KeyboardInterrupt
        seen.append((Path(root), (Path(root) / src_rel).read_bytes()))
        return True

    monkeypatch.setattr(mutate, "run", fake)
    return seen


# --- the copy -------------------------------------------------------------------

def test_the_copy_is_the_project_and_nothing_else(mutate, tmp_path, scratches):
    project = _project(tmp_path / "proj")
    copy = mutate.make_scratch(project)
    assert copy.parent == scratches
    assert copy.name.startswith(f"orchestrator2-mutate-{os.getpid()}-")
    assert _files(copy) == {"calc.py", "tests/test_calc.py", ".gitignore",
                            "helper.py", "node_modules/pkg/index.js"}
    assert (copy / "calc.py").read_bytes() == (project / "calc.py").read_bytes()


def test_without_git_it_leaves_out_the_same_things(mutate, tmp_path, monkeypatch):
    project = _project(tmp_path / "proj", git=False)
    (project / ".git").mkdir()          # not a repository git can read
    (project / ".git" / "junk").write_bytes(b"x")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    copy = mutate.make_scratch(project)
    assert _files(copy) == {"calc.py", "tests/test_calc.py", ".gitignore",
                            "helper.py", "node_modules/pkg/index.js"}


def test_a_copy_that_fails_halfway_is_not_left_behind(mutate, tmp_path, scratches,
                                                       monkeypatch):
    project = _project(tmp_path / "proj")
    real_copy2 = shutil.copy2
    calls = []

    def flaky(src, dst, *a, **kw):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(mutate.shutil, "copy2", flaky)
    with pytest.raises(OSError):
        mutate.make_scratch(project)
    assert _copies(scratches) == []


def test_a_scratch_directory_inside_the_project_is_refused(mutate, tmp_path,
                                                           monkeypatch):
    # git would list the copies as project files, and each copy would hold the
    # last one.
    project = _project(tmp_path / "proj")
    monkeypatch.setenv("ORCH2_MUTATE_SCRATCH", str(project / "scratch"))
    with pytest.raises(SystemExit):
        mutate.make_scratch(project)
    assert not (project / "scratch").exists()


# --- the sweep --------------------------------------------------------------------

def test_the_project_is_never_written(mutate, tmp_path, monkeypatch):
    project = _project(tmp_path / "proj")
    before = _snapshot(project)
    _target(mutate, monkeypatch, project)
    seen = _recorder(mutate, monkeypatch)
    mutate.main(["mutate.py"])
    assert _snapshot(project) == before
    assert seen and all(root != project for root, _ in seen)
    # ...and the mutants were written somewhere: the copy.
    original = (project / "calc.py").read_bytes()
    assert all(src != original for _, src in seen[1:])


def test_each_mutant_is_the_original_with_one_change(mutate, tmp_path, monkeypatch):
    project = _project(tmp_path / "proj")
    _target(mutate, monkeypatch, project)
    seen = _recorder(mutate, monkeypatch)
    mutate.main(["mutate.py"])
    applied = [(old, new) for _n, old, new in MUTATIONS if old in CALC]
    assert [src for _, src in seen] == \
        [CALC.encode()] + [CALC.replace(old, new).encode() for old, new in applied]


def test_line_endings_are_kept(mutate, tmp_path, monkeypatch):
    project = _project(tmp_path / "proj", crlf=True)
    _target(mutate, monkeypatch, project)
    seen = _recorder(mutate, monkeypatch)
    mutate.main(["mutate.py"])
    crlf = CALC.replace("\n", "\r\n")
    assert seen[0][1] == crlf.encode()
    old, new = MUTATIONS[0][1], MUTATIONS[0][2]
    assert seen[1][1] == crlf.replace(old.replace("\n", "\r\n"),
                                      new.replace("\n", "\r\n")).encode()


def test_the_copy_is_put_back_for_the_next_target(mutate, tmp_path, monkeypatch):
    # Several targets test the same file in one copy.
    project = _project(tmp_path / "proj")
    _target(mutate, monkeypatch, project, {
        "first": ("calc.py", "tests/test_calc.py", MUTATIONS[:1], "pytest"),
        "second": ("calc.py", "tests/test_calc.py", MUTATIONS[2:3], "pytest"),
    })
    seen = _recorder(mutate, monkeypatch)
    mutate.main(["mutate.py"])
    baseline_of_second = seen[2][1]
    assert baseline_of_second == CALC.encode()


def test_the_copy_is_deleted_afterwards(mutate, tmp_path, scratches, monkeypatch):
    project = _project(tmp_path / "proj")
    _target(mutate, monkeypatch, project)
    _recorder(mutate, monkeypatch)
    mutate.main(["mutate.py"])
    assert _copies(scratches) == []


def test_the_copy_is_deleted_when_the_sweep_is_interrupted(mutate, tmp_path,
                                                            scratches, monkeypatch):
    project = _project(tmp_path / "proj")
    before = _snapshot(project)
    _target(mutate, monkeypatch, project)
    _recorder(mutate, monkeypatch, interrupt_at=2)
    with pytest.raises(KeyboardInterrupt):
        mutate.main(["mutate.py"])
    assert _copies(scratches) == []
    assert _snapshot(project) == before


def test_a_leftover_mutbak_stops_the_run(mutate, tmp_path, scratches, monkeypatch,
                                          capsys):
    # The old in-place sweep, killed mid-mutation, left the original here and
    # perhaps a mutant in calc.py.  Copying that would sweep the mutant.
    project = _project(tmp_path / "proj")
    (project / "calc.py.mutbak").write_bytes(CALC.encode())
    _target(mutate, monkeypatch, project)
    seen = _recorder(mutate, monkeypatch)
    assert mutate.main(["mutate.py"]) == 2
    assert "calc.py.mutbak" in capsys.readouterr().out
    assert seen == [] and _copies(scratches) == []


# --- copies left by runs that were killed ---------------------------------------

def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_a_killed_runs_copy_is_deleted_and_a_running_ones_kept(mutate, tmp_path,
                                                               scratches):
    abandoned = scratches / f"orchestrator2-mutate-{_dead_pid()}-abc"
    running = scratches / f"orchestrator2-mutate-{os.getpid()}-xyz"
    unrelated = scratches / "something-else"
    for d in (abandoned, running, unrelated):
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"x")
    mutate.make_scratch(_project(tmp_path / "proj"))
    assert not abandoned.exists()
    assert running.exists() and unrelated.exists()


def test_only_scratch_names_carry_a_pid(mutate):
    assert mutate._owner_pid("orchestrator2-mutate-123-abc") == 123
    assert mutate._owner_pid("orchestrator2-mutate-abc") is None
    assert mutate._owner_pid("orchestrator2-mutate-123") is None
    assert mutate._owner_pid("other-123-abc") is None


def test_a_read_only_file_does_not_strand_the_copy(mutate, tmp_path):
    d = tmp_path / "copy"
    d.mkdir()
    f = d / "locked.py"
    f.write_bytes(b"x")
    os.chmod(f, stat.S_IREAD)
    assert mutate._rmtree(d) is True
    assert not d.exists()


# --- real runs in the copy -------------------------------------------------------

def test_a_real_sweep_catches_what_it_should(mutate, tmp_path, scratches,
                                             monkeypatch, capsys):
    project = _project(tmp_path / "proj")
    before = _snapshot(project)
    live = project / "calc.py"
    monkeypatch.setenv("LIVE_CALC", str(live))
    monkeypatch.setenv("LIVE_SHA", hashlib.sha256(live.read_bytes()).hexdigest())
    _target(mutate, monkeypatch, project)
    assert mutate.main(["mutate.py"]) == 1
    out = capsys.readouterr().out
    assert "caught   add subtracts" in out
    assert "caught   a different limit, the same size" in out
    for name in SURVIVORS:
        assert f"  survivor: {name}" in out, out
    assert "2/6 caught overall" in out
    assert _snapshot(project) == before
    assert _copies(scratches) == []


def test_python_compiles_each_run_from_the_source(mutate, tmp_path, monkeypatch):
    # A mutant the same size as the source before it, written within the same
    # second, matches a cached .pyc's mtime-and-size check.
    # The tool must be what turns bytecode off.  Under a sweep this suite runs
    # with it off already, inherited, and so passed whatever the tool did.
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    project = _project(tmp_path / "proj")
    copy = mutate.make_scratch(project)
    assert mutate.run("pytest", "tests/test_calc.py", copy)
    assert not list(copy.rglob("__pycache__")), "the run wrote bytecode"
    src = copy / "calc.py"
    st = src.stat()
    src.write_bytes(src.read_bytes().replace(b"LIMIT = 10", b"LIMIT = 11"))
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert src.stat().st_size == st.st_size
    assert not mutate.run("pytest", "tests/test_calc.py", copy), \
        "the run used the bytecode of the source before"


def test_a_baseline_that_fails_says_why(mutate, tmp_path, monkeypatch, capsys):
    project = _project(tmp_path / "proj")
    (project / "calc.py").write_bytes(CALC.replace("LIMIT = 10", "LIMIT = 12").encode())
    copy = mutate.make_scratch(project)
    assert mutate.sweep("calc", "calc.py", "tests/test_calc.py", MUTATIONS,
                        "pytest", root=copy) == ["<baseline failure>"]
    out = capsys.readouterr().out
    assert "BASELINE FAILS" in out
    assert "test_limit" in out, "nothing said which test failed"
