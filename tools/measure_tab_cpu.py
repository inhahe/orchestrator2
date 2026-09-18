"""OS-level CPU of a Chromium tree showing an idle orchestrator2 tab.

Deliberately runs **no** CDP profiler: the sampling profiler has its own thread
inside the renderer process, which inflates the very number we're measuring
(it made an idle tab look like 8.9% of a core instead of 1.7%).  This is the
clean version — load the page, sit still, diff per-process CPU by process type.

Four passes isolate *which layer* costs what on a tab that is doing nothing:

  1. as shipped                — the real cost
  2. status/panel render no-op — WS traffic + JSON parse still happen, but the
                                 2s ticker no longer touches the DOM
  3. socket messages dropped   — nothing is processed at all; pure cost of
                                 holding a 50k-element DOM on screen.  The
                                 socket is silenced, *not* closed: closing it
                                 trips app.js's reconnect + full history replay
                                 and swamps the measurement.
  4. about:blank               — baseline for the browser + GPU processes

Usage:  C:/Users/inhah/AppData/Local/Python/pythoncore-3.14-64/python.exe tools/measure_tab_cpu.py [url] [seconds]
"""
from __future__ import annotations

import collections
import sys
import time

import psutil
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8420/?rid=default"
WINDOW = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
SETTLE = 25.0

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _tree(root: psutil.Process) -> dict[int, tuple[str, float]]:
    out: dict[int, tuple[str, float]] = {}
    for proc in [root, *root.children(recursive=True)]:
        try:
            cmd = " ".join(proc.cmdline())
            typ = "browser"
            if "--type=" in cmd:
                typ = cmd.split("--type=", 1)[1].split(" ", 1)[0]
            t = proc.cpu_times()
            out[proc.pid] = (typ, (t.user + t.system) * 1000.0)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue
    return out


def _sample(root: psutil.Process, seconds: float, label: str) -> float:
    before = _tree(root)
    time.sleep(seconds)
    after = _tree(root)
    by_type: collections.Counter[str] = collections.Counter()
    for pid, (typ, cpu) in after.items():
        if pid in before:
            by_type[typ] += cpu - before[pid][1]
    total = sum(by_type.values())
    print(f"\n=== {label} ({seconds:.0f}s) ===")
    for typ, ms in by_type.most_common():
        if ms < 1:
            continue
        print(f"  {typ:26s} {ms:8.0f} ms   ({ms / (seconds * 10):5.1f}% of a core)")
    print(f"  {'TOTAL':26s} {total:8.0f} ms   ({total / (seconds * 10):5.1f}% of a core)")
    return total


def _browser_proc() -> psutil.Process:
    me = psutil.Process()
    for proc in me.children(recursive=True):
        try:
            if "chrome.exe" in proc.name().lower() and "--type=" not in " ".join(proc.cmdline()):
                return proc
        except psutil.Error:
            continue
    raise RuntimeError("could not locate the Chromium browser process")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        root = _browser_proc()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        # app.js keeps its socket in a module-local, so capture every socket
        # the page opens (including reconnects) before any script runs.
        page.add_init_script("""
          window.__o2sockets = [];
          const _WS = window.WebSocket;
          window.WebSocket = function (...a) {
            const s = new _WS(...a);
            window.__o2sockets.push(s);
            return s;
          };
          window.WebSocket.prototype = _WS.prototype;
          Object.assign(window.WebSocket, _WS);
        """)

        print(f"loading {URL} …")
        page.goto(URL, wait_until="load", timeout=60_000)
        print(f"settling {SETTLE:.0f}s for history replay …")
        time.sleep(SETTLE)
        info = page.evaluate(
            "() => ({nodes: document.getElementsByTagName('*').length,"
            " msgs: document.querySelectorAll('#messages .msg').length})")
        print(f"DOM: {info['nodes']:,} elements, {info['msgs']:,} messages")

        shipped = _sample(root, WINDOW, "1. as shipped, visible + idle")

        page.evaluate("""() => {
          window.__o2 = {s: Status.update, p: Panels.update};
          Status.update = () => {};
          Panels.update = () => {};
        }""")
        no_dom = _sample(root, WINDOW, "2. ticker arrives but renders nothing")

        # Silence the socket *without closing it*: closing trips app.js's
        # reconnect path, which re-attaches and replays the whole 2.5 MB
        # history — that re-render is what made an earlier version of this
        # pass report 7% of a core and look like the smoking gun.
        page.evaluate("""() => {
          for (const w of window.__o2sockets || []) { w.onmessage = () => {}; }
        }""")
        time.sleep(3)
        no_ws = _sample(root, WINDOW, "3. messages dropped (static DOM only)")

        page.goto("about:blank")
        time.sleep(4)
        base = _sample(root, WINDOW, "4. about:blank control")

        w = WINDOW * 10
        print("\n>>> breakdown, % of one core:")
        print(f"      total idle cost           {(shipped - base) / w:5.1f}%")
        print(f"      · ticker DOM work         {(shipped - no_dom) / w:5.1f}%")
        print(f"      · WS traffic + parse      {(no_dom - no_ws) / w:5.1f}%")
        print(f"      · holding the DOM on screen {(no_ws - base) / w:5.1f}%")
        browser.close()


if __name__ == "__main__":
    main()
