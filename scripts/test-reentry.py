#!/usr/bin/env python3
"""
Check that a returning visitor gets a fresh session.

The bug this guards against: a visitor closes the browser and opens it again
right away. The old session is still shutting down, but the server used to
hand it out again because the cookie still pointed at it - so the visitor
landed on "this session has already ended" and had to click their way out.

The steps below are the ones a real browser takes:
    1. open the front door            -> a session, and a cookie
    2. open the control channel        -> the server sees a visitor
    3. close the browser               -> the socket dies, the page says bye
    4. open the front door again       -> must be a different session
"""

import http.cookiejar
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify import WebSocket                       # noqa: E402

BASE = os.environ.get("PORTOS_URL", "http://127.0.0.1:8081").rstrip("/")

# The control channel lives on the same host and port as the front door.
_hostport = BASE.split("//", 1)[-1].partition("/")[0]
HOST, _, _port = _hostport.partition(":")
PORT = int(_port or 80)


def session_id(url):
    return url.split("/s/")[1].split("/")[0]


def enter(opener):
    """Open the front door and return where it leads."""
    with opener.open(BASE + "/", timeout=180) as response:
        return response.geturl()


def main():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    first = enter(opener)
    sid1 = session_id(first)
    print(f"  first visit  -> {sid1}")

    # The viewer opens the control channel as soon as the page is up.
    ws = WebSocket(HOST, PORT, f"/s/{sid1}/ctl?token=ctl")
    ws.send(json.dumps({"t": "hello"}))
    print("  control channel:", ws.recv().strip()[:60])

    # The visitor closes the browser: the page says goodbye and the socket
    # dies with the tab.
    ws.send(json.dumps({"t": "bye"}))
    time.sleep(0.5)
    ws.close()
    print("  browser closed")

    # ...and comes straight back.
    time.sleep(1)
    second = enter(opener)
    sid2 = session_id(second)
    print(f"  second visit -> {sid2}")

    if sid1 == sid2:
        print("\n\033[31mFAIL\033[0m the returning visitor was sent back into the "
              "session that was already ending")
        return 1

    print("\n\033[32mPASS\033[0m the returning visitor got a fresh session")
    return 0


if __name__ == "__main__":
    sys.exit(main())
