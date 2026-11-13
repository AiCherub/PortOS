#!/usr/bin/env python3
"""
The returning visitor, in a real browser.

The bug this guards against: a visitor closes the browser and opens it again
straight away. The old session is still shutting down, but the server used
to hand it out again because the cookie still pointed at it - so the visitor
landed on "this session has already ended" and had to click their way out.

The browser does the work here: it keeps the cookie, it says goodbye when
the tab goes away, and it fetches the page again when the visitor comes back.
The tab is closed for real, so the browser tears its sockets down the way it
does when a visitor closes the browser window.
"""

import json
import os
import sys
import time
import urllib.request

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


def where(cdp):
    result = cdp.call("Runtime.evaluate",
                      {"expression": "location.href", "returnByValue": True})
    return result.get("result", {}).get("value", "")


def main():
    try:
        browser = browser_socket()
    except Exception as exc:                       # noqa: BLE001
        print(f"\n\033[31mFAIL\033[0m no browser is listening on {DEBUG}")
        print("  start one with: msedge --headless=new --remote-debugging-port=9222 about:blank")
        print(f"  ({exc})")
        return 1
    browser.call("Target.setDiscoverTargets", {"discover": True})

    # The visitor opens the site.
    tab = open_tab(browser)
    cdp = CDP(tab["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.navigate", {"url": ROOT})
    time.sleep(12)
    first = where(cdp)
    print(f"  first visit  -> {first}")

    # The visitor closes the browser tab.
    close_tab(browser, tab["id"])
    print("  browser closed")
    time.sleep(3)

    # ...and comes straight back.
    tab = open_tab(browser)
    cdp = CDP(tab["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.navigate", {"url": ROOT})
    time.sleep(14)
    second = where(cdp)
    print(f"  second visit -> {second}")

    if first == second:
        print("\n\033[31mFAIL\033[0m the returning visitor was sent back into the "
              "session that was already ending")
        return 1
    if "/viewer.html" not in second:
        print("\n\033[31mFAIL\033[0m the returning visitor did not reach a session")
        return 1

    print("\n\033[32mPASS\033[0m the returning visitor got a fresh session")

    # A refresh is not a departure. The visitor must land in the same
    # session, with the program still running.
    cdp.call("Page.reload", {"ignoreCache": True})
    time.sleep(12)
    third = where(cdp)
    print(f"  after a refresh -> {third}")
    if third != second:
        print("\n\033[31mFAIL\033[0m the refresh did not keep the session")
        return 1
    print("\n\033[32mPASS\033[0m the refresh kept the session")

    # Leave nothing behind.
    close_tab(browser, tab["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
