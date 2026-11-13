#!/usr/bin/env python3
"""
Closing the program ends the session - and the visitor is told why.

    python3 closing-program.py

Opens the front door, closes the program the way a visitor would (by closing
its window inside the remote desktop) and checks what the page says.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appwindow import app_window, close                    # noqa: E402
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
    url = where.get("result", {}).get("value", "")
    sid = url.split("/s/")[-1].split("/")[0]
    print(f"  session {sid}")

    # Close the program the way a visitor would: by closing its window.
    window = app_window(sid)
    if not window:
        print("\n\033[31mFAIL\033[0m the program has no window to close")
        browser.call("Target.closeTarget", {"targetId": target_id})
        return 1
    print(f"  the program's window: {window[1]}")
    close(sid, window[0])
    print("  the program was closed")

    time.sleep(20)
    overlay = cdp.call("Runtime.evaluate", {
        "expression": "(document.getElementById('overlay')||{}).innerText||''",
        "returnByValue": True})
    text = overlay.get("result", {}).get("value", "").replace("\n", " | ")
    print(f"  the page says: {text}")

    browser.call("Target.closeTarget", {"targetId": target_id})

    if "已经结束" in text and "重新进入" in text:
        print("\n\033[32mPASS\033[0m the visitor was told that the session is over")
        return 0
    print("\n\033[31mFAIL\033[0m the page did not explain the end of the session")
    return 1


if __name__ == "__main__":
    sys.exit(main())
