#!/usr/bin/env python3
"""
Type text into the program that is running inside this session.

The visitor's own input method - the one in their browser, the one they use
everywhere else - turns Chinese into text. That text arrives here over the
control channel and has to end up in the remote window.

Two things make this less obvious than it looks:

  * The RFB protocol has no "insert this string" message. Everything that
    reaches the remote is a key press, so a string has to be typed.
  * `xdotool type` cannot type Chinese. It works on bytes, so UTF-8 text
    comes out as mojibake ("你好世界" turns into "好世").

What does work is the way every remote desktop does it: put the text on the
X11 clipboard and paste it. Wine bridges the X11 clipboard to the Windows
clipboard, so Ctrl+V inside the program inserts the text as it is - Chinese
included, rendered with the fonts that are in the image.

Plain ASCII is typed key by key, because that needs no clipboard and behaves
like real typing (undo history, cursor movement, and so on). Everything else
is pasted in one piece - mixing the two would let the paste overtake the
keys that were typed before it.

    python3 type_text.py "你好"      # type it into the focused window
"""

import os
import subprocess
import sys
import threading
import time

DISPLAY = os.environ.get("DISPLAY", ":1")

# Only one text may be on its way into the program at a time. Two of them
# would fight over the clipboard, and the first one would paste the second
# one's text.
LOCK = threading.Lock()

# How long to wait for the clipboard to really carry the text before pasting.
CLIPBOARD_TIMEOUT = 2.0
# How long to let the program take the pasted text before the old clipboard
# content is put back.
RESTORE_DELAY = 0.6


def log(message):
    print(f"[type-text] {message}", flush=True)


def run(command, timeout=10):
    """Run a command and return (returncode, stdout)."""
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, env=env)
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        return 1, ""
    except OSError as exc:
        log(f"{command[0]} failed: {exc}")
        return 1, ""


def is_ascii(text):
    return all(ord(ch) < 128 for ch in text)


def clipboard_get():
    """The current clipboard text, or None when there is no clipboard."""
    code, out = run(["xclip", "-selection", "clipboard", "-target", "UTF8_STRING", "-o"], timeout=2)
    if code != 0:
        return None
    return out


def clipboard_set(text):
    """Put text on the clipboard and wait until it can be read back.

    xclip keeps owning the selection after it is started, so it has to be
    left running - that is what makes the paste work a moment later.
    """
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    try:
        proc = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-target", "UTF8_STRING", "-i"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env,
        )
    except OSError as exc:
        log(f"could not start xclip: {exc}")
        return False

    try:
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.flush()
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        # xclip may have forked already; the child still reads the pipe.
        pass

    deadline = time.time() + CLIPBOARD_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.05)
        if clipboard_get() == text:
            return True
    log("the clipboard did not take the text")
    return False


def focused_window():
    """The window that currently has the X input focus ("" if there is none)."""
    code, out = run(["xdotool", "getwindowfocus"])
    if code != 0:
        return ""
    return out.strip()


def root_window():
    """The id of the root window - what the focus falls back to when no
    window has it."""
    code, out = run(["xwininfo", "-root"])
    if code != 0:
        return ""
    for line in out.splitlines():
        if line.startswith("Root window id:"):
            return line.split(":", 1)[1].strip()
    return ""


def program_window():
    """The program's own window: the largest one on the desktop.

    Same rule as fit-window.sh - the showcased program is the biggest thing
    on the screen.
    """
    code, out = run(["wmctrl", "-l", "-G"])
    if code != 0:
        return ""
    best = ""
    best_area = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            width, height = int(parts[4]), int(parts[5])
        except ValueError:
            continue
        if width <= 0 or height <= 0:
            continue
        area = width * height
        if area > best_area:
            best_area, best = area, parts[0]
    return best


def ensure_focus():
    """Make sure the program has the input focus.

    Normally it does: the visitor clicked into it. But a visitor may type
    straight away, and then nothing has the focus (it sits on the root
    window) and the text would go nowhere.
    """
    window = focused_window()
    root = root_window()
    if window and window not in ("0", root):
        return True
    window = program_window()
    if not window:
        return False
    log(f"no window had the focus; giving it to {window}")
    run(["xdotool", "windowactivate", "--sync", window])
    run(["xdotool", "windowfocus", window])
    time.sleep(0.2)
    return True


def type_ascii(text):
    """Type ASCII text key by key."""
    code, _ = run(["xdotool", "type", "--delay", "15", text], timeout=30)
    if code != 0:
        log("xdotool could not type the text")
        return False
    return True


def paste(text):
    """Put text on the clipboard and paste it into the focused window."""
    if not clipboard_set(text):
        return False
    run(["xdotool", "key", "--clearmodifiers", "ctrl+v"])
    return True


def schedule_restore(saved, pasted_text):
    """Give the clipboard back once the program has read it.

    The paste borrowed the clipboard, and the visitor may have had something
    in there that they copied out of the program - TigerVNC would even have
    copied it to their own machine. But the clipboard is only given back if it
    still holds what we put there: if the visitor copied something else in
    the meantime, that is theirs to keep.
    """

    def worker():
        time.sleep(RESTORE_DELAY)
        if clipboard_get() != pasted_text:
            return
        clipboard_set("" if saved is None else saved)

    threading.Thread(target=worker, daemon=True).start()


def deliver_text(text):
    """Type text into the program and report whether it went through."""
    if not text:
        return False

    with LOCK:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        ensure_focus()

        saved = clipboard_get()
        if is_ascii(text):
            ok = type_ascii(text)
        else:
            # Anything that is not plain ASCII is pasted in one piece, even
            # the ASCII around it. Typing part of the text and pasting the
            # rest can put the two halves in the wrong order: the program
            # takes its time with the typed keys, and the paste can overtake
            # them.
            pasted = paste(text)
            ok = pasted
            if pasted:
                # Keep the shared lock until temporary IME contents are gone.
                time.sleep(RESTORE_DELAY)
                if clipboard_get() == text:
                    clipboard_set("" if saved is None else saved)

    log(f"typed {len(text)} characters")
    return ok


def main():
    text = " ".join(sys.argv[1:])
    if not text:
        print("usage: type_text.py <text>", file=sys.stderr)
        return 2
    return 0 if deliver_text(text) else 1


if __name__ == "__main__":
    sys.exit(main())
