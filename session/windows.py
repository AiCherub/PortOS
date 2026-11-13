#!/usr/bin/env python3
"""
The windows of a session: list them, and bring one back.

A session shows one program and nothing else - there is no desktop and no
task bar. If the program puts its own window away (some Windows programs
minimise themselves) the visitor is left with an empty picture and no way
of getting the window back.

So the session keeps track of its windows, and the viewer offers them in a
bar at the bottom of the browser window: clicking a window brings it back.

    python3 windows.py list          -> JSON window list with optional PNG icons
    python3 windows.py activate <id>  -> put the window back and focus it
"""

import base64
import json
import os
import re
import struct
import subprocess
import sys
import zlib

DISPLAY = os.environ.get("DISPLAY", ":1")
ENV = dict(os.environ, DISPLAY=DISPLAY)

# A window id is a hex number. Nothing else is ever handed to a command.
WINDOW_ID = re.compile(r"^0x[0-9a-fA-F]+$")
ICON_SIZE = 32  # 16 CSS pixels at 2x density


def run(args, timeout=10):
    """Run a command and return (returncode, stdout)."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, env=ENV)
    except Exception:                              # noqa: BLE001
        return 1, ""
    return proc.returncode, proc.stdout


def active_window():
    """The window the window manager is pointing at."""
    _, out = run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    found = re.search(r"0x[0-9a-fA-F]+", out)
    return found.group(0) if found else ""


def window_ids():
    """The windows the window manager knows about."""
    _, out = run(["wmctrl", "-l"])
    ids = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if parts and WINDOW_ID.match(parts[0]):
            ids.append(parts[0])
    return ids


def same_window(one, other):
    """Are these the same window?

    The ids come from different tools and they do not pad the same way:
    xprop says 0xe00001, wmctrl says 0x00e00001.
    """
    try:
        return int(one, 16) == int(other, 16)
    except (TypeError, ValueError):
        return False


def png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def window_icon(value):
    """Choose an X11 ARGB icon and encode a small transparent PNG."""
    values = value.split(",")
    best = None
    offset = 0
    try:
        while offset + 2 <= len(values):
            width, height = int(values[offset]), int(values[offset + 1])
            offset += 2
            end = offset + width * height
            if width <= 0 or height <= 0 or end > len(values):
                break
            size = max(width, height)
            rank = (size < ICON_SIZE, abs(size - ICON_SIZE))
            if best is None or rank < best[0]:
                best = rank, offset, width, height
            offset = end

        if best is None:
            return None

        _, start, width, height = best
        scale = min(1, ICON_SIZE / max(width, height))
        out_width = max(1, round(width * scale))
        out_height = max(1, round(height * scale))
        columns = [int((x + 0.5) * width / out_width) for x in range(out_width)]
        rows = bytearray()
        for y in range(out_height):
            source = start + int((y + 0.5) * height / out_height) * width
            rows.append(0)  # PNG scanline filter: None
            for column in columns:
                argb = int(values[source + column])
                rows.extend(((argb >> 16) & 255, (argb >> 8) & 255,
                             argb & 255, (argb >> 24) & 255))
    except ValueError:
        return None

    header = struct.pack(">IIBBBBB", out_width, out_height, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
           + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


def describe(wid, focused):
    """What the visitor would like to know about one window."""
    # Read the raw cardinals instead of xprop's default terminal rendering.
    _, out = run(["xprop", "-id", wid, "-f", "_NET_WM_ICON", "32c",
                  "_NET_WM_NAME", "WM_NAME", "_NET_WM_STATE",
                  "WM_TRANSIENT_FOR", "_NET_WM_ICON"])
    title = ""
    fallback = ""
    hidden = False
    parent = None
    icon = None
    for line in out.splitlines():
        name, _, value = line.partition("=")
        name = name.split("(", 1)[0]
        if name == "_NET_WM_NAME":
            title = value.strip().strip('"')
        elif name == "WM_NAME":
            fallback = value.strip().strip('"')
        elif name == "_NET_WM_STATE":
            hidden = "_NET_WM_STATE_HIDDEN" in line
        elif name == "WM_TRANSIENT_FOR":
            match = re.search(r"0x[0-9a-fA-F]+", line)
            if match:
                parent = int(match.group(0), 16)
        elif name == "_NET_WM_ICON" and value:
            icon = window_icon(value)
    window = {
        "id": wid,
        "title": title or fallback or "(无名窗口)",
        "hidden": hidden,
        "focused": bool(focused) and same_window(wid, focused),
    }
    if icon:
        window["icon"] = icon
    return window, parent


def list_windows():
    """Every window of the session, with the state the visitor cares about."""
    focused = active_window()
    windows = {int(wid, 16): describe(wid, focused) for wid in window_ids()}
    for wid, (window, parent) in windows.items():
        # Openbox can use the parent's icon for a dialog without its own.
        seen = {wid}
        while "icon" not in window and parent in windows and parent not in seen:
            seen.add(parent)
            ancestor, parent = windows[parent]
            if "icon" in ancestor:
                window["icon"] = ancestor["icon"]
    return [window for window, _ in windows.values()]


def activate(wid):
    """Put a window back on the screen and give it the focus."""
    if not isinstance(wid, str) or not WINDOW_ID.match(wid):
        return False

    _, state = run(["xprop", "-id", wid, "_NET_WM_STATE"])
    if "_NET_WM_STATE_HIDDEN" in state:
        # A window that is hidden has to be mapped again before it can be
        # focused - otherwise the activation runs into a window that is not
        # on the screen.
        run(["xdotool", "windowmap", wid])

    code, _ = run(["wmctrl", "-i", "-a", wid])
    if code != 0:
        run(["xdotool", "windowactivate", wid])
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    command = sys.argv[1]

    if command == "list":
        print(json.dumps(list_windows(), ensure_ascii=False))
        return 0

    if command == "activate":
        if len(sys.argv) < 3:
            print("activate needs a window id", file=sys.stderr)
            return 1
        return 0 if activate(sys.argv[2]) else 1

    print("unknown command: %s" % command, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
