"""Text clipboard operations serialized with IME's temporary clipboard use."""
from type_text import LOCK, clipboard_get, clipboard_set, ensure_focus, run
import time

MAX_CLIPBOARD = 65536  # UTF-8 bytes


def read_clipboard():
    with LOCK:
        text = clipboard_get()
        if text is None or len(text.encode('utf-8')) > MAX_CLIPBOARD:
            return None
        return text


def write_clipboard(text, paste=False):
    if not isinstance(text, str) or '\x00' in text:
        return False
    if len(text.encode('utf-8')) > MAX_CLIPBOARD:
        return False
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    with LOCK:
        # Preserve Wine's existing clipboard owner/formats when text matches.
        # Reclaiming the selection needlessly can race Wine's format cache.
        if clipboard_get() != text:
            # Wine's delayed clipboard rendering may reclaim ownership just
            # after a copy. Retry once after that owner change has settled.
            if not clipboard_set(text):
                time.sleep(0.1)
                if not clipboard_set(text):
                    return False
        if paste:
            ensure_focus()
            return run(['xdotool', 'key', '--clearmodifiers', 'ctrl+v'])[0] == 0
        return True


def copy_selection(cut=False):
    with LOCK:
        ensure_focus()
        run(['xdotool', 'key', '--clearmodifiers', 'ctrl+x' if cut else 'ctrl+c'])
        time.sleep(0.15)
        text = clipboard_get()
        return text if text is not None and len(text.encode('utf-8')) <= MAX_CLIPBOARD else None
