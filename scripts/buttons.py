#!/usr/bin/env python3
"""
The two buttons at the bottom of the page.

    python3 buttons.py

* Ending the session throws the environment away, so the button asks first.
  A mis-tap on a phone must not do that.
* A phone or a tablet has a soft keyboard of its own. The button next to
  the one that ends the session brings it up.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot import CDP, DEBUG, browser_socket, fetch_json    # noqa: E402

ROOT = os.environ.get("PORTOS_URL", "http://127.0.0.1:8081")

STATE = """
(() => {
  const keyboard = document.getElementById('soft-keyboard');
  const confirm = document.getElementById('confirm');
  const overlay = document.getElementById('overlay');
  return {
    keyboardShown: keyboard ? getComputedStyle(keyboard).display !== 'none' : null,
    confirmOpen: confirm ? !confirm.hasAttribute('hidden') : null,
    overlayGone: overlay ? overlay.classList.contains('gone') : null,
    focused: document.activeElement ? document.activeElement.id : '',
  };
})()
"""


def state(cdp):
    res = cdp.call("Runtime.evaluate", {"expression": STATE, "returnByValue": True})
    return res.get("result", {}).get("value")


def middle(cdp, selector):
    res = cdp.call("Runtime.evaluate", {
        "expression": """
        (() => {
          const el = document.querySelector('%s');
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
        })()
        """ % selector,
        "returnByValue": True})
    return res.get("result", {}).get("value")


def click(cdp, x, y):
    for kind in ("mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent",
                 {"type": kind, "x": x, "y": y, "button": "left",
                  "buttons": 1 if kind == "mousePressed" else 0,
                  "clickCount": 1})
        time.sleep(0.05)


def escape(cdp):
    for kind in ("keyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent",
                 {"type": kind, "key": "Escape", "code": "Escape",
                  "windowsVirtualKeyCode": 27})
    time.sleep(0.3)


def main():
    browser = browser_socket()
    browser.call("Target.setDiscoverTargets", {"discover": True})
    target = browser.call("Target.createTarget", {"url": "about:blank"})
    target_id = target["targetId"]
    time.sleep(1)

    page = next(t for t in fetch_json(DEBUG + "/json/list") if t.get("id") == target_id)
    cdp = CDP(page["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Emulation.setDeviceMetricsOverride",
             {"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": False})
    cdp.call("Page.navigate", {"url": ROOT})
    time.sleep(15)

    ok = True

    # A desktop has no soft keyboard of its own, so there is no button.
    desktop = state(cdp)
    print(f"  a desktop: the keyboard button is "
          f"{'there' if desktop['keyboardShown'] else 'not there'}")
    ok &= desktop["keyboardShown"] is False

    # The button that ends the session asks first.
    end = middle(cdp, "#end-session")
    click(cdp, end[0], end[1])
    time.sleep(0.5)
    asked = state(cdp)
    print(f"  after clicking it: the question is "
          f"{'up' if asked['confirmOpen'] else 'not up'}")
    ok &= asked["confirmOpen"] is True
    ok &= asked["overlayGone"] is True

    # Escape takes the question back.
    escape(cdp)
    taken_back = state(cdp)
    print(f"  escape: the question is "
          f"{'up' if taken_back['confirmOpen'] else 'not up'}")
    ok &= taken_back["confirmOpen"] is False

    # A phone has a soft keyboard, and the button brings it up.
    cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    cdp.call("Page.reload")
    time.sleep(15)
    phone = state(cdp)
    print(f"  a phone: the keyboard button is "
          f"{'there' if phone['keyboardShown'] else 'not there'}")
    ok &= phone["keyboardShown"] is True

    keyboard = middle(cdp, "#soft-keyboard")
    if keyboard:
        click(cdp, keyboard[0], keyboard[1])
        time.sleep(0.5)
        focused = state(cdp)
        print(f"  the field that has the focus: {focused['focused']}")
        ok &= focused["focused"] == "ime"

    # And the session really ends when the question is answered.
    end = middle(cdp, "#end-session")
    click(cdp, end[0], end[1])
    time.sleep(0.4)
    ok_at = middle(cdp, "#confirm-ok")
    click(cdp, ok_at[0], ok_at[1])
    time.sleep(3)
    ended = state(cdp)
    print(f"  after saying yes: the page says the session is over "
          f"({'yes' if not ended['overlayGone'] else 'no'})")
    ok &= ended["overlayGone"] is False

    browser.call("Target.closeTarget", {"targetId": target_id})

    if ok:
        print("\n\033[32mPASS\033[0m the buttons do what they should")
        return 0
    print("\n\033[31mFAIL\033[0m the buttons do not do what they should")
    return 1


if __name__ == "__main__":
    sys.exit(main())
