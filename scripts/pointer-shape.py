#!/usr/bin/env python3
"""
The pointer takes the shape of the remote pointer.

    python3 pointer-shape.py

The picture carries no pointer - the visitor gets the pointer of their own
machine - but that pointer should change its shape like the remote one does:
an I-beam over a text field, a resize arrow over a window border.

This opens the front door in a real browser, moves the mouse over the
program and watches what shape the canvas gets.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appwindow import settled_geometry, windows            # noqa: E402
from shot import CDP, DEBUG, browser_socket, fetch_json    # noqa: E402

ROOT = os.environ.get("PORTOS_URL", "http://127.0.0.1:8081")

CURSOR = """
(() => {
  const c = document.querySelector('#screen canvas');
  return c ? getComputedStyle(c).cursor : 'no canvas';
})()
"""


def targets():
    return fetch_json(DEBUG + "/json/list")


def text_window(sid):
    """A window with text in it, and where it sits on the remote screen.

    The program on show is the file manager, and it has neither a text field
    nor a window border to point at. A text editor is opened next to it, and
    the pointer is checked against that.
    """
    subprocess.run(["docker", "exec", f"portos-{sid}", "sh", "-c",
                    "setsid wine notepad.exe >/tmp/notepad.log 2>&1 < /dev/null &"],
                   capture_output=True, text=True)
    editor = None
    for _ in range(30):
        editor = next((w for w in windows(sid) if "记事本" in w[1]), None)
        if editor:
            break
        time.sleep(1)
    if not editor:
        return None
    return settled_geometry(sid, editor[0])


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
    cdp.call("Emulation.setDeviceMetricsOverride",
             {"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": False})
    cdp.call("Page.navigate", {"url": ROOT})
    time.sleep(15)
    # The viewer tells the session how big the picture is, and the program
    # moves when the screen changes size. Let that settle before asking
    # where the program is.
    time.sleep(3)

    where = cdp.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
    url = where.get("result", {}).get("value", "")
    sid = url.split("/s/")[-1].split("/")[0]
    print(f"  session {sid}")

    spot = text_window(sid)
    if not spot:
        print("  there is no window with text in it")
        return 1
    x, y, width, height = spot
    print(f"  the window sits at {x},{y} and is {width}x{height}")

    def cursor_at(label, px, py, expected):
        cdp.call("Input.dispatchMouseEvent",
                 {"type": "mouseMoved", "x": px, "y": py, "button": "none"})
        time.sleep(0.5)
        res = cdp.call("Runtime.evaluate",
                       {"expression": CURSOR, "returnByValue": True})
        value = res.get("result", {}).get("value")
        print(f"  {label:<16} ({px},{py}) -> cursor: {value}")
        return value == expected

    def cursor_here(px, py):
        cdp.call("Input.dispatchMouseEvent",
                 {"type": "mouseMoved", "x": px, "y": py, "button": "none"})
        time.sleep(0.5)
        res = cdp.call("Runtime.evaluate",
                       {"expression": CURSOR, "returnByValue": True})
        return res.get("result", {}).get("value")

    ok = True
    ok &= cursor_at("in the text", x + width // 2, y + height // 2, "text")

    # The border that resizes the window is only a pixel or two wide, and
    # where exactly it is depends on the window manager. So the last few
    # pixels of the window are tried, and the border counts as found when
    # one of them shows the resize arrow.
    edge = False
    for probe in range(x + width - 4, x + width):
        shape = cursor_here(probe, y + height // 2)
        print(f"  {'on the edge':<16} ({probe},{y + height // 2}) -> cursor: {shape}")
        if shape == "ew-resize":
            edge = True
            break
    ok &= edge

    ok &= cursor_at("outside", 20, 20, "default")

    browser.call("Target.closeTarget", {"targetId": target_id})

    if ok:
        print("\n\033[32mPASS\033[0m the pointer takes the shape of the remote one")
        return 0
    print("\n\033[31mFAIL\033[0m the pointer did not take the shape of the remote one")
    return 1


if __name__ == "__main__":
    sys.exit(main())
