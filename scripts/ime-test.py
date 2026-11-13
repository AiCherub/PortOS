#!/usr/bin/env python3
"""
Chinese input: the input method on the visitor's own machine has to reach
the program.

    msedge --headless=new --remote-debugging-port=9222 about:blank &
    python3 ime-test.py

Opens the front door, clicks into the program, composes Chinese the way an
input method does (compose, then commit) and checks that the text ended up
in the remote program. Ordinary typing is checked as well, because the
hidden input field sits between the visitor and the remote keyboard.

The browser runs on the host, so when this script runs inside the server
container the endpoint is found under host.docker.internal (see shot.py).
"""

import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appwindow import settled_geometry, windows            # noqa: E402
from shot import CDP, DEBUG, browser_socket, fetch_json    # noqa: E402

ROOT = os.environ.get("PORTOS_URL", "http://127.0.0.1:8081")
CHINESE = "你好世界"
TEST_DOCUMENT = "/tmp/portos-ime-test.txt"


def targets():
    return fetch_json(DEBUG + "/json/list")


def session_of(cdp):
    """The session id the page ended up in."""
    where = cdp.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
    url = where.get("result", {}).get("value", "")
    return url.split("/s/")[-1].split("/")[0]


def docker(sid, *command, timeout=20):
    """Run a command inside the session container."""
    return subprocess.run(["docker", "exec", f"portos-{sid}", *command],
                          capture_output=True, text=True, timeout=timeout)


def remote_text(sid):
    """Save Notepad's document so the test can read the text directly."""
    docker(sid, "xdotool", "key", "--clearmodifiers", "ctrl+s").check_returncode()
    time.sleep(0.3)
    result = docker(sid, "cat", TEST_DOCUMENT)
    result.check_returncode()
    return result.stdout.removeprefix("\ufeff")


def wait_for_remote_text(sid, expected, timeout=8):
    """Allow Notepad to finish saving after synthetic input."""
    deadline = time.monotonic() + timeout
    while True:
        text = remote_text(sid).replace("\r\n", "\n")
        if text == expected or time.monotonic() >= deadline:
            return text


def click_picture(cdp, spot=None, screen=None):
    """Click in the picture, so the program gets the focus.

    Without a spot the middle of the picture is clicked. With one - a
    rectangle on the remote screen - the click lands in the middle of that
    rectangle, so a particular window can be aimed at.
    """
    box = cdp.call("Runtime.evaluate", {
        "expression": "(() => { const c = document.querySelector('#screen canvas');"
                      " if (!c) return null; const r = c.getBoundingClientRect();"
                      " return {x: r.x, y: r.y, w: r.width, h: r.height}; })()",
        "returnByValue": True})
    rect = box.get("result", {}).get("value")
    if not rect:
        # The canvas only shows up once the picture is there, and that can
        # take a while when the session had to be started from scratch.
        for _ in range(30):
            time.sleep(1)
            box = cdp.call("Runtime.evaluate", {
                "expression": "(() => { const c = document.querySelector('#screen canvas');"
                              " if (!c) return null; const r = c.getBoundingClientRect();"
                              " return {x: r.x, y: r.y, w: r.width, h: r.height}; })()",
                "returnByValue": True})
            rect = box.get("result", {}).get("value")
            if rect:
                break
    if not rect:
        return False
    if spot and screen and screen[0] and screen[1]:
        x = rect["x"] + (spot[0] + spot[2] / 2) / screen[0] * rect["w"]
        y = rect["y"] + (spot[1] + spot[3] / 2) / screen[1] * rect["h"]
    else:
        x = rect["x"] + rect["w"] / 2
        y = rect["y"] + rect["h"] / 2
    for kind in ("mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": "left",
            "clickCount": 1, "buttons": 1 if kind == "mousePressed" else 0,
        })
    return True


def screen_size(sid):
    """The size of the remote screen."""
    out = docker(sid, "xrandr", "--query").stdout
    found = re.search(r"current (\d+) x (\d+)", out)
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)


def type_with_input_method(cdp, text):
    """Compose text the way an input method does, and commit it."""
    cdp.call("Input.imeSetComposition", {
        "text": text, "selectionStart": len(text), "selectionEnd": len(text)})
    time.sleep(0.3)
    cdp.call("Input.insertText", {"text": text})


def compose_with_input_method(cdp, text, stray_key):
    """Compose a word, press a key while composing, then commit.

    The key pressed during the composition belongs to the input method, not
    to the program - the pinyin must not show up in the remote document.
    """
    cdp.call("Input.imeSetComposition", {
        "text": text, "selectionStart": len(text), "selectionEnd": len(text)})
    time.sleep(0.3)
    cdp.call("Input.dispatchKeyEvent", {
        "type": "keyDown", "key": stray_key, "code": "Key" + stray_key.upper(),
        "text": stray_key, "windowsVirtualKeyCode": ord(stray_key.upper()),
        "nativeVirtualKeyCode": ord(stray_key.upper())})
    cdp.call("Input.dispatchKeyEvent", {
        "type": "keyUp", "key": stray_key, "code": "Key" + stray_key.upper(),
        "windowsVirtualKeyCode": ord(stray_key.upper()),
        "nativeVirtualKeyCode": ord(stray_key.upper())})
    time.sleep(0.3)
    cdp.call("Input.insertText", {"text": text})


