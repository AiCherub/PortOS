#!/usr/bin/env python3
"""
End-to-end verification of a PortOS deployment.

Runs against a live stack (default http://127.0.0.1:8081) and checks the six
things that matter:

  1. there is a single door, and it leads straight into the program
  2. the viewer loads as a whole and receives a VNC framebuffer update
  3. the remote resolution follows the requested size  (requirement 3)
  4. the session is destroyed once the visitor disconnects  (requirement 4)
  5. the next visitor gets a clean environment
  6. closing the program ends the session too

Usage:
    python3 verify.py [--url http://127.0.0.1:8081]
"""

import argparse
import base64
import json
import os
import posixpath
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appwindow import app_window, close                    # noqa: E402

BASE = "http://127.0.0.1:8081"

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = []


def check(name, condition, detail=""):
    print(f"  [{PASS if condition else FAIL}] {name}" + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(name)
    return condition


# --------------------------------------------------------------- tiny WS client

class WebSocket:
    """Just enough WebSocket to talk to the control and VNC channels.

    websockify only accepts binary frames, so everything is sent as bytes
    even though the payload happens to be JSON.
    """

    def __init__(self, host, port, path, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.stream = b""
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: binary\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("the handshake failed")
            data += chunk
        headers, self.wire = data.split(b"\r\n\r\n", 1)
        status = headers.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raise RuntimeError(f"the handshake failed: {status}")

    def _read(self, count):
        while len(self.wire) < count:
            chunk = self.sock.recv(count - len(self.wire))
            if not chunk:
                raise RuntimeError("the connection closed")
            self.wire += chunk
        data, self.wire = self.wire[:count], self.wire[count:]
        return data

    def send(self, text):
        # The control channel speaks newline-delimited JSON.
        self.send_bytes((text + "\n").encode())

    def send_bytes(self, payload):
        header = bytearray([0x82])          # FIN + binary frame
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_frame(self, timeout=8):
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
            raise RuntimeError("the server closed the connection")
        return opcode, payload

    def recv(self, timeout=8):
        _, payload = self.recv_frame(timeout)
        return payload.decode(errors="replace")

    def read_bytes(self, count):
        while len(self.stream) < count:
            opcode, payload = self.recv_frame()
            if opcode in (0x0, 0x1, 0x2):
                self.stream += payload
        data, self.stream = self.stream[:count], self.stream[count:]
        return data

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------- helpers

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(method, path, payload=None):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"error": body}
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {url}: {exc}") from exc


def _reachable_host(base):
    """Make the session base reachable from where this script runs.

    The session container publishes its port on the docker host. When the
    verifier runs inside the docker network, that port is reachable as
    host.docker.internal rather than under the compose service name.
    """
    scheme, _, rest = base.partition("//")
    host, _, tail = rest.partition("/")
    hostname, _, port = host.partition(":")
    try:
        socket.create_connection((hostname, int(port or 80)), timeout=3).close()
        return base
    except OSError:
        pass
    replacement = f"host.docker.internal:{port}" if port else "host.docker.internal"
    return f"{scheme}//{replacement}/{tail}" if tail else f"{scheme}//{replacement}"


def enter():
    """Follow the single door and return where it leads.

    Returns (status, location, session id, base url of the session).
    The base url works both behind a reverse proxy (/s/<sid>/...) and when
    the session container is reached directly.
    """
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(BASE + "/", timeout=120) as response:
            location = response.geturl()
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") or ""
        if exc.code not in (301, 302, 303, 307, 308):
            return exc.code, location, "", ""
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {BASE}: {exc}") from exc

    # Behind a reverse proxy the redirect is relative; in direct mode it is
    # an absolute address on the published port.
    if location and not location.startswith("http"):
        location = BASE + location

    parts = [p for p in location.split("/") if p]
    sid = ""
    for index in range(len(parts) - 1):
        if parts[index] == "s" and re.fullmatch(r"[0-9a-f]{32}", parts[index + 1]):
            sid = parts[index + 1]
            break
    if not sid and "sid=" in location:
        sid = location.split("sid=", 1)[1].split("&", 1)[0]
    base = location.rsplit("/viewer.html", 1)[0]
    return 302, location, sid, _reachable_host(base)


def ws_target(base):
    """Split a session base url into the host, port and path prefix."""
    without_scheme = base.split("//", 1)[-1]
    hostport, _, path = without_scheme.partition("/")
    host, _, port = hostport.partition(":")
    prefix = "/" + path.strip("/")
    if prefix != "/":
        prefix += "/"
    return host, int(port or 80), prefix


def check_vnc_stream(base):
    """Request a framebuffer update through the viewer's VNC WebSocket."""
    host, port, prefix = ws_target(base)
    with closing(WebSocket(host, port, f"{prefix}websockify?token=vnc")) as vnc:
        version = vnc.read_bytes(12)
        if version != b"RFB 003.008\n":
            raise RuntimeError(f"unexpected VNC version: {version!r}")
        vnc.send_bytes(version)
        security_types = vnc.read_bytes(vnc.read_bytes(1)[0])
        if 1 not in security_types:
            raise RuntimeError(f"VNC did not offer the expected security type: {security_types!r}")
        vnc.send_bytes(b"\x01")
        if vnc.read_bytes(4) != b"\x00" * 4:
            raise RuntimeError("VNC rejected the connection")
        vnc.send_bytes(b"\x01")  # ClientInit with the shared flag
        width, height, name_length = struct.unpack(">HH16xI", vnc.read_bytes(24))
        vnc.read_bytes(name_length)
        vnc.send_bytes(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))
        message, rectangles = struct.unpack(">BxH", vnc.read_bytes(4))
        if message != 0 or rectangles == 0:
            raise RuntimeError(f"unexpected VNC framebuffer update: {message=}, {rectangles=}")
        vnc.read_bytes(12)  # First rectangle header


