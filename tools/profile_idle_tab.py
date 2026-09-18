"""Measure what an *idle* orchestrator2 tab actually spends CPU on.

Opens the real UI in a real Chromium via Playwright, lets the history replay
settle, then takes three independent measurements over a quiet window:

  1. ``Performance.getMetrics`` deltas — splits the time into ScriptDuration /
     RecalcStyleDuration / LayoutDuration / TaskDuration, which tells you
     *whether* the cost is JS, style, or layout before you go hunting.
  2. A CDP sampling profile — attributes the ScriptDuration to individual
     functions (self time), so a runaway timer/observer shows up by name.
  3. OS-level CPU of every process in the Chromium tree, by process type
     (renderer / gpu-process / …).  The page's main thread can be 99% idle
     while the *compositor* burns a core on an animation, and only this
     measurement can see that.

A control pass on ``about:blank`` in the same browser gives the baseline, so
the numbers are a difference rather than an absolute.

Usage:  C:/Users/inhah/AppData/Local/Python/pythoncore-3.14-64/python.exe tools/profile_idle_tab.py [url] [seconds]
"""
from __future__ import annotations

import collections
import json
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8420/?rid=default"
WINDOW = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
SETTLE = 25.0

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

METRICS = (
    "TaskDuration", "ScriptDuration", "LayoutDuration",
    "RecalcStyleDuration", "LayoutCount", "RecalcStyleCount", "Nodes",
    "JSEventListeners", "Timestamp",
)

_PS_TREE = r"""
$root = %d
$seen = @{}; $queue = New-Object System.Collections.Queue
$queue.Enqueue($root)
$all = Get-CimInstance Win32_Process | Group-Object ParentProcessId -AsHashTable -AsString
while($queue.Count -gt 0){
  $id = $queue.Dequeue(); if($seen.ContainsKey($id)){continue}; $seen[$id]=$true
  if($all.ContainsKey("$id")){ foreach($c in $all["$id"]){ $queue.Enqueue([int]$c.ProcessId) } }
}
$out = foreach($id in $seen.Keys){
  $c = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
  if(-not $c){continue}
  $p = Get-Process -Id $id -ErrorAction SilentlyContinue
  if(-not $p){continue}
  $t = if($c.CommandLine -match '--type=([a-z-]+)'){$matches[1]}else{'browser'}
  [pscustomobject]@{ pid=$id; type=$t; cpu=$p.TotalProcessorTime.TotalMilliseconds }
}
$out | ConvertTo-Json -Compress
"""


def _tree_cpu(root_pid: int) -> dict[int, tuple[str, float]]:
    """{pid: (process_type, total_cpu_ms)} for the whole Chromium tree."""
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS_TREE % root_pid],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        data = json.loads(raw) if raw else []
    except Exception as exc:                                # pragma: no cover
        print(f"  (tree CPU sample failed: {exc})")
        return {}
    if isinstance(data, dict):
        data = [data]
    return {int(d["pid"]): (d["type"], float(d["cpu"])) for d in data}


def _report_tree(before, after, wall: float, label: str) -> float:
    by_type: collections.Counter[str] = collections.Counter()
    for pid, (typ, cpu) in after.items():
        if pid in before:
            by_type[typ] += cpu - before[pid][1]
    total = sum(by_type.values())
    print(f"\n=== OS CPU of the Chromium tree — {label} ({wall:.0f}s) ===")
    for typ, ms in by_type.most_common():
        if ms < 1:
            continue
        print(f"  {typ:16s} {ms:8.0f} ms   ({ms / (wall * 10):5.1f}% of a core)")
    print(f"  {'TOTAL':16s} {total:8.0f} ms   ({total / (wall * 10):5.1f}% of a core)")
    return total


