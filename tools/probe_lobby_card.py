#!/usr/bin/env python3
"""Render the lobby's running cards in a real browser and measure the title.

jsdom does no layout, so `tests/lobby_running_card.test.js` can prove the
markup is right and still not tell you whether the title *fits*.  That was the
whole complaint ("its title mostly blocked by other things on the same line"),
so it wants a real engine: this loads the real `styles.css` and the real
`Lobby._runningCard` output at the real card width (`.lobby-inner` is 920px
max, the grid is `repeat(auto-fill, minmax(260px, 1fr))`, so three across) and
reports, per card, whether `.lobby-card-title` is being clipped
(`scrollWidth > clientWidth`).

Run by hand; not part of the suite (it needs Playwright and a browser):

    D:/python314/python.exe tools/probe_lobby_card.py [--png out.png]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The reported case plus an ordinary neighbour, and a deliberately long title
# to check the elision path still works when it is genuinely needed.
CARDS = [
    {"rid": "s3", "session_id": "aaaa1111", "title": "OSc",
     "cwd": "D:\\visual studio projects\\os",
     "account": "C:\\Users\\me\\.claude-account-c",
     "busy": False, "viewers": 1, "last_activity": 0},
    # Foreign, owning hub unreachable: the *unknown* dot (hollow ring), with
    # no viewer count, because neither is knowable from here.
    {"rid": None, "session_id": "bbbb2222", "title": "Good Photons",
     "cwd": "D:\\visual studio projects\\forward raytracer",
     "account": "C:\\Users\\me\\.claude-account-b",
     "busy": None, "viewers": None, "live_known": False, "last_activity": 0,
     "foreign": True, "port": 63978, "holder_pid": 4242,
     "started": "2026-09-06 10:00"},
    # Foreign, but the owning hub answered: real busy dot, real viewer count.
    {"rid": None, "session_id": "eeee5555", "title": "OSb",
     "cwd": "D:\\visual studio projects\\os",
     "account": "C:\\Users\\me\\.claude-account-b",
     "busy": True, "viewers": 1, "live_known": True, "last_activity": 0,
     "foreign": True, "port": 51842, "holder_pid": 4343,
     "started": "2026-09-06 09:00"},
    {"rid": "s2", "session_id": "cccc3333", "title": "orchestrator2c",
     "cwd": "D:\\visual studio projects\\orchestrator2",
     "account": "C:\\Users\\me\\.claude-account-c",
     "busy": True, "viewers": 0, "last_activity": 0},
    {"rid": "s9", "session_id": "dddd4444",
     "title": "A deliberately very long session title that cannot possibly fit",
     "cwd": "D:\\somewhere",
     "account": "C:\\Users\\me\\.claude", "busy": False, "viewers": 2,
     "last_activity": 0},
]

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>%(css)s</style></head><body>
<div id="lobby"><div class="lobby-inner">
  <section class="lobby-section">
    <div class="lobby-section-head"><h2>Running</h2>
      <span id="lobby-running-count" class="lobby-count">0</span></div>
    <div id="lobby-running" class="lobby-cards"></div>
    <div id="lobby-running-empty" class="lobby-empty hidden"></div>
  </section>
  <div id="lobby-recent"></div><div id="lobby-recent-empty"></div>
  <span id="lobby-recent-count"></span><div id="lobby-recent-loading"></div>
  <input id="lobby-new-cwd"><button id="lobby-new-btn"></button>
  <button id="lobby-close"></button><button id="sessions-btn"></button>
  <button id="lobby-shutdown"></button><button id="lobby-restart"></button>
  <div id="lobby-notice"><span id="lobby-notice-text"></span>
  <button id="lobby-notice-close"></button></div>
</div></div>
<script>
  window.App = { send: function(){}, isConnected: function(){ return true; } };
  window.Util = { formatDuration: function(s){ return s + 's'; } };
</script>
<script>%(lobby)s</script>
<script>
  Lobby.init();
  Lobby.render({ running: %(cards)s, recent: [], recent_pending: false });
</script>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    # Into the temp dir, not the tree: the project has no .gitignore and is
    # mirrored to a public repo, so a screenshot left in `tools/` gets
    # published.
    ap.add_argument("--png", default=str(
        Path(tempfile.gettempdir()) / "orch2_lobby_card.png"))
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--legacy", action="store_true", help=(
        "Undo the three fixes in memory (foreign tag back in the title row, "
        "the null==null 'current' badge back, title back to a zero "
        "flex-basis) to show the probe reproduces the reported clipping.  A "
        "measurement that cannot fail proves nothing."))
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("needs Playwright:  pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
    lobby = (ROOT / "static" / "lobby.js").read_text(encoding="utf-8")
    if args.legacy:
        # All three halves of the original, or the reproduction understates it:
        # in the reported screenshot the title was sharing the row with the
        # foreign tag *and* the bogus "current" badge, which is what took
        # "Good Photons" down to "Goo...".
        old_title_row = (
            "        ${isCurrent ? '<span class=\"lobby-current-tag\">"
            "current</span>' : ''}\n")
        assert old_title_row in lobby
        lobby = lobby.replace(old_title_row, old_title_row + "        ${foreignTag}\n", 1)
        lobby = lobby.replace("    if (foreignTag) metaParts.push(foreignTag);\n", "", 1)
        guarded = "    const isCurrent = !!m.rid && m.rid === _currentRid && !m.foreign;"
        assert guarded in lobby
        lobby = lobby.replace(guarded, "    const isCurrent = m.rid === _currentRid;", 1)
        assert "  flex: 1 1 auto;\n  min-width: 0;\n}" in css
        css = css.replace("  flex: 1 1 auto;\n  min-width: 0;\n}", "  flex: 1;\n}", 1)

    html = PAGE % {
        "css": css,
        "lobby": lobby,
        "cards": json.dumps(CARDS),
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": 700})
        page.set_content(html)
        page.wait_for_selector(".lobby-card")
        rows = page.evaluate("""() =>
          Array.from(document.querySelectorAll('#lobby-running .lobby-card'))
            .map((c) => {
              const t = c.querySelector('.lobby-card-title');
              const top = c.querySelector('.lobby-card-top');
              return {
                title: t.textContent.trim(),
                cardW: Math.round(c.getBoundingClientRect().width),
                titleW: Math.round(t.getBoundingClientRect().width),
                needed: t.scrollWidth,
                clipped: t.scrollWidth > t.clientWidth + 1,
                headerKids: Array.from(top.children)
                  .map((k) => k.className.split(' ')[0]),
                metaH: Math.round(
                  c.querySelector('.lobby-card-meta').getBoundingClientRect().height),
                dot: c.querySelector('.lobby-dot').className
                  .replace('lobby-dot', '').trim(),
                overflows: c.scrollWidth > c.clientWidth + 1,
              };
            })""")
        page.screenshot(path=args.png)
        browser.close()

    bad = 0
    for r in rows:
        flag = ""
        if r["clipped"]:
            flag = "  <-- CLIPPED"
            # A title longer than any card can show is expected to clip; the
            # bug was clipping one that fits.
            if r["needed"] <= r["cardW"] - 40:
                flag = "  <-- CLIPPED, and it would have fit"
                bad += 1
        if r["overflows"]:
            flag += "  <-- CARD OVERFLOWS"
            bad += 1
        print(f'  {r["title"][:44]:<46} card={r["cardW"]:>4} '
              f'title={r["titleW"]:>4} needs={r["needed"]:>4} '
              f'meta_h={r["metaH"]:>3}{flag}')
        print(f'      header: {", ".join(r["headerKids"])}  dot={r["dot"]}')
    print(f"\nscreenshot: {args.png}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
