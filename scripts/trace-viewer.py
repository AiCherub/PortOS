#!/usr/bin/env python3
"""
Watch what one visitor's page really does: the navigations, the WebSocket
frames it sends, and what it finally shows.

    python3 trace-viewer.py [url]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot import CDP, DEBUG, browser_socket, fetch_json    # noqa: E402

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "PORTOS_URL", "http://127.0.0.1:8081")
WATCH = float(os.argv[2]) if len(sys.argv) > 2 else 25.0


def targets():
    return fetch_json(DEBUG + "/json/list")


def main():
    browser = browser_socket()
    browser.call("Target.setDiscoverTargets", {"discover": True})
    target = browser.call("Target.createTarget", {"url": "about:blank"})
    target_id = target["targetId"]
    for _ in range(40):
        found = next((t for t in targets() if t.get("id") == target_id), None)
        if found:
            break
        time.sleep(0.25)

    cdp = CDP(found["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Log.enable")
    cdp.call("Network.enable")

    start = time.time()
    cdp.call("Page.navigate", {"url": ROOT})

    names = {}

    def stamp():
        return f"{time.time() - start:6.2f}s"

    deadline = time.time() + WATCH
    while time.time() < deadline:
        try:
            msg = json.loads(cdp.recv(timeout=max(1, deadline - time.time())))
        except Exception:                          # noqa: BLE001
            break
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "Page.frameNavigated":
            frame = params.get("frame", {})
            if not params.get("type"):
                print(f"{stamp()} navigate   {frame.get('url')}")
        elif method == "Network.webSocketCreated":
            url = params.get("url", "")
            name = "vnc" if "websockify" in url else "ctl"
            names[params.get("requestId")] = name
            print(f"{stamp()} ws open   {name}")
        elif method == "Network.webSocketClosed":
            print(f"{stamp()} ws close  {names.get(params.get('requestId'), '?')}")
        elif method == "Network.webSocketFrameSent":
            name = names.get(params.get("requestId"), "?")
            payload = params.get("response", {}).get("payloadData", "")
            if name == "ctl" or payload[:1] == "{":
                print(f"{stamp()} ws {name} sent   {payload[:120]!r}")
        elif method == "Network.webSocketFrameReceived":
            name = names.get(params.get("requestId"), "?")
            payload = params.get("response", {}).get("payloadData", "")
            if name == "ctl":
                print(f"{stamp()} ws {name} recv   {payload[:120]!r}")
        elif method == "Runtime.exceptionThrown":
            detail = params["exceptionDetails"]
            print(f"{stamp()} EXCEPTION {detail.get('text')}")
        elif method == "Log.entryAdded":
            entry = params["entry"]
            print(f"{stamp()} log {entry.get('level')}: {entry.get('text')}")
        elif method == "Runtime.consoleAPICalled":
            args = params.get("args", [])
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
            print(f"{stamp()} console {text}")

    where = cdp.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
    overlay = cdp.call("Runtime.evaluate", {
        "expression": "(document.getElementById('overlay')||{}).innerText||''",
        "returnByValue": True})
    print(f"\n  ended at {where.get('result', {}).get('value')}")
    print(f"  overlay: {overlay.get('result', {}).get('value')!r}")

    browser.call("Target.closeTarget", {"targetId": target_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
