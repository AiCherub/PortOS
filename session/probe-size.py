#!/usr/bin/env python3
"""
Report the size of the remote framebuffer as a VNC client sees it.

This connects to the session's own VNC port and reads the desktop size out of
the server's init message, which is the only way to prove that a resolution
change really reached the server rather than just being requested.

    python3 probe-size.py [port]

Prints "WIDTH HEIGHT" and exits 0.
"""

import socket
import struct
import sys


def recv_exact(sock, count):
    buffer = b""
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise RuntimeError("the connection closed early")
        buffer += chunk
    return buffer


def probe(port=5901, host="127.0.0.1"):
    sock = socket.create_connection((host, port), timeout=10)
    try:
        recv_exact(sock, 12)                 # server protocol version
        sock.sendall(b"RFB 003.008\n")
        count = recv_exact(sock, 1)[0]
        if count == 0:
            raise RuntimeError("the server refused the connection")
        recv_exact(sock, count)              # security types
        sock.sendall(bytes([1]))             # security type: None
        result = struct.unpack(">I", recv_exact(sock, 4))[0]
        if result != 0:
            raise RuntimeError(f"the server refused the connection ({result})")
        sock.sendall(bytes([1]))             # ClientInit, shared
        width, height = struct.unpack(">HH", recv_exact(sock, 4))
        recv_exact(sock, 16)                 # pixel format
        name_length = struct.unpack(">I", recv_exact(sock, 4))[0]
        recv_exact(sock, name_length)        # desktop name
        return width, height
    finally:
        sock.close()


if __name__ == "__main__":
    try:
        w, h = probe(int(sys.argv[1]) if len(sys.argv) > 1 else 5901)
    except Exception as exc:                 # noqa: BLE001
        print(f"ERROR {exc}")
        sys.exit(1)
    print(f"{w} {h}")
