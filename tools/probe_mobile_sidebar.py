#!/usr/bin/env python3
"""Render the real page at a phone viewport and ask whether the panels are reachable.

Reported 2026-09-14: "on mobile, i can't see any of the side panes including
queued prompts."  The CSS and the JS both *look* correct -- the sidebar is a
fixed overlay toggled by a gear button that the media query un-hides -- so the
answer is not in reading them.  This measures instead: is the gear on screen,
does tapping it put the sidebar on screen, and is the sidebar's content
actually within the viewport once it is.

Run by hand (needs Playwright and a browser):

    D:/python314/python.exe tools/probe_mobile_sidebar.py
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Pixel-8-ish.  Anything at or under the 768px breakpoint exercises the same
# rules; the point is a real phone width, not a particular phone.
PHONE = {"width": 412, "height": 915}
PHONE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")

# Stand-ins for the modules app.js talks to, so init() completes without a
# server.  Only what the sidebar path touches.
# Nothing is stubbed: the probe points at a live hub, so the socket connects,
# a session attaches and the lobby hides -- which is the state the report is
# about.  A stubbed socket leaves the tab in *landing* mode with the lobby
# covering everything, which is a different (and, as it turns out, also
# interesting) situation.
STUBS = ""


def _report(page):
    return page.evaluate("""() => {
      const vw = window.innerWidth, vh = window.innerHeight;
      const box = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return {
          w: Math.round(r.width), h: Math.round(r.height),
          x: Math.round(r.left), y: Math.round(r.top),
          display: cs.display, visibility: cs.visibility,
          opacity: cs.opacity, zIndex: cs.zIndex,
          onScreen: r.width > 0 && r.height > 0 &&
                    r.right > 0 && r.left < vw &&
                    r.bottom > 0 && r.top < vh,
        };
      };
      const sidebar = document.getElementById('sidebar');
      return {
        vw, vh,
        matchesMobileQuery: window.matchMedia('(max-width: 768px)').matches,
        gear: box(document.getElementById('sidebar-toggle')),
        sidebar: box(sidebar),
        sidebarClasses: sidebar ? sidebar.className : null,
        backdropExists: !!document.getElementById('sidebar-backdrop'),
        queuePanel: box(document.getElementById('panel-queue')
                        || document.querySelector('.panel')),
        panelCount: document.querySelectorAll('#sidebar .panel').length,
      };
    }""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8420/",
                    help="Page to load; pass /?rid=<rid> to land in a session.")
    ap.add_argument("--png", default=str(
        Path(tempfile.gettempdir()) / "orch2_mobile_sidebar.png"))
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("needs Playwright:  pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    # Serve from a file:// dir so the /static/ links resolve.
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport=PHONE, user_agent=PHONE_UA,
                                  device_scale_factor=2, is_mobile=True,
                                  has_touch=True)
        page = ctx.new_page()
        page.route("**/*", lambda route: route.continue_())
        page.add_init_script(STUBS)
        errors = []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(args.url, wait_until="domcontentloaded", timeout=15000)
        # Attaching means a socket, a session_list and a history replay; on a
        # hub with several large sessions that is seconds, not milliseconds.
        for _ in range(40):
            page.wait_for_timeout(500)
            if page.evaluate("() => document.getElementById('lobby')"
                             "        .classList.contains('hidden')"):
                break
        before = _report(page)
        print("viewport             :", before["vw"], "x", before["vh"])
        print("max-width:768 matches:", before["matchesMobileQuery"])
        print("gear button          :", before["gear"])
        print("sidebar (closed)     :", before["sidebar"])
        print("backdrop created     :", before["backdropExists"])
        print("panels in sidebar    :", before["panelCount"])

        # The status bar scrolls horizontally on a phone; anything inside it
        # that is not pinned can be scrolled out of reach.
        bar = page.evaluate("""() => {
          const b = document.getElementById('status-bar');
          if (!b) return null;
          b.scrollLeft = b.scrollWidth;            // scroll to the far end
          const g = document.getElementById('sidebar-toggle');
          const s = document.getElementById('sessions-btn');
          const vis = (el) => {
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: Math.round(r.left), w: Math.round(r.width),
                    onScreen: r.right > 0 && r.left < window.innerWidth};
          };
          return {clientW: b.clientWidth, scrollW: b.scrollWidth,
                  overflows: b.scrollWidth > b.clientWidth + 1,
                  gearAfterScroll: vis(g), sessionsAfterScroll: vis(s)};
        }""")
        print("status bar           :", bar)
        page.screenshot(path=args.png.replace(".png", "_closed.png"))

        print("lobby visible        :", page.evaluate(
            "() => { const l = document.getElementById('lobby');"
            "        return l ? !l.classList.contains('hidden') : null; }"))
        if before["gear"] and before["gear"]["onScreen"]:
            page.click("#sidebar-toggle", timeout=5000)
            page.wait_for_timeout(400)
            after = _report(page)
            print()
            print("after tapping the gear:")
            print("  sidebar classes    :", after["sidebarClasses"])
            print("  sidebar            :", after["sidebar"])
            print("  a panel            :", after["queuePanel"])
            page.screenshot(path=args.png)
            print("screenshot:", args.png)
        else:
            print("\n!! the gear is not on screen, so the panels are unreachable")
        if errors:
            print("\nconsole:")
            for e in errors[:12]:
                print("  ", e[:160])
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
