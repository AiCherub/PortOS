#!/usr/bin/env python3
"""
PortOS session control channel.

A tiny newline-delimited JSON server. websockify bridges it to the browser as
a WebSocket, which gives the browser two things the VNC protocol cannot:

  * "size"  - ask the server to change the remote resolution. TigerVNC ignores
              the RFB SetDesktopSize message, so this is the only way to make
              the desktop follow the browser window.
  * "text"  - text the visitor composed with the input method on their own
              machine, which has to be typed into the remote window.
  * "windows" - the windows the session has. There is no desktop and no task
              bar in here, so this is what lets the visitor find a window
              again; "activate" puts one back on the screen.
  * "cursor" - the shape of the pointer on the X server, so that the
              browser can give the visitor's own pointer the right shape.
  * "ping"  - a heartbeat, used to decide when the visitor has really left.

Lifecycle: this process is the container's main process. When it decides the
session is over it exits, the container stops, and the next visitor gets a
brand new environment.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from type_text import deliver_text          # noqa: E402
from clipboard_sync import read_clipboard, write_clipboard, copy_selection

DISPLAY = os.environ.get("DISPLAY", ":1")
RESET_GRACE = float(os.environ.get("RESET_GRACE", "120"))  # no heartbeat for this long -> end
BOOT_GRACE = float(os.environ.get("BOOT_GRACE", "180"))    # nobody showed up -> end
BYE_GRACE = float(os.environ.get("BYE_GRACE", "15"))        # browser said goodbye -> end after this
SET_RES = "/opt/portos/session/set-resolution.sh"
WINDOWS_HELPER = "/opt/portos/session/windows.py"
CURSOR_WATCHER = "/opt/portos/session/cursor_watch.py"
READY_MARKER = "/tmp/control-ready"
# Written as soon as the session is condemned - the visitor said goodbye, or
# the heartbeat stopped. The server reads it to tell a session that is on its
# way out from one that is still good. A browser that navigates away can
# leave its socket open for a while, so the socket alone is not enough.
ENDING_MARKER = "/tmp/session-ending"
# A pre-warmed container waits for its first visitor indefinitely. The server
# drops this marker in as soon as it hands the container to somebody; from
# then on the normal boot grace applies, so a visitor who clicks "launch" and
# immediately closes the tab does not leave a container behind.
CLAIMED_MARKER = "/tmp/claimed"
VISITOR_MARKER = "/tmp/visitor-connected"
# The entrypoint writes the pid of the program it launched. Watching it is
# how the control channel learns that the program was closed, so that the
# visitor can be told why the session is over.
APP_PID_FILE = "/tmp/app.pid"
SESSION_ID = os.environ.get("SESSION_ID", "")
CLIPBOARD_MODE = os.environ.get("SESSION_CLIPBOARD", "both")
CLIPBOARD_IN = CLIPBOARD_MODE in ("both", "in")
CLIPBOARD_OUT = CLIPBOARD_MODE in ("both", "out")
INIT_W = int(os.environ.get("INIT_W", "1280"))
INIT_H = int(os.environ.get("INIT_H", "720"))
MAX_W = int(os.environ.get("MAX_W", "2560"))
MAX_H = int(os.environ.get("MAX_H", "1440"))
MIN_W, MIN_H = 480, 320
# How much text one message may carry. A word from the input method is a few
# characters; anything bigger is not something a visitor typed.
MAX_TEXT = 4096

STATE = {
    "started": time.time(),
    "first_client": None,
    "last_msg": time.time(),
    "bye_at": None,
    "clients": 0,
    "size": (INIT_W, INIT_H),
    # The windows the session has. The viewer shows them in the bar at the
    # bottom, so that a program which puts its window away can be found
    # again - there is no desktop and no task bar in here.
    "windows": [],
    # Everybody who is watching right now, so that the session can say
    # goodbye to all of them at once.
    "writers": set(),
    # The shape of the pointer on the X server right now ("text" over a
    # text field, "ew-resize" over a window border, and so on). The
    # browser draws the pointer of the visitor's own machine, and this is
    # what tells it which shape that pointer should have.
    "cursor": "default",
}


def log(message):
    print(f"[control {SESSION_ID}] {message}", flush=True)


def mark_ending():
    """Tell the server that this session is on its way out."""
    try:
        with open(ENDING_MARKER, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError:
        pass


def clear_ending():
    """The visitor is back, so the session is good again."""
    try:
        os.remove(ENDING_MARKER)
    except OSError:
        pass


def reply(writer, payload):
    try:
        writer.write((json.dumps(payload) + "\n").encode())
    except Exception:
        pass


def broadcast(payload):
    """Tell everybody who is watching that something happened."""
    for writer in list(STATE["writers"]):
        reply(writer, payload)


def program_gone():
    """Has the program inside this session been closed?

    The entrypoint watches it too, but it only notices a few seconds later.
    The control channel asks straight away, because it is the one that has
    to tell the visitor before the session goes away.
    """
    try:
        with open(APP_PID_FILE, "r", encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def apply_size(width, height):
    """Resize the remote screen. Returns (ok, actual_width, actual_height)."""
    width = max(MIN_W, min(int(width), MAX_W))
    height = max(MIN_H, min(int(height), MAX_H))

    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    try:
        proc = subprocess.run(
            [SET_RES, str(width), str(height)],
            capture_output=True, text=True, timeout=25, env=env,
        )
        parts = proc.stdout.split()
        ok = proc.returncode == 0
    except Exception as exc:                       # noqa: BLE001
        log(f"resize failed: {exc}")
        parts = []
        ok = False

    if len(parts) == 2:
        try:
            aw, ah = int(parts[0]), int(parts[1])
        except ValueError:
            aw, ah = STATE["size"]
    else:
        aw, ah = STATE["size"]

    STATE["size"] = (aw, ah)
    ok = ok and (aw, ah) == (width, height)
    log(f"resize request {width}x{height} -> actual {aw}x{ah} ({'ok' if ok else 'clamped'})")
    return ok, aw, ah


def list_windows():
    """The windows the session has right now."""
    try:
        proc = subprocess.run(
            [sys.executable, WINDOWS_HELPER, "list"],
            capture_output=True, text=True, timeout=15,
        )
        windows = json.loads(proc.stdout)
    except Exception as exc:                       # noqa: BLE001
        log(f"could not list the windows: {exc}")
        return []
    if not isinstance(windows, list):
        return []
    return [w for w in windows if isinstance(w, dict)]


def activate_window(window_id):
    """Put a window back on the screen and give it the focus."""
    try:
        proc = subprocess.run(
            [sys.executable, WINDOWS_HELPER, "activate", window_id],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:                       # noqa: BLE001
        log(f"could not activate {window_id}: {exc}")
        return False
    return proc.returncode == 0


async def refresh_windows():
    """Ask the session what windows it has."""
    loop = asyncio.get_running_loop()
    STATE["windows"] = await loop.run_in_executor(None, list_windows)


async def watch_cursor():
    """Follow the shape of the pointer on the X server.

    The picture carries no pointer, so the browser draws the pointer of
    the visitor's own machine. This is what tells it what that pointer
    should look like: an I-beam over a text field, a resize arrow over a
    window border.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, CURSOR_WATCHER,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except Exception as exc:                       # noqa: BLE001
        log(f"could not start the cursor watch: {exc}")
        return

    while True:
        try:
            line = await proc.stdout.readline()
        except Exception as exc:                   # noqa: BLE001
            log(f"the cursor watch stopped: {exc}")
            return
        if not line:
            return
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        shape = msg.get("shape")
        if not isinstance(shape, str) or shape == STATE["cursor"]:
            continue
        STATE["cursor"] = shape
        log(f"the pointer is now {shape}")
        broadcast({"t": "cursor", "shape": shape})