def _metrics(cdp) -> dict[str, float]:
    got = cdp.send("Performance.getMetrics")["metrics"]
    return {m["name"]: m["value"] for m in got if m["name"] in METRICS}


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        root_pid = browser.browser_type.executable_path and _browser_pid(browser)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        cdp = page.context.new_cdp_session(page)
        cdp.send("Performance.enable")

        print(f"loading {URL} …")
        page.goto(URL, wait_until="load", timeout=60_000)
        print(f"settling {SETTLE:.0f}s for history replay …")
        time.sleep(SETTLE)

        nodes = _metrics(cdp).get("Nodes", 0)
        print(f"DOM nodes after replay: {nodes:,.0f}")

        before = _metrics(cdp)
        tree_before = _tree_cpu(root_pid)
        cdp.send("Profiler.enable")
        cdp.send("Profiler.setSamplingInterval", {"interval": 200})
        cdp.send("Profiler.start")
        print(f"profiling {WINDOW:.0f}s of idle …")
        time.sleep(WINDOW)
        prof = cdp.send("Profiler.stop")["profile"]
        after = _metrics(cdp)
        tree_after = _tree_cpu(root_pid)

        wall = after["Timestamp"] - before["Timestamp"]
        print(f"\n=== Performance.getMetrics over {wall:.1f}s wall ===")
        for k in ("TaskDuration", "ScriptDuration",
                  "RecalcStyleDuration", "LayoutDuration"):
            d = after.get(k, 0) - before.get(k, 0)
            print(f"  {k:22s} {d * 1000:8.1f} ms   ({d / wall * 100:5.2f}% of a core)")
        for k in ("LayoutCount", "RecalcStyleCount"):
            d = after.get(k, 0) - before.get(k, 0)
            print(f"  {k:22s} {d:8.0f}      ({d / wall:.1f}/s)")
        print(f"  {'JSEventListeners':22s} {after.get('JSEventListeners', 0):8.0f}")

        busy_tree = _report_tree(tree_before, tree_after, WINDOW, "orchestrator2 tab")

        # --- Attribute script time to functions (self time) ---------------
        nodes_by_id = {n["id"]: n for n in prof["nodes"]}
        deltas = prof.get("timeDeltas", [])
        total_us = sum(deltas) or 1
        weighted: collections.Counter[int] = collections.Counter()
        for i, sid in enumerate(prof.get("samples", [])):
            weighted[sid] += deltas[i] if i < len(deltas) else 0

        print(f"\n=== CDP sampling profile ({total_us / 1e6:.1f}s of samples) ===")
        for nid, us in weighted.most_common(25):
            cf = nodes_by_id[nid]["callFrame"]
            name = cf.get("functionName") or "(anonymous)"
            url = (cf.get("url") or "").rsplit("/", 1)[-1]
            line = cf.get("lineNumber", -1) + 1
            pct = us / total_us * 100
            if pct < 0.05:
                continue
            where = f"{url}:{line}" if url else ""
            print(f"  {us / 1000:8.1f} ms  {pct:5.2f}%  {name:34s} {where}")

        # --- Control: same browser, blank page ----------------------------
        print("\ncontrol: navigating to about:blank …")
        page.goto("about:blank")
        time.sleep(3)
        ctl_before = _tree_cpu(root_pid)
        time.sleep(WINDOW)
        ctl_after = _tree_cpu(root_pid)
        base_tree = _report_tree(ctl_before, ctl_after, WINDOW, "about:blank control")

        print(f"\n>>> idle orchestrator2 tab costs "
              f"{(busy_tree - base_tree) / (WINDOW * 10):.1f}% of a core "
              f"over the blank-page baseline.")

        browser.close()


def _browser_pid(browser) -> int:
    """Playwright doesn't expose the browser PID; find it via the driver tree."""
    import psutil  # lazy: only needed here
    me = psutil.Process()
    for proc in me.children(recursive=True):
        try:
            if "chrome.exe" in proc.name().lower() and "--type=" not in " ".join(proc.cmdline()):
                return proc.pid
        except Exception:
            continue
    raise RuntimeError("could not locate the Chromium browser process")


if __name__ == "__main__":
    main()