def type_ascii(cdp, text):
    """Plain typing, with no input method involved."""
    for ch in text:
        cdp.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": ch, "code": "Key" + ch.upper(),
            "text": ch, "windowsVirtualKeyCode": ord(ch.upper()),
            "nativeVirtualKeyCode": ord(ch.upper())})
        cdp.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": ch, "code": "Key" + ch.upper(),
            "windowsVirtualKeyCode": ord(ch.upper()),
            "nativeVirtualKeyCode": ord(ch.upper())})
        time.sleep(0.05)


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
             {"width": 1280, "height": 720, "deviceScaleFactor": 1,
              "mobile": False})
    cdp.call("Page.navigate", {"url": ROOT})
    time.sleep(14)

    sid = session_of(cdp)
    print(f"  session {sid}")
    if not sid:
        print("\n\033[31mFAIL\033[0m the page never reached a session")
        return 1

    # The program on show is the file manager, and a file manager does not
    # take text. A text editor is opened next to it, and the visitor's input
    # method is checked against that.
    docker(sid, "python3", "-c",
           "from pathlib import Path; import sys; "
           "Path(sys.argv[1]).write_text('IME test', encoding='utf-8-sig')",
           TEST_DOCUMENT).check_returncode()
    docker(sid, "sh", "-c",
           'setsid wine notepad.exe "$1" >/tmp/notepad.log 2>&1 < /dev/null &',
           "ime-test", r"Z:\tmp\portos-ime-test.txt").check_returncode()
    editor = None
    for _ in range(30):
        editor = next((w for w in windows(sid) if "记事本" in w[1]), None)
        if editor:
            break
        time.sleep(1)
    else:
        print("\n\033[31mFAIL\033[0m the text editor never appeared")
        return 1
    print(f"  the text editor is on the screen: {editor[1]}")

    # The click has to land on the editor, so that the editor gets the
    # focus and the keys go to it.
    spot = settled_geometry(sid, editor[0])
    if not spot:
        print("\n\033[31mFAIL\033[0m the editor's place on the screen is unknown")
        return 1
    if not click_picture(cdp, spot, screen_size(sid)):
        print("\n\033[31mFAIL\033[0m there is no picture to click")
        return 1
    time.sleep(1)

    # The hidden field is where the input method composes its text.
    cdp.call("Runtime.evaluate",
             {"expression": "document.getElementById('ime').focus()"})

    # Start from an empty document.
    docker(sid, "xdotool", "key", "--clearmodifiers", "ctrl+a")
    time.sleep(0.2)
    docker(sid, "xdotool", "key", "--clearmodifiers", "Delete")
    time.sleep(0.5)

    type_with_input_method(cdp, CHINESE)
    time.sleep(2.5)
    typed = wait_for_remote_text(sid, CHINESE)
    print(f"  the program shows: {typed!r}")

    ok = typed == CHINESE

    # Ordinary typing has to keep working - the hidden field sits between the
    # visitor and the remote keyboard.
    type_ascii(cdp, "ok")
    time.sleep(1.5)
    typed = wait_for_remote_text(sid, CHINESE + "ok")
    print(f"  after typing ascii: {typed!r}")
    ok = ok and typed == CHINESE + "ok"

    # The keys that belong to the composition must not reach the program:
    # the pinyin would show up next to the Chinese.
    compose_with_input_method(cdp, "中", "z")
    time.sleep(2.5)
    typed = wait_for_remote_text(sid, CHINESE + "ok中")
    print(f"  after composing with a stray key: {typed!r}")
    ok = ok and typed == CHINESE + "ok中"

    # A soft keyboard produces no key events at all, so its text has to
    # travel the other way round.
    cdp.call("Input.insertText", {"text": "soft"})
    time.sleep(2.5)
    typed = wait_for_remote_text(sid, CHINESE + "ok中soft")
    print(f"  after a soft keyboard: {typed!r}")
    ok = ok and typed == CHINESE + "ok中soft"

    # The return key belongs to the program, not to the input method.
    cdp.call("Input.dispatchKeyEvent", {
        "type": "keyDown", "key": "Enter", "code": "Enter",
        "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    cdp.call("Input.dispatchKeyEvent", {
        "type": "keyUp", "key": "Enter", "code": "Enter",
        "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    time.sleep(1.5)
    typed = wait_for_remote_text(sid, CHINESE + "ok中soft\n")
    print(f"  after pressing return: {typed!r}")
    ok = ok and typed == CHINESE + "ok中soft\n"

    browser.call("Target.closeTarget", {"targetId": target_id})

    # Leave the editor the way it was found.
    docker(sid, "sh", "-c", "pkill -f notepad.exe")
    docker(sid, "rm", "-f", TEST_DOCUMENT)
    time.sleep(3)

    if ok:
        print("\n\033[32mPASS\033[0m the input method reached the program")
        return 0
    print("\n\033[31mFAIL\033[0m the text did not arrive as expected")
    return 1


if __name__ == "__main__":
    sys.exit(main())
