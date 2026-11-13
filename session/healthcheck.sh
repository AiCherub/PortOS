#!/usr/bin/env bash
#
# Readiness probe for a session container.
#
#   1. the control channel is up (it writes /tmp/control-ready)
#   2. the noVNC web server is answering
#   3. when REQUIRE_WINDOW=1, the program has a window on screen
#
# The third condition is what makes a pre-warmed container genuinely ready to
# hand to a visitor: the program is already running and visible.
#
# Each failure says what went wrong: docker keeps this output in the
# container's health log, which makes "did not become ready" diagnosable.
#
set -u

if [ "${AUDIO_ENABLED:-true}" = "true" ] && {
   ! PULSE_SERVER=unix:/tmp/runtime/pulse/native pactl info >/dev/null 2>&1 || \
   ! python3 -c "import socket; socket.create_connection(('127.0.0.1', 5903), 2).close()" 2>/dev/null;
}; then
    echo "the audio service is not ready"
    exit 1
fi

if [ ! -f /tmp/control-ready ]; then
    echo "the control channel is not up yet"
    exit 1
fi

if ! python3 -c "import socket; socket.create_connection(('127.0.0.1', 6080), 2).close()" 2>/dev/null; then
    echo "the web server on 6080 is not answering"
    exit 1
fi

if [ "${REQUIRE_WINDOW:-0}" = "1" ] && [ ! -f /tmp/app-ready ]; then
    echo "the program has no window yet"
    exit 1
fi

exit 0
