"""The chat page runs only its own script files.

It renders what models write, and a model can be led by what it reads into
writing markup: a ``[text](javascript:...)`` link got through once (2026-10-23
review).  So the page is served under a Content-Security-Policy that lets it run
only the script files it loads -- no inline script, no ``javascript:`` URL, no
event handler written into the page.

The policy is only safe while the page needs none of those itself, so that is
pinned too.  And two error pages that repeated the URL's own text unescaped
("session not found: <id>") escape it now: a crafted link ran script in them.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                         # noqa: E402
from starlette.requests import Request               # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "static"


@pytest.fixture(autouse=True)
def response_classes(monkeypatch):
    """What building the app puts in server's globals (it imports them late,
    to start fast), without building it."""
    from starlette import responses
    for name in ("HTMLResponse", "RedirectResponse"):
        monkeypatch.setattr(server, name, getattr(responses, name), raising=False)


def _get(query: str = ""):
    """The response to GET /?*query*."""
    req = Request({"type": "http", "method": "GET", "path": "/",
                   "query_string": query.encode(), "headers": []})
    return asyncio.run(server.index(req))


def test_the_chat_page_runs_only_its_own_scripts(monkeypatch):
    monkeypatch.setattr(server, "_picker_mode", False)

    policy = _get().headers.get("content-security-policy", "")

    assert "script-src 'self'" in policy, policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy


def _page_scripts() -> list[Path]:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return [STATIC / Path(src).name
            for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)]


def test_the_page_has_no_inline_script_for_the_policy_to_block():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    tags = re.findall(r"<script\b[^>]*>", html, re.I)
    assert tags, "found no script tags at all"
    inline = [t for t in tags if "src=" not in t]
    assert not inline, f"inline script, which the policy blocks: {inline}"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "an inline event handler"
    assert "javascript:" not in html.lower()


@pytest.mark.parametrize("script", _page_scripts(), ids=lambda p: p.name)
def test_nor_do_its_scripts_write_any(script):
    """Handlers in markup a script builds, and code made from strings."""
    src = script.read_text(encoding="utf-8")
    assert not re.search(r"""\son[a-z]+\s*=\s*\\?["'`]""", src), "writes an inline event handler"
    assert not re.search(r"\beval\(|new Function\(", src), "makes code from a string"
    assert not re.search(r"""setTimeout\(\s*["'`]|setInterval\(\s*["'`]""", src), \
        "a timer given a string"


@pytest.mark.parametrize("param", ["resume", "cwd"])
def test_an_error_page_does_not_run_the_urls_markup(monkeypatch, param):
    evil = '<img src=x onerror=alert(1)>'

    async def refuse(**kw):
        return False, f"session not found: {kw.get(param)}"
    monkeypatch.setattr(server, "_reconfigure", refuse)

    body = _get(f"{param}={evil}").body.decode("utf-8")

    assert "<img" not in body, body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body, body
