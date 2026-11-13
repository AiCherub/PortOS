#!/usr/bin/env python3
"""Per-listener PCM capture: s16le, 48000 Hz, stereo, via websockify.

No microphone or host sound device is opened. A stalled listener is disconnected
instead of accumulating an unbounded audio queue. Capture ends on disconnect.
"""
import asyncio
import contextlib
import struct

BLOCK_BYTES = 3840  # 20 ms, 960 stereo frames
SILENCE = bytes(BLOCK_BYTES)


async def stream(reader, writer):
    process = None
    disconnected = asyncio.create_task(reader.read(1))
    try:
        process = await asyncio.create_subprocess_exec(
            "parec", "--raw", "--format=s16le", "--rate=48000",
            "--channels=2", "--channel-map=front-left,front-right",
            "--device=portos.monitor", "--latency-msec=20",
            stdout=asyncio.subprocess.PIPE,
        )
        position = 0
        while not disconnected.done():
            read = asyncio.create_task(process.stdout.readexactly(BLOCK_BYTES))
            done, _ = await asyncio.wait(
                [read, disconnected], return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                read.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await read
                break
            data = read.result()
            if not data:
                break
            # Only exact digital silence is omitted; quiet music is preserved.
            if data != SILENCE:
                writer.write(struct.pack('<I', position) + data)
                await asyncio.wait_for(writer.drain(), timeout=1)
            position = (position + 960) & 0xffffffff
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        disconnected.cancel()
        with contextlib.suppress(asyncio.CancelledError, OSError):
            await disconnected
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def main():
    server = await asyncio.start_server(stream, "127.0.0.1", 5903)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
