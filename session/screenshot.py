#!/usr/bin/env python3
"""
Grab the remote framebuffer and write it out as a PNG.

Connects to the session's VNC port as a client, asks for a raw pixel dump and
converts it to PNG. Used to check what the browser would actually show, and to
produce thumbnails for the lobby.

    python3 screenshot.py [output.png] [port]
"""

import struct
import sys
import zlib


def recv_exact(sock, count):
    buffer = b""
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise RuntimeError("the connection closed early")
        buffer += chunk
    return buffer


class PixelFormat:
    def __init__(self, data):
        (self.bpp, self.depth, self.big_endian, self.true_colour,
         self.red_max, self.green_max, self.blue_max,
         self.red_shift, self.green_shift, self.blue_shift) = struct.unpack(">BBBBHHHBBB", data[:13])
        self.bytes_per_pixel = self.bpp // 8

    def to_rgb(self, pixel):
        if self.big_endian:
            value = int.from_bytes(pixel, "big")
        else:
            value = int.from_bytes(pixel, "little")
        red = (value >> self.red_shift) & self.red_max
        green = (value >> self.green_shift) & self.green_max
        blue = (value >> self.blue_shift) & self.blue_max
        return red, green, blue


def write_png(path, width, height, rows):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))
    with open(path, "wb") as handle:
        handle.write(data)


def grab(port=5901, host="127.0.0.1"):
    import socket
    sock = socket.create_connection((host, port), timeout=20)
    try:
        recv_exact(sock, 12)
        sock.sendall(b"RFB 003.008\n")
        count = recv_exact(sock, 1)[0]
        recv_exact(sock, count)
        sock.sendall(bytes([1]))                    # security type: None
        if struct.unpack(">I", recv_exact(sock, 4))[0] != 0:
            raise RuntimeError("the server refused the connection")
        sock.sendall(bytes([1]))                    # ClientInit, shared

        width, height = struct.unpack(">HH", recv_exact(sock, 4))
        fmt = PixelFormat(recv_exact(sock, 16))
        name_length = struct.unpack(">I", recv_exact(sock, 4))[0]
        recv_exact(sock, name_length)

        # Ask for raw pixels only, so the decoding stays simple.
        sock.sendall(struct.pack(">BBH", 2, 0, 1) + struct.pack(">i", 0))
        sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))

        pixels = bytearray(width * height * 3)
        area = 0
        sock.settimeout(20)
        while area < width * height:
            header = recv_exact(sock, 4)
            if header[0] != 0:
                continue
            rectangles = struct.unpack(">H", header[2:4])[0]
            for _ in range(rectangles):
                x, y, w, h, encoding = struct.unpack(">HHHHi", recv_exact(sock, 12))
                if encoding == -223:                 # DesktopSize
                    new_w, new_h = struct.unpack(">HH", recv_exact(sock, 4))
                    if (new_w, new_h) != (width, height):
                        width, height = new_w, new_h
                        pixels = bytearray(width * height * 3)
                        area = 0
                    continue
                if encoding != 0:                    # only Raw is supported
                    raise RuntimeError(f"unsupported encoding {encoding}")
                data = recv_exact(sock, w * h * fmt.bytes_per_pixel)
                for row in range(h):
                    base = (y + row) * width + x
                    offset = row * w * fmt.bytes_per_pixel
                    for column in range(w):
                        start = offset + column * fmt.bytes_per_pixel
                        red, green, blue = fmt.to_rgb(data[start:start + fmt.bytes_per_pixel])
                        target = (base + column) * 3
                        pixels[target] = red
                        pixels[target + 1] = green
                        pixels[target + 2] = blue
                area += w * h
            # Ask for whatever is still missing.
            if area < width * height:
                sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))
        return width, height, pixels
    finally:
        sock.close()


def main():
    output = sys.argv[1] if len(sys.argv) > 1 else "/tmp/screenshot.png"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5901
    width, height, pixels = grab(port)
    rows = [bytes(pixels[y * width * 3:(y + 1) * width * 3]) for y in range(height)]
    write_png(output, width, height, rows)
    print(f"{output} {width}x{height}")


if __name__ == "__main__":
    main()
