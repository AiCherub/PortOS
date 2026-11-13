#!/usr/bin/env python3
"""
Write the openbox configuration the session runs with.

openbox draws the window decorations the visitor sees. The title bar keeps
its minimise button - a program that can be put away is a program that can
be brought back, because the viewer keeps a bar with the session's windows
at the bottom of the browser window.

What is changed is the menu on the title bar's right click (the same one
comes up on the window icon and on Alt+Space). openbox's own menu is in
English and it has two entries that make no sense here:

  * "Send to desktop" moves the window to another virtual desktop. The
    session is configured with a single desktop;
  * "Roll up/down" turns the window into a title bar, which in a session
    that is one program on one screen is just confusing.

So the menu is replaced with one of our own: the same actions, in Chinese,
with the two of them left out. The session uses the Win10 Dark Openbox theme.

The animation of a window that is minimized is turned off as well. Every
frame of it is another picture for the visitor to receive and another one
for the compositor to draw, and the session is on a slow line.

The other right click - the one on the desktop itself - is taken away
altogether. It brings up the menu the distribution ships (Applications,
Accessories, Terminals, Log Out ...), and none of that belongs to the
program the visitor came for: the entries are the distribution's own
programs, which are not in this image, and "Log Out" would end the session
for good instead of logging anybody out of anything. So the desktop's
right button does nothing at all.

    python3 openbox-config.py [source] [target]
"""

import os
import re
import sys
import xml.etree.ElementTree as ET

SOURCE = "/etc/xdg/openbox/rc.xml"
TARGET = "/tmp/openbox-rc.xml"

# openbox does not read menu definitions out of rc.xml - they live in their
# own file, which rc.xml then points at.
MENU_FILE = "/tmp/openbox-menu.xml"

# The id our menu is known by inside openbox.
MENU_ID = "portos-client"

# The id of the menu the distribution ships under the name "root-menu".
# A right click on the desktop shows it.
ROOT_MENU_ID = "root-menu"

RC_NAMESPACE = "http://openbox.org/3.4/rc"
RC_NS = {"rc": RC_NAMESPACE}
DESKTOP_ACTIONS = {"gotodesktop", "sendtodesktop"}

THEME_FONT_FAMILIES = {
    "ActiveWindow": "Liberation Sans",
    "InactiveWindow": "Liberation Sans",
    "MenuHeader": "Noto Sans CJK SC",
    "MenuItem": "Noto Sans CJK SC",
    "ActiveOnScreenDisplay": "Noto Sans CJK SC",
    "InactiveOnScreenDisplay": "Noto Sans CJK SC",
}

# A mouse binding - openbox's <mousebind> element. It has children (the
# actions that are carried out), so it always has a closing tag, and the
# bindings do not nest inside each other.
MOUSEBIND_RE = re.compile(
    r"[ \t]*<mousebind\b[^>]*/>[ \t]*\r?\n?"
    r"|[ \t]*<mousebind\b[^>]*>.*?</mousebind>[ \t]*\r?\n?",
    re.DOTALL)

# The action inside a mouse binding that puts a menu on the screen.
SHOW_MENU_RE = re.compile(
    r"[ \t]*<action\s+name=\"ShowMenu\"\s*>\s*"
    r"<menu>\s*%s\s*</menu>\s*</action>[ \t]*\r?\n?"
    % re.escape(ROOT_MENU_ID),
    re.DOTALL)

# The menu the visitor gets instead of openbox's own client menu.
CLIENT_MENU = """<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/openbox_menu">
  <menu id="{id}" label="窗口">
    <item label="置于顶层">
      <action name="Raise"/>
    </item>
    <item label="置于底层">
      <action name="Lower"/>
    </item>
    <item label="最大化／还原">
      <action name="ToggleMaximize"/>
    </item>
    <item label="最小化">
      <action name="Iconify"/>
    </item>
    <item label="移动">
      <action name="Move"/>
    </item>
    <item label="调整大小">
      <action name="Resize"/>
    </item>
    <separator/>
    <item label="关闭">
      <action name="Close"/>
    </item>
  </menu>
</openbox_menu>
""".format(id=MENU_ID)


def patch_client_menu(text):
    """Point every place that shows openbox's client menu at our own menu."""
    return text.replace("<menu>client-menu</menu>", "<menu>%s</menu>" % MENU_ID)