async def handle_message(msg, writer):
    kind = msg.get("t")
    STATE["last_msg"] = time.time()

    if kind == "ping":
        reply(writer, {"t": "pong", "ts": time.time()})
    elif kind == "hello":
        # Look at the windows straight away, so that the bar at the bottom
        # of the page has something to show before the first poll.
        await refresh_windows()
        reply(writer, {
            "t": "hello",
            "sid": SESSION_ID,
            "size": {"w": STATE["size"][0], "h": STATE["size"][1]},
            "app": open("/tmp/app.cmd").read().strip() if os.path.exists("/tmp/app.cmd") else "",
            "windows": STATE["windows"],
            # The shape of the pointer, so that a visitor who comes in
            # late does not have to move the mouse to find out.
            "cursor": STATE["cursor"],
        })
    elif kind == "size":
        try:
            width = int(msg.get("w", 0))
            height = int(msg.get("h", 0))
        except (TypeError, ValueError):
            reply(writer, {"t": "size", "ok": False})
            return
        loop = asyncio.get_running_loop()
        ok, aw, ah = await loop.run_in_executor(None, apply_size, width, height)
        reply(writer, {"t": "size", "ok": ok, "w": aw, "h": ah})
    elif kind == "text":
        # Text the visitor composed with the input method on their own
        # machine. It cannot be typed key by key (the remote speaks keys, and
        # Chinese is not a key), so it goes in through the clipboard - see
        # type_text.py.
        text = msg.get("text", "")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            reply(writer, {"t": "text", "ok": False})
            return
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, deliver_text, text)
        reply(writer, {"t": "text", "ok": bool(ok)})
    elif kind == "clipboard-set":
        ok = False
        if CLIPBOARD_IN:
            ok = await asyncio.to_thread(write_clipboard, msg.get("text"), msg.get("paste") is True)
        reply(writer, {"t": "clipboard-set", "ok": ok, "id": msg.get("id")})
    elif kind == "clipboard-get" and CLIPBOARD_OUT:
        text = await asyncio.to_thread(read_clipboard)
        reply(writer, {"t": "clipboard", "text": text})
    elif kind == "clipboard-copy" and CLIPBOARD_OUT:
        text = await asyncio.to_thread(copy_selection, msg.get("cut") is True)
        reply(writer, {"t": "clipboard", "text": text, "copied": True})
    elif kind == "windows":
        # The list the session keeps up to date - no need to go and look.
        reply(writer, {"t": "windows", "windows": STATE["windows"]})
    elif kind == "activate":
        # The visitor clicked a window in the bar: put it back on the screen.
        window_id = msg.get("id", "")
        if not isinstance(window_id, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", window_id):
            reply(writer, {"t": "activate", "ok": False})
            return
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, activate_window, window_id)
        reply(writer, {"t": "activate", "ok": bool(ok)})
    elif kind == "bye":
        # One page is going away. The session only ends when the *last*
        # visitor leaves: a visitor who closes a second tab must not end
        # the session of the visitor who is still watching.
        if STATE["clients"] > 1:
            log("one visitor said goodbye; somebody is still watching")
        else:
            STATE["bye_at"] = time.time()
            mark_ending()
            log("visitor said goodbye")
        reply(writer, {"t": "bye", "ok": True})
    elif kind == "quit":
        # The visitor pressed "leave": end the session right away.
        log("the visitor asked to leave")
        mark_ending()
        reply(writer, {"t": "quit", "ok": True})
        await asyncio.sleep(0.5)
        os._exit(0)
    elif kind == "ended":
        # The program was closed. Say so to everybody who is watching, so
        # that the visitor gets an explanation instead of a dead end.
        log("the session was ended (%s)" % msg.get("reason", "program"))
        broadcast({"t": "ended", "reason": msg.get("reason", "program")})