def framebuffer_size(sid):
    """Ask the session container what its VNC framebuffer really is."""
    status, _ = api("GET", f"/api/sessions/{sid}")
    if status != 200:
        return None
    container = subprocess.run(
        ["docker", "exec", f"portos-{sid}", "python3", "/opt/portos/session/probe-size.py"],
        capture_output=True, text=True, timeout=30,
    )
    if container.returncode != 0:
        return None
    parts = container.stdout.split()
    if len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


def wait_for_framebuffer(sid, want, timeout=90):
    """Give the session a moment, then poll until the size settles."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = framebuffer_size(sid)
        if last == want:
            return last
        time.sleep(2)
    return last


def crawl_modules(base):
    """Walk the viewer's ES module graph the way the browser does.

    Returns (number of modules fetched, list of paths that failed).
    """
    base = base.rstrip("/") + "/"
    seen = set()
    queue = ["viewer/viewer.js", "core/rfb.js"]
    broken = []

    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            with urllib.request.urlopen(base + path, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
        except Exception as exc:                    # noqa: BLE001
            broken.append(f"{path} ({exc})")
            continue
        for match in re.finditer(r"from\s+[\"']([^\"']+)[\"']|import\s+[\"']([^\"']+)[\"']", body):
            reference = match.group(1) or match.group(2)
            if reference.startswith("."):
                folder = posixpath.dirname(path)
                queue.append(posixpath.normpath(posixpath.join(folder, reference)))

    return len(seen), broken


# ---------------------------------------------------------------------- tests

def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=BASE)
    args = parser.parse_args()
    BASE = args.url.rstrip("/")

    print(f"\nVerifying {BASE}\n")

    print("1. the single door")
    status, location, sid, sbase = enter()
    if not check("the entry point starts the configured program", status == 302 and bool(sid),
                 location):
        raise SystemExit(1)
    check("the entry point redirects into the viewer",
          "/viewer.html" in location, location)

    print("\n2. the environment was already prepared")
    # The viewer is an ES module graph: the browser pulls every file noVNC
    # needs at once. If anything in the request path is rate limited or
    # misrouted, the graph fails to load and the viewer never connects.
    modules, broken = crawl_modules(sbase)
    check("the whole viewer module graph loads", not broken,
          f"{modules} modules, {len(broken)} broken" + (f" {broken[:3]}" if broken else ""))
    try:
        check_vnc_stream(sbase)
    except (OSError, RuntimeError) as exc:
        check("the viewer receives a VNC framebuffer update", False, str(exc))
    else:
        check("the viewer receives a VNC framebuffer update", True)

    print("\n3. the remote resolution follows the browser window")
    size = wait_for_framebuffer(sid, (1280, 720))
    check("the session starts at the default size", size == (1280, 720), f"got {size}")

    ws_host, ws_port, ws_prefix = ws_target(sbase)
    ws = None
    try:
        ws = WebSocket(ws_host, ws_port, f"{ws_prefix}ctl?token=ctl")
        ws.send(json.dumps({"t": "ping"}))
        pong = ws.recv()
        check("the control channel answers", '"pong"' in pong, pong.strip()[:80])

        ws.send(json.dumps({"t": "size", "w": 900, "h": 640}))
        reply = json.loads(ws.recv())
        check("the resize is accepted", reply.get("ok") is True, json.dumps(reply))
        check("the server reports the new size", (reply.get("w"), reply.get("h")) == (900, 640),
              f"{reply.get('w')}x{reply.get('h')}")

        size = wait_for_framebuffer(sid, (900, 640), timeout=45)
        check("the VNC framebuffer really changed", size == (900, 640), f"got {size}")
        randr = subprocess.run(["docker", "exec", f"portos-{sid}", "xrandr", "--current"],
                               capture_output=True, text=True, timeout=30)
        check("the resized VNC output stays connected",
              randr.returncode == 0 and re.search(
                  r"^VNC-0 connected (?:primary )?900x640\+0\+0\b", randr.stdout, re.MULTILINE))

        # A bigger target must work as well, not only shrinking.
        ws.send(json.dumps({"t": "size", "w": 1600, "h": 900}))
        reply = json.loads(ws.recv())
        check("the session can grow as well", reply.get("ok") is True, json.dumps(reply))
        size = wait_for_framebuffer(sid, (1600, 900), timeout=45)
        check("the framebuffer grew to 1600x900", size == (1600, 900), f"got {size}")
    except Exception as exc:                       # noqa: BLE001
        check("the control channel works", False, str(exc))
    finally:
        if ws:
            try:
                ws.send(json.dumps({"t": "bye"}))
                time.sleep(1)
            except Exception:                      # noqa: BLE001
                pass
            ws.close()

    print("\n4. the session is destroyed when the visitor leaves")
    before = subprocess.run(["docker", "ps", "-q", "--filter", f"name=portos-{sid}"],
                             capture_output=True, text=True).stdout.strip()
    check("the session container is running", bool(before))

    print("   waiting for the session to be reclaimed (this takes up to ~90s)...")
    deadline = time.time() + 150
    gone = False
    while time.time() < deadline:
        alive = subprocess.run(["docker", "ps", "-q", "--filter", f"name=portos-{sid}"],
                               capture_output=True, text=True).stdout.strip()
        if not alive:
            gone = True
            break
        time.sleep(5)
    check("the container is gone once the visitor disconnected", gone)

    print("\n5. the next visitor gets a clean environment")
    status, session = api("POST", "/api/sessions")
    sid2 = session.get("sid")
    check("the session API starts the configured program", status == 201 and bool(sid2))
    if sid2:
        check("it is a different environment", sid2 != sid, sid2)

        ws2 = None
        try:
            sbase2 = BASE + session["url"].rsplit("/viewer.html", 1)[0]
            h2, p2, prefix2 = ws_target(sbase2)
            ws2 = WebSocket(h2, p2, f"{prefix2}ctl?token=ctl")
            ws2.send(json.dumps({"t": "size", "w": 1024, "h": 768}))
            reply = json.loads(ws2.recv())
            check("the new session takes the requested size",
                  (reply.get("w"), reply.get("h")) == (1024, 768), json.dumps(reply))
            size = wait_for_framebuffer(sid2, (1024, 768))
            check("the new session runs at its own size", size == (1024, 768), f"got {size}")
        except Exception as exc:                   # noqa: BLE001
            check("the new session can be resized", False, str(exc))
        finally:
            if ws2:
                try:
                    ws2.send(json.dumps({"t": "bye"}))
                    time.sleep(1)
                except Exception:                  # noqa: BLE001
                    pass
                ws2.close()
        subprocess.run(["docker", "rm", "-f", f"portos-{sid2}"],
                       capture_output=True, text=True)

    print("\n6. closing the program ends the session too")
    status, location3, sid3, sbase3 = enter()
    check("a session was started", bool(sid3), location3)
    if sid3:
        # Wait for the program to have a window.
        deadline = time.time() + 60
        while time.time() < deadline:
            windows = subprocess.run(
                ["docker", "exec", f"portos-{sid3}", "wmctrl", "-l"],
                capture_output=True, text=True).stdout.strip()
            if windows:
                break
            time.sleep(2)

        # Close the program the way a visitor would: by closing its window.
        window = app_window(sid3)
        if window:
            close(sid3, window[0])

        deadline = time.time() + 60
        gone = False
        while time.time() < deadline:
            alive = subprocess.run(
                ["docker", "ps", "-q", "--filter", f"name=portos-{sid3}"],
                capture_output=True, text=True).stdout.strip()
            if not alive:
                gone = True
                break
            time.sleep(2)
        check("closing the program destroys the session", gone)

    print()
    if failures:
        print(f"\033[31m{len(failures)} check(s) failed:\033[0m")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("\033[32mall checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
