"""Run the jsdom frontend suites as part of `pytest tests/`.

The bugs these cover live entirely in the browser -- a hidden window that stops
trimming its message list and then crawls when shown, and a WebSocket that dies
while hidden and never comes back -- so they can only be tested in a DOM.
Rather than keep a second, separately-remembered test command, shell out to node
and surface the output here.

Every ``tests/*.test.js`` is picked up automatically, so a new suite needs no
change to this file.  Mutation-checked by ``tools/mutate.py``.

Skips -- rather than fails -- when node or the jsdom dependency is absent, so
the Python suite still runs on a machine that has never had npm run in it.
Install with: npm install   (in the project root)
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITES = sorted(p.name for p in Path(__file__).resolve().parent.glob("*.test.js"))


def _node() -> str | None:
    return shutil.which("node")


def test_frontend_suites_are_discovered():
    """A glob that silently matches nothing would make every test below vanish
    without failing anything."""
    assert SUITES, "no tests/*.test.js found -- the JS suites are not running"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
@pytest.mark.parametrize("suite", SUITES)
def test_frontend_js_suite(suite: str):
    if not (ROOT / "node_modules" / "jsdom").is_dir():
        pytest.skip("jsdom not installed -- run `npm install` in the project root")

    proc = subprocess.run([_node(), str(ROOT / "tests" / suite)], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=300)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, "\n" + output

    # A suite that ran no assertions at all would also exit 0.
    m = re.search(r"(\d+)/(\d+) passed", output)
    assert m, "no '<n>/<n> passed' summary in output:\n" + output
    passed, total = int(m.group(1)), int(m.group(2))
    assert total > 0, "suite reported zero tests:\n" + output
    assert passed == total, "\n" + output