async def handle_client(reader, writer):
    peer = writer.get_extra_info("peername")
    STATE["clients"] += 1
    STATE["writers"].add(writer)
    if STATE["first_client"] is None:
        STATE["first_client"] = time.time()
        log(f"visitor connected from {peer}")
        # The server uses this to tell "handed out but nobody showed up" from
        # "really in use".
        try:
            with open(VISITOR_MARKER, "w", encoding="utf-8") as handle:
                handle.write(str(time.time()))
        except OSError:
            pass
    STATE["bye_at"] = None          # a (re)connection cancels a pending goodbye
    clear_ending()                 # ...and the session is good again

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                await handle_message(msg, writer)
    except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
        pass
    except Exception as exc:                       # noqa: BLE001
        log(f"client error: {exc}")
    finally:
        STATE["clients"] = max(0, STATE["clients"] - 1)
        STATE["writers"].discard(writer)
        log("visitor disconnected")
        if STATE["clients"] == 0:
            # Nobody is watching any more, so the session is over: the
            # container goes away and the next visitor gets a clean
            # environment. A visitor who comes back within the grace period
            # cancels this again when they reconnect.
            if STATE["bye_at"] is None:
                STATE["bye_at"] = time.time()
                mark_ending()
        try:
            writer.close()
        except Exception:
            pass


