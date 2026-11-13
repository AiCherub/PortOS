#!/usr/bin/env python3
"""
The window of the showcased program.

The regression scripts used to close the program by its title ("记事本"),
which only works while a text editor is the program on show. The window is
found by size instead - the biggest window on the screen is the program's
own window, the same rule the session's own window-fitting follows - so the
scripts do not have to know what the program calls itself.
"""

import subprocess
import time


def windows(sid):
    """Every window of the session: [(id, title), ...], the biggest first."""
    out = subprocess.run(
        ["docker", "exec", f"portos-{sid}", "wmctrl", "-l", "-G"],
        capture_output=True, text=True, timeout=30).stdout
    found = []
    for line in out.strip().splitlines():
        parts = line.split(None, 7)
        if len(parts) < 6:
            continue
        try:
            area = int(parts[4]) * int(parts[5])
        except ValueError:
            continue
        title = parts[7].strip() if len(parts) > 7 else ""
        found.append((area, parts[0], title))
    found.sort(reverse=True)
    return [(wid, title) for _, wid, title in found]


def app_window(sid):
    """The program's own window: (id, title). None when there is none."""
    found = windows(sid)
    return found[0] if found else None


def geometry(sid, wid):
    """Where a window is on the remote screen: (x, y, width, height)."""
    out = subprocess.run(
        ["docker", "exec", f"portos-{sid}", "wmctrl", "-l", "-G"],
        capture_output=True, text=True, timeout=30).stdout
    for line in out.strip().splitlines():
        parts = line.split(None, 7)
        if parts and parts[0] == wid and len(parts) >= 6:
            return tuple(int(p) for p in parts[2:6])
    return None


def settled_geometry(sid, wid, tries=6, delay=1.0):
    """Where a window is, once it has stopped moving.

    A window is mapped where the program asked for it, and the window
    manager puts it where it wants a moment later. Reading the place right
    away therefore gets the wrong answer - and a click based on it lands
    somewhere else entirely.
    """
    last = None
    for _ in range(tries):
        time.sleep(delay)
        spot = geometry(sid, wid)
        if spot and spot == last:
            return spot
        last = spot
    return last


def close(sid, wid):
    """Close a window the way a visitor would: by clicking its X."""
    return subprocess.run(
        ["docker", "exec", f"portos-{sid}", "wmctrl", "-i", "-c", wid],
        capture_output=True, text=True, timeout=30)
