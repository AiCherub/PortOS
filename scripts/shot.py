#!/usr/bin/env python3
"""
Take a screenshot of a page through the Chrome DevTools Protocol.

Used to see what the visitor actually gets: the picture of the remote
program, the bar underneath it, and any error the page writes to the console.

    msedge --headless=new --remote-debugging-port=9222 about:blank &
    python3 shot.py http://127.0.0.1:8081/ out.png 15

The browser runs on the host, so when this script runs inside the server
container the endpoint is found under host.docker.internal. Set CDP to point
somewhere else.
"""

import base64
import ipaddress
import json
import os
import struct
import sys
import time
import urllib.parse
import urllib.request


def devtools_host(host, port):
    """The Host header the browser's DevTools endpoint insists on.

    Edge answers "500 Internal Server Error" when the Host header is a name
    it cannot parse as an address, so a request that reaches the browser as
    "host.docker.internal:9222" has to say "127.0.0.1:9222" instead. The
    connection itself still goes to the name it was given.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"127.0.0.1:{port}"
    return f"{host}:{port}"


def fetch_json(url, timeout=10):
    """Read a JSON document from the DevTools endpoint."""
    parts = urllib.parse.urlsplit(url)
    request = urllib.request.Request(url)
    request.add_header("Host", devtools_host(parts.hostname or "127.0.0.1",
                                             parts.port or 80))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def find_cdp():
    """Locate the DevTools endpoint.

    Tries the configured address first, then localhost, then the host as seen
    from inside a container.
    """
    candidates = []
    configured = os.environ.get("CDP")
    if configured:
        candidates.append(configured.rstrip("/"))
    candidates += ["http://127.0.0.1:9222", "http://host.docker.internal:9222"]
    for base in candidates:
        try:
            fetch_json(base + "/json/version", timeout=3)
            return base
        except Exception:                          # noqa: BLE001
            continue
    return candidates[0]


DEBUG = find_cdp()


def browser_socket():
    """The browser's own DevTools socket, used to open and close tabs."""
    try:
        info = fetch_json(DEBUG + "/json/version")
    except Exception as exc:                      # noqa: BLE001
        raise RuntimeError(
            f"no browser is listening on {DEBUG}\n"
            "  start one with: msedge --headless=new "
            "--remote-debugging-port=9222 about:blank") from exc
    return CDP(info["webSocketDebuggerUrl"])


def connect_host(url):
    """The address to open a DevTools WebSocket to.

    The browser hands out "ws://localhost:9222/..." even when it was reached
    as host.docker.internal. Inside a container that name is the container
    itself, so the connection has to go to the address the endpoint was
    really found at.
    """
    host = urllib.parse.urlsplit(url).hostname or "127.0.0.1"
    if host in ("localhost", "127.0.0.1", "::1"):
        outside = urllib.parse.urlsplit(DEBUG).hostname
        if outside and outside not in ("localhost", "127.0.0.1", "::1"):
            return outside
    return host


class CDP:
    """Just enough WebSocket to drive the DevTools protocol."""

    def __init__(self, url):
        without = url.split("//", 1)[-1]
        hostport, _, path = without.partition("/")
        host, _, port = hostport.partition(":")
        target = connect_host(url)
        self.sock = __import__("socket").create_connection(
            (target, int(port or 80)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {devtools_host(host, int(port or 80))}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("the handshake failed")
            data += chunk
        if " 101 " not in data.split(b"\r\n", 1)[0].decode(errors="replace"):
            raise RuntimeError("the handshake failed")
        self.buffer = b""
        self.next_id = 0

    def _read(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("the connection closed")
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def send(self, method, params=None):
        self.next_id += 1
        message = json.dumps({"id": self.next_id, "method": method,
                              "params": params or {}})
        payload = message.encode()
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)
        return self.next_id

    def recv(self, timeout=30):
        self.sock.settimeout(timeout)
        first, second = self._read(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(length) if length else b""
        if opcode == 0x8:
            raise RuntimeError("the browser closed the connection")
        return payload.decode(errors="replace")

    def call(self, method, params=None, timeout=30):
        """Send a command and wait for its answer."""
        wanted = self.send(method, params)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.recv(timeout=max(1, deadline - time.time())))
            if msg.get("id") == wanted:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise RuntimeError(f"{method}: no answer")


def main():
    url = sys.argv[1]
    out = sys.argv[2]
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 12

    targets = fetch_json(DEBUG + "/json/list")
    page = next(t for t in targets if t.get("type") == "page")
    print(f"  driving {page['url']}")

    cdp = CDP(page["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Log.enable")
    cdp.call("Emulation.setDeviceMetricsOverride",
             {"width": 1280, "height": 720, "deviceScaleFactor": 1,
              "mobile": False})
    cdp.call("Page.navigate", {"url": url})

    # Collect whatever the page says while it settles.
    errors = []
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            msg = json.loads(cdp.recv(timeout=max(1, deadline - time.time())))
        except Exception:                          # noqa: BLE001
            break
        method = msg.get("method", "")
        if method == "Runtime.exceptionThrown":
            detail = msg["params"]["exceptionDetails"]
            errors.append(detail.get("text", "") + " " +
                          str(detail.get("exception", {}).get("description", ""))[:300])
        elif method == "Log.entryAdded":
            entry = msg["params"]["entry"]
            if entry.get("level") in ("error", "warning"):
                errors.append(f"{entry.get('level')}: {entry.get('text')}")
        elif method == "Runtime.consoleAPICalled":
            args = msg["params"].get("args", [])
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
            print(f"  console: {text}")

    shot = cdp.call("Page.captureScreenshot", {"format": "png"})
    with open(out, "wb") as handle:
        handle.write(base64.b64decode(shot["data"]))
    print(f"  saved {out}")

    where = cdp.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
    print(f"  the page ended up at {where.get('result', {}).get('value')}")

    if errors:
        print("\n  the page reported:")
        for line in errors:
            print(f"    - {line}")
    else:
        print("  the page reported no errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
