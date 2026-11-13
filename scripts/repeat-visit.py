#!/usr/bin/env python3
"""
Open the front door again and again, the way a visitor who keeps coming back
does, and report the first visit that does not end in a working session.

    python3 repeat-visit.py [rounds]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot import CDP, DEBUG, browser_socket, fetch_json    # noqa: E402

ROOT = os.environ.get("PORTOS_URL", "http://127.0.0.1:8081")
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def targets():
    return fetch_json(DEBUG + "/json/list")


def main():
    browser = browser_socket()
    browser.call("Target.setDiscoverTargets", {"discover": True})

    for round_number in range(1, ROUNDS + 1):
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
        cdp.call("Log.enable")
        cdp.call("Page.navigate", {"url": ROOT})
        time.sleep(10)

        logs = []
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                msg = json.loads(cdp.recv(timeout=max(1, deadline - time.time())))
            except Exception:                      # noqa: BLE001
                break
            if msg.get("method") == "Runtime.consoleAPICalled":
                args = msg["params"].get("args", [])
                logs.append(" ".join(str(a.get("value", a.get("description", "")))
                                     for a in args))

        where = cdp.call("Runtime.evaluate",
                         {"expression": "location.href", "returnByValue": True})
        overlay = cdp.call("Runtime.evaluate", {
            "expression": "(document.getElementById('overlay')||{}).innerText||''",
            "returnByValue": True})
        text = overlay.get("result", {}).get("value", "").replace("\n", " | ")
        sid = where.get("result", {}).get("value", "").split("/s/")[-1][:32]
        broken = "已经结束" in text or "中断" in text
        print(f"  round {round_number:2d} sid={sid} "
              f"{'BROKEN' if broken else 'ok     '} {text[:60]}")
        print(f"      console: {logs}")

        browser.call("Target.closeTarget", {"targetId": target_id})
        if broken:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
