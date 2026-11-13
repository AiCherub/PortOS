#!/usr/bin/env python3
"""
Watch the shape of the pointer on the X server and report it.

The picture the browser gets carries no pointer - the X server runs with
-nocursor - because a pointer that is drawn into the picture is always one
round trip behind the visitor's mouse. The browser shows the pointer of the
visitor's own machine instead, and this is what tells it which shape that
pointer should have: an I-beam over a text field, a resize arrow over a
window border, and so on.

How the shape is found out:

  * TigerVNC cannot pass the pointer on. It has no cursor pseudo-encoding,
    so the browser never receives the pointer image.
  * XFIXES can hand out the pointer image, but on TigerVNC that image does
    not agree with what the X server actually draws - the bits come out
    scrambled. So the image is of no use here.
  * What XFIXES does tell us reliably is the size of the pointer and where
    its hot spot sits inside it. That is enough to tell the shapes apart:
    the arrow has its hot spot in the top left corner, the I-beam sits in
    the middle of a narrow tall image, the resize arrows have their hot
    spot on the edge they point at.

Output: one JSON object per line on stdout, whenever the pointer changes:

    {"shape": "text", "w": 9, "h": 16, "hotx": 4, "hoty": 8}

The control channel picks these up and passes them on to the browser.
"""

import ctypes
import json
import os
import sys
import time

DISPLAY = os.environ.get("DISPLAY", ":1")
# How often to look. The shape changes when the pointer crosses into
# another kind of area, so this is how long the browser may show the
# previous shape. 50ms is well below what an eye notices.
POLL = 0.05


# --------------------------------------------------------------- the X server

def load_x11():
    """The two libraries we need: the X server itself and XFIXES."""
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]

    fixes = ctypes.CDLL("libXfixes.so.3")
    fixes.XFixesGetCursorImage.restype = ctypes.c_void_p
    fixes.XFixesGetCursorImage.argtypes = [ctypes.c_void_p]
    return x11, fixes


class CursorReader:
    """Asks the X server for the pointer that is under the pointer."""

    def __init__(self):
        self.x11, self.fixes = load_x11()
        self.display = self.x11.XOpenDisplay(DISPLAY.encode())
        if not self.display:
            raise RuntimeError("the X server is not answering on %s" % DISPLAY)

    def read(self):
        """The pointer under the pointer: (w, h, hotx, hoty, serial).

        The serial is a counter of the X server: it changes whenever the
        pointer changes, so it says "this is a different pointer" without
        having to compare the shape.
        """
        image = self.fixes.XFixesGetCursorImage(self.display)
        if not image:
            return None
        raw = ctypes.string_at(image, 48)
        # The layout is the one of XFixesCursorImage (see Xfixes.h):
        #   short x, y; short width, height; short xhot, yhot;
        #   (padding); unsigned long cursor_serial; ... ; pixels
        width = int.from_bytes(raw[4:6], "little")
        height = int.from_bytes(raw[6:8], "little")
        hot_x = int.from_bytes(raw[8:10], "little")
        hot_y = int.from_bytes(raw[10:12], "little")
        serial = int.from_bytes(raw[16:20], "little")
        return width, height, hot_x, hot_y, serial


# ------------------------------------------------------------- telling shapes

def classify(width, height, hot_x, hot_y):
    """What kind of pointer is this?

    The hot spot is the point of the pointer image that sits on the
    position: for the arrow it is the tip, for the resize arrows it is the
    tip of the arrow that points the way.
    """
    if width <= 0 or height <= 0:
        return "default"

    middle_x = (width - 1) / 2
    middle_y = (height - 1) / 2
    centred_x = abs(hot_x - middle_x) <= 1
    centred_y = abs(hot_y - middle_y) <= 1
    at_left = hot_x <= 1
    at_right = hot_x >= width - 2
    at_top = hot_y <= 1
    at_bottom = hot_y >= height - 2

    # The arrow: the hot spot is the tip of it, in the top left corner.
    if at_top and at_left:
        return "default"

    # A corner: the pointer points diagonally.
    if (at_top or at_bottom) and (at_left or at_right):
        if (at_top and at_left) or (at_bottom and at_right):
            return "nwse-resize"
        return "nesw-resize"

    # A side: the pointer points sideways, or up and down.
    if centred_y and (at_left or at_right):
        return "ew-resize"
    if centred_x and (at_top or at_bottom):
        return "ns-resize"

    # In the middle: the I-beam over a text field, and the four-headed
    # arrow that drags a window around.
    if centred_x and centred_y:
        if width <= height * 0.75:
            return "text"
        return "move"

    return "default"


def main():
    try:
        reader = CursorReader()
    except Exception as exc:                       # noqa: BLE001
        print("no cursor watch: %s" % exc, file=sys.stderr, flush=True)
        return 0

    last_serial = None
    while True:
        try:
            info = reader.read()
        except Exception:                          # noqa: BLE001
            time.sleep(1)
            continue
        if info is None:
            time.sleep(1)
            continue

        width, height, hot_x, hot_y, serial = info
        if serial != last_serial:
            last_serial = serial
            print(json.dumps({
                "shape": classify(width, height, hot_x, hot_y),
                "w": width, "h": height, "hotx": hot_x, "hoty": hot_y,
            }), flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
