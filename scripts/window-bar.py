#!/usr/bin/env python3
"""
A window that is put away can be found again - through the bar at the bottom.

    python3 window-bar.py

The session shows a program and nothing else: no desktop, no task bar. So
when a window is minimised the visitor is left with an empty picture. This
checks the two things that keep that from being a dead end:

  * the window can be minimised like any other window (the title bar has
    the button, the menu has the entry), and the menu has no "Send to
    desktop" - that one would move the window to a virtual desktop the
    visitor cannot reach;
  * a maximized window stays minimized until it is brought back through the
    window bar, and its original size can still be restored.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appwindow import app_window, settled_geometry        # noqa: E402
from shot import CDP, DEBUG, browser_socket, fetch_json    # noqa: E402

ROOT = os.environ.get("PORTOS_URL", "http://127.0.0.1:8081")
MAXIMIZED = ("_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")

failures = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    if not ok:
        failures.append(name)


def in_session(sid, *args):
    """Run a command inside the session container."""
    proc = subprocess.run(["docker", "exec", f"portos-{sid}", *args],
                          capture_output=True, text=True, timeout=30)
    return proc.stdout


def evaluate(cdp, expression):
    result = cdp.call("Runtime.evaluate",
                      {"expression": expression, "returnByValue": True})
    return result.get("result", {}).get("value", "")


def main():
    browser = browser_socket()
    browser.call("Target.setDiscoverTargets", {"discover": True})
    target = browser.call("Target.createTarget", {"url": "about:blank"})
    target_id = target["targetId"]
    found = None
    for _ in range(40):
        targets = fetch_json(DEBUG + "/json/list")
        found = next((t for t in targets if t.get("id") == target_id), None)
        if found:
            break
        time.sleep(0.25)

    cdp = CDP(found["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.navigate", {"url": ROOT})
    time.sleep(14)

    sid = evaluate(cdp, "location.href").split("/s/")[-1].split("/")[0]
    print(f"  session {sid}")
    if not sid:
        print("\n\033[31mFAIL\033[0m no session to test")
        return 1

    # --- the window can be minimised, but not lost -------------------------
    layout = in_session(sid, "grep", "-o", "<titleLayout>[^<]*",
                        "/tmp/openbox-rc.xml").strip()
    buttons = layout.replace("<titleLayout>", "")
    check("the title bar has a minimise button", "I" in buttons,
          f"titleLayout {buttons}")

    menu = in_session(sid, "cat", "/tmp/openbox-menu.xml")
    check("the menu can minimise a window", "Iconify" in menu,
          "最小化 in the client menu")
    check("the menu does not send the window to another desktop",
          "GoToDesktop" not in menu and "Send to desktop" not in menu,
          "no desktop in the menu")

    client_menu = in_session(sid, "grep", "-c", "client-menu",
                             "/tmp/openbox-rc.xml").strip()
    check("the client menu is ours", client_menu in ("0", ""),
          f"{client_menu} references to openbox's own menu")

    # --- the window is found again through the bar ------------------------
    # The program's window is the biggest one on the screen, and it is
    # called whatever the program calls itself.
    found = app_window(sid)
    if not found:
        check("the program has a window", False, "no window in the session")
        browser.call("Target.closeTarget", {"targetId": target_id})
        return 1
    window_id, title = found
    print(f"  window {window_id} {title}")

    bar = evaluate(cdp, "(document.getElementById('window-list')||{}).innerText||''")
    check("the bar lists the window", title in bar, bar.replace("\n", " | "))

    normal_geometry = settled_geometry(sid, window_id)
    in_session(sid, "wmctrl", "-i", "-r", window_id, "-b",
               "add,maximized_vert,maximized_horz")
    maximized_geometry = settled_geometry(sid, window_id)
    state = in_session(sid, "xprop", "-id", window_id, "_NET_WM_STATE")
    check("the window maximizes", all(flag in state for flag in MAXIMIZED), state.strip())

    in_session(sid, "xdotool", "windowminimize", window_id)
    time.sleep(5)
    state = in_session(sid, "xprop", "-id", window_id, "_NET_WM_STATE")
    check("the maximized window stays minimized",
          "_NET_WM_STATE_HIDDEN" in state, state.strip())

    bar = evaluate(cdp, "(document.getElementById('window-list')||{}).innerText||''")
    check("the minimized window still has its title in the bar",
          title in bar and "已最小化" not in bar,
          bar.replace("\n", " | "))

    # Click the window in the bar and see whether it comes back.
    rect = evaluate(cdp, """
      (function () {
        const button = document.querySelector('#window-list button');
        if (!button) return null;
        const box = button.getBoundingClientRect();
        return {x: box.left + box.width / 2, y: box.top + box.height / 2};
      })()
    """)
    if not rect:
        check("the window in the bar can be clicked", False, "no button")
    else:
        point = json.loads(rect) if isinstance(rect, str) else rect
        for kind in ("mousePressed", "mouseReleased"):
            cdp.call("Input.dispatchMouseEvent", {
                "type": kind, "x": point["x"], "y": point["y"],
                "button": "left", "clickCount": 1,
            })
        time.sleep(3)

    state = in_session(sid, "xprop", "-id", window_id, "_NET_WM_STATE")
    check("the window is back on the screen and still maximized",
          "_NET_WM_STATE_HIDDEN" not in state and all(flag in state for flag in MAXIMIZED)
          and maximized_geometry is not None
          and settled_geometry(sid, window_id) == maximized_geometry, state.strip())

    in_session(sid, "wmctrl", "-i", "-r", window_id, "-b",
               "remove,maximized_vert,maximized_horz")
    check("leaving maximized mode restores the original geometry",
          normal_geometry is not None and settled_geometry(sid, window_id) == normal_geometry)

    browser.call("Target.closeTarget", {"targetId": target_id})

    if failures:
        print(f"\n\033[31mFAIL\033[0m {len(failures)} check(s) failed")
        return 1
    print("\n\033[32mPASS\033[0m a window that was put away can be found again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
