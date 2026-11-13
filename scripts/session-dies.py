#!/usr/bin/env python3
"""
The session dies under the visitor's feet.

The container is pulled out from under the page, the way a reaper or a crash
would do it. The visitor is still watching, so the page must fetch a fresh
environment by itself instead of showing a dead end.

    python3 session-dies.py
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot import CDP, DEBUG, browser_socket, fetch_json    # noqa: E402

ROOT = os.environ.get("PORTOS_URL", "http://127.0.0.1:8081")


def targets():
    return fetch_json(DEBUG + "/json/list")


def main():
    browser = browser_socket()
    browser.call("Target.setDiscoverTargets", {"discover": True})
    target = browser.call("Target.createTarget", {"url": "about:blank"})
    target_id = target["targetId"]
    found = None
    for _ in range(40):
        found = next((t for t in targets() if t.get("id") == target_id), None)
        if found:
            break
        time.sleep(0.25)

    cdp = CDP(found["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.navigate", {"url": ROOT})
    time.sleep(14)

    where = cdp.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
    first = where.get("result", {}).get("value", "")
    sid = first.split("/s/")[-1].split("/")[0]
    print(f"  session {sid}")

    # Pull the environment away without saying goodbye.
    subprocess.run(["docker", "rm", "-f", f"portos-{sid}"],
                   capture_output=True, text=True)
    print("  the environment was destroyed")

    time.sleep(25)
    where = cdp.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
    second = where.get("result", {}).get("value", "")
    overlay = cdp.call("Runtime.evaluate", {
        "expression": "(document.getElementById('overlay')||{}).innerText||''",
        "returnByValue": True})
    print(f"  the page ended up at {second}")
    print(f"  the page says: {overlay.get('result', {}).get('value', '')!r}")

    browser.call("Target.closeTarget", {"targetId": target_id})

    if second != first and "/viewer.html" in second:
        print("\n\033[32mPASS\033[0m the visitor was given a fresh environment "
              "without having to click anything")
        return 0
    print("\n\033[31mFAIL\033[0m the visitor was left at a dead end")
    return 1


if __name__ == "__main__":
    sys.exit(main())
