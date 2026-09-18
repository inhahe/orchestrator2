"""Name every DOM mutation an *idle* orchestrator2 tab still makes.

Change-detection guards are easy to get subtly wrong (a guard that returns
*after* the write, a `textContent` assignment of an identical string, a
`classList.toggle` that flips nothing).  Rather than reason about them, watch
the document: a MutationObserver reports what actually changed, and grouping by
target tells you which renderer is responsible.

Usage:  C:/Users/inhah/AppData/Local/Python/pythoncore-3.14-64/python.exe tools/probe_mutations.py [url] [seconds]
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8420/?rid=default"
WINDOW = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
SETTLE = 25.0

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

INSTALL = """
() => {
  window.__mut = {};
  const desc = (n) => {
    if (!n) return '(null)';
    if (n.nodeType === 3) return desc(n.parentNode) + ' →#text';
    if (n.nodeType !== 1) return 'node(' + n.nodeType + ')';
    let s = n.tagName.toLowerCase();
    if (n.id) s += '#' + n.id;
    else if (n.className && typeof n.className === 'string')
      s += '.' + n.className.trim().split(/\\s+/).slice(0, 2).join('.');
    return s;
  };
  const obs = new MutationObserver((records) => {
    for (const r of records) {
      const key = r.type + ' ' + (r.attributeName || '') + ' @ ' + desc(r.target);
      window.__mut[key] = (window.__mut[key] || 0) + 1;
    }
  });
  obs.observe(document.documentElement, {
    childList: true, subtree: true, attributes: true,
    characterData: true, attributeOldValue: false,
  });
  window.__mutStop = () => obs.disconnect();
}
"""


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        print(f"loading {URL} …")
        page.goto(URL, wait_until="load", timeout=60_000)
        print(f"settling {SETTLE:.0f}s for history replay …")
        time.sleep(SETTLE)

        state = page.evaluate(
            "() => (document.getElementById('status-state')||{}).textContent")
        print(f"status bar says: {state!r}")

        page.evaluate(INSTALL)
        print(f"watching {WINDOW:.0f}s of idle …")
        time.sleep(WINDOW)
        muts = page.evaluate("() => { window.__mutStop(); return window.__mut; }")

        total = sum(muts.values())
        print(f"\n=== {total} DOM mutations in {WINDOW:.0f}s "
              f"({total / WINDOW:.1f}/s) ===")
        for key, n in sorted(muts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:6d}  {key}")
        if not muts:
            print("  (none — the idle tab is fully quiet)")
        browser.close()


if __name__ == "__main__":
    main()
