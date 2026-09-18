"""List the CSS animations still running on an *idle* orchestrator2 tab.

The renderer burns CPU off the main thread (compositor/raster) when something
animates forever, even if the JS profile says the page is 99% idle.
``document.getAnimations()`` names every one of them, with the element it's
attached to — which is the fastest way from "the renderer is busy" to "this
selector is why".

Usage:  C:/Users/inhah/AppData/Local/Python/pythoncore-3.14-64/python.exe tools/probe_animations.py [url]
"""
from __future__ import annotations

import json
import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8420/?rid=default"
SETTLE = 25.0

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROBE = """
() => {
  const anims = document.getAnimations().map(a => {
    const t = a.effect && a.effect.target;
    const desc = t ? (t.tagName.toLowerCase()
        + (t.id ? '#' + t.id : '')
        + (t.className && typeof t.className === 'string'
           ? '.' + t.className.trim().split(/\\s+/).join('.') : '')) : '(none)';
    return {
      name: a.animationName || (a.constructor && a.constructor.name) || '?',
      state: a.playState,
      target: desc,
      iterations: a.effect ? a.effect.getTiming().iterations : null,
    };
  });
  const counts = {};
  for (const a of anims) {
    const k = a.name + ' | ' + a.state + ' | ' + a.target;
    counts[k] = (counts[k] || 0) + 1;
  }
  return {
    total: anims.length,
    counts,
    nodes: document.getElementsByTagName('*').length,
    messages: document.querySelectorAll('#messages .msg').length,
    canvases: document.querySelectorAll('canvas').length,
    willChange: Array.from(document.querySelectorAll('*'))
      .filter(e => getComputedStyle(e).willChange !== 'auto').length,
  };
}
"""


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        print(f"loading {URL} …")
        page.goto(URL, wait_until="load", timeout=60_000)
        print(f"settling {SETTLE:.0f}s …")
        time.sleep(SETTLE)
        info = page.evaluate(PROBE)
        print(json.dumps({k: v for k, v in info.items() if k != "counts"}, indent=2))
        print(f"\nrunning animations: {info['total']}")
        for k, n in sorted(info["counts"].items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}  {k}")
        browser.close()


if __name__ == "__main__":
    main()
