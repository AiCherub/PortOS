#!/usr/bin/env python3
"""
Tell the control channel that the session is over, and why.

The entrypoint is the one that notices that the program was closed, and it
notices faster than the control channel's own watchdog. It hands the news
over through this little script, so that the visitor learns why the session
ended before the container goes away.
"""

import json
import os
import socket
import sys

PORT = int(os.environ.get("CTL_TCP_PORT", "5902"))
REASON = sys.argv[1] if len(sys.argv) > 1 else "program"


def main():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=3) as sock:
            sock.sendall(
                (json.dumps({"t": "ended", "reason": REASON}) + "\n").encode())
    except OSError as exc:
        print(f"could not reach the control channel: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
