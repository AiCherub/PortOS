#!/usr/bin/env python3
"""
Explore what a visitor gets when they open a second browser window.

    python3 window-reentry.py [gap_seconds]

Opens a tab, waits for it to settle, then opens a second tab after the given
gap and reports what each of them shows. The gap is what decides whether the
old tab is still open when the new one arrives.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot import CDP, DEBUG, browser_socket, fetch_json    # noqa: E402

ROOT = os.environ.get("PORTOS_URL", "http://127.0.0.1:8081")


def targets():
    return fetch_json(DEBUG + "/json/list")


def open_tab(browser, url="about:blank"):
    result = browser.call("Target.createTarget", {"url": url})
    target_id = result["targetId"]
    for _ in range(40):
        for target in targets():
            if target.get("id") == target_id:
                return target
        time.sleep(0.25)
    raise RuntimeError("the new tab never showed up")


def close_tab(browser, target_id):
    browser.call("Target.closeTarget", {"targetId": target_id})


def describe(cdp):
    """Where the tab is, and what it is showing."""
    where = cdp.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
    overlay = cdp.call("Runtime.evaluate", {
        "expression": "(document.getElementById('overlay')||{}).innerText || ''",
        "returnByValue": True})
    return (where.get("result", {}).get("value", ""),
            overlay.get("result", {}).get("value", "").replace("\n", " | "))


def console_errors(cdp, seconds):
    """Whatever the page logged while it settled."""
    errors = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            msg = json.loads(cdp.recv(timeout=max(1, deadline - time.time())))
        except Exception:                          # noqa: BLE001
            break
        method = msg.get("method", "")
        if method == "Runtime.exceptionThrown":
            errors.append(msg["params"]["exceptionDetails"].get("text", ""))
        elif method == "Log.entryAdded":
            entry = msg["params"]["entry"]
            if entry.get("level") in ("error", "warning"):
                errors.append(f"{entry.get('level')}: {entry.get('text')}")
        elif method == "Runtime.consoleAPICalled":
            args = msg["params"].get("args", [])
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
            errors.append(text)
    return errors


def main():
    gap = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    browser = browser_socket()
    browser.call("Target.setDiscoverTargets", {"discover": True})

    tab_a = open_tab(browser)
    cdp_a = CDP(tab_a["webSocketDebuggerUrl"])
    cdp_a.call("Page.enable")
    cdp_a.call("Runtime.enable")
    cdp_a.call("Log.enable")
    cdp_a.call("Runtime.addBinding", {"name": "noop"})
    cdp_a.call("Page.navigate", {"url": ROOT})
    time.sleep(14)
    print(f"  tab A console: {console_errors(cdp_a, 2)}")
    print(f"  tab A -> {describe(cdp_a)}")

    if gap >= 0:
        time.sleep(gap)

    tab_b = open_tab(browser)
    cdp_b = CDP(tab_b["webSocketDebuggerUrl"])
    cdp_b.call("Page.enable")
    cdp_b.call("Runtime.enable")
    cdp_b.call("Log.enable")
    cdp_b.call("Page.navigate", {"url": ROOT})
    time.sleep(16)
    print(f"  tab B console: {console_errors(cdp_b, 3)}")
    print(f"  tab B -> {describe(cdp_b)}")

    # Now the visitor closes the first window. The second one is still
    # watching, so it must keep its session.
    print("  closing tab A ...")
    close_tab(browser, tab_a["id"])
    time.sleep(20)
    print(f"  tab B console: {console_errors(cdp_b, 2)}")
    print(f"  tab B -> {describe(cdp_b)}")

    close_tab(browser, tab_b["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