async def watchdog():
    """End the session once the visitor is gone."""
    claimed_at = None
    while True:
        await asyncio.sleep(2)
        now = time.time()

        # Keep the window list fresh. It is what the bar at the bottom of
        # the page shows, and it is how a program that puts its own window
        # away can be found again. Nobody is watching? Then there is no
        # point in going and looking.
        if STATE["clients"] > 0:
            await refresh_windows()

        # The program was closed. Say so before the session goes away, so
        # that the visitor gets an explanation instead of a dead end.
        if program_gone():
            log("the program was closed; ending the session")
            broadcast({"t": "ended", "reason": "program"})
            mark_ending()
            # Give the message a moment to reach the browser.
            await asyncio.sleep(0.5)
            os._exit(0)

        if STATE["first_client"] is None:
            # A container that has been handed to somebody but that nobody has
            # opened yet is given the boot grace period, then it is dropped.
            # If the server takes the claim back (because the visitor never
            # showed up), the container goes back to waiting in the pool.
            if not os.path.exists(CLAIMED_MARKER):
                if claimed_at is not None:
                    log("the claim was withdrawn; waiting in the pool again")
                claimed_at = None
                continue
            if claimed_at is None:
                claimed_at = now
                log("the container was claimed; waiting for the visitor")
            if now - claimed_at > BOOT_GRACE:
                log("the visitor never showed up; ending the session")
                os._exit(0)
            continue

        if STATE["bye_at"] is not None and now - STATE["bye_at"] > BYE_GRACE:
            log("visitor left; ending the session")
            mark_ending()
            os._exit(0)

        if now - STATE["last_msg"] > RESET_GRACE:
            log(f"no heartbeat for {RESET_GRACE:.0f}s; ending the session")
            mark_ending()
            os._exit(0)


async def watch_clipboard():
    previous = None
    while True:
        await asyncio.sleep(0.5)
        if not STATE["writers"]:
            previous = None
            continue
        text = await asyncio.to_thread(read_clipboard)
        if text != previous:
            previous = text
            broadcast({"t": "clipboard", "text": text})


async def main():
    server = await asyncio.start_server(handle_client, "0.0.0.0", 5902, limit=524288)
    log(f"control channel listening on 5902 (session {SESSION_ID})")

    # Readiness marker. The health check looks at this file rather than
    # connecting to the control port: a probe connection would look like a
    # visitor arriving and disconnecting, and would keep resetting the
    # goodbye timer.
    try:
        with open(READY_MARKER, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError:
        pass

    asyncio.create_task(watchdog())
    asyncio.create_task(watch_cursor())
    if CLIPBOARD_OUT:
        asyncio.create_task(watch_clipboard())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