def patch_animations(text):
    """Turn the window animations off.

    openbox animates a window that is minimized - it slides the window
    into its icon. Every frame of that is another picture for the visitor
    to receive and another picture for the compositor to draw, and the
    session is on a slow line to the browser. A window that is minimized
    is simply gone; the browser's window bar brings it back.

    The animation is the only one openbox has: it is switched off in the
    theme's settings, and the theme that is used defines no others.
    """
    return text.replace("<animateIconify>yes</animateIconify>",
                        "<animateIconify>no</animateIconify>")


def patch_menu_files(text, menu_file=MENU_FILE):
    """Tell openbox about the file our menu is written in.

    The <menu> element that holds the menu *settings* is the only one with
    <file> children, so the new file goes in next to the first of those.
    """
    if "<file>%s</file>" % menu_file in text:
        return text
    patched, count = re.subn(r"(<file>[^<]*</file>)",
                             r"\1\n  <file>%s</file>" % menu_file,
                             text, count=1)
    if not count:
        print("warning: no menu file list in the configuration", file=sys.stderr)
    return patched


def patch_theme(root):
    """Select Win10 Dark and use the fonts from its preview."""
    theme = root.find("rc:theme", RC_NS)
    theme.find("rc:name", RC_NS).text = "Win10-Dark"
    for font in theme.findall("rc:font", RC_NS):
        family = THEME_FONT_FAMILIES.get(font.get("place"))
        if family is None:
            continue
        for setting, value in (("name", family), ("size", "10"), ("weight", "normal")):
            element = font.find(f"rc:{setting}", RC_NS)
            if element is None:
                element = ET.SubElement(font, f"{{{RC_NAMESPACE}}}{setting}")
            element.text = value


def patch_root_menu(text):
    """Take the desktop's right click away from openbox's root menu.

    The menu comes up through a mouse binding, so the binding is what
    goes. It is found by the menu it shows rather than by the button it
    is on, so it is found whatever button the distribution happens to
    use for it.
    """

    def without_root_menu(mousebind):
        block = mousebind.group(0)
        if ROOT_MENU_ID not in block:
            return block
        stripped = SHOW_MENU_RE.sub("", block)
        if "<action" not in stripped:
            # Nothing else was bound to that button, so the binding goes
            # with it: the button does nothing at all.
            return ""
        return stripped

    return MOUSEBIND_RE.sub(without_root_menu, text)


def patch_desktops(root):
    """Use one desktop and remove virtual desktop shortcuts."""
    desktops = root.find("rc:desktops", RC_NS)
    desktops.find("rc:number", RC_NS).text = "1"
    desktops.find("rc:firstdesk", RC_NS).text = "1"
    root.find("rc:mouse/rc:screenEdgeWarpTime", RC_NS).text = "0"
    root.find("rc:menu/rc:manageDesktops", RC_NS).text = "no"

    for tag in ("keybind", "mousebind"):
        for parent in root.findall(".//rc:%s/.." % tag, RC_NS):
            for binding in parent.findall("rc:" + tag, RC_NS):
                for action in binding.findall("rc:action", RC_NS):
                    if action.get("name", "").lower() in DESKTOP_ACTIONS:
                        binding.remove(action)
                if not binding.findall("rc:*", RC_NS):
                    parent.remove(binding)


def build(source=SOURCE, target=TARGET):
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()

    text = patch_client_menu(text)
    text = patch_menu_files(text)
    text = patch_animations(text)
    text = patch_root_menu(text)

    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(text, parser)
    ET.register_namespace("", RC_NAMESPACE)
    patch_theme(root)
    patch_desktops(root)
    ET.indent(root, space="  ")

    with open(target, "w", encoding="utf-8") as handle:
        handle.write(ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n")

    with open(MENU_FILE, "w", encoding="utf-8") as handle:
        handle.write(CLIENT_MENU)

    print("openbox configuration written to %s (menu: %s)" % (target, MENU_FILE))
    return 0


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else SOURCE
    target = sys.argv[2] if len(sys.argv) > 2 else TARGET
    if not os.path.exists(source):
        print("no openbox configuration at %s" % source, file=sys.stderr)
        sys.exit(1)
    sys.exit(build(source, target))
