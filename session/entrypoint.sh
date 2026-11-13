#!/usr/bin/env bash
#
# PortOS session entrypoint.
#
# Brings up, in order:
#   1. Xvnc      - one process serving both X11 and VNC, sized large so that it
#                  can be shrunk to any browser window size at runtime
#   2. openbox   - window manager, so the program behaves like a real window
#   3. websockify x2 - one for the noVNC web client + RFB stream, one for the
#                  control channel (resolution / heartbeat)
#   4. the control channel, started before the program so that it is listening
#                  as early as possible
#   5. the showcased Windows program under Wine
#
# The container is disposable: when the control channel decides the visitor is
# gone, control.py exits and the whole session is torn down.
#
set -uo pipefail

SESSION_ID="${SESSION_ID:-unknown}"
APP_CMD="${APP_CMD:-wine}"
APP_ARGS="${APP_ARGS:-[]}"
INIT_W="${INIT_W:-1280}"
INIT_H="${INIT_H:-720}"
MAX_W="${MAX_W:-2560}"
MAX_H="${MAX_H:-1440}"
RESET_GRACE="${RESET_GRACE:-120}"     # seconds without a heartbeat -> end session
BOOT_GRACE="${BOOT_GRACE:-180}"       # seconds to wait for the first visitor
WM="${WM:-openbox}"
FIT_MODE="${FIT_MODE:-natural}"
export AUDIO_ENABLED="${AUDIO_ENABLED:-true}"
case "$AUDIO_ENABLED" in
    true|false) ;;
    *) echo 'AUDIO_ENABLED must be "true" or "false"' >&2; exit 1 ;;
esac
export SESSION_CLIPBOARD="${SESSION_CLIPBOARD-both}"
case "$SESSION_CLIPBOARD" in
    both|out|in|disabled) ;;
    *) echo 'SESSION_CLIPBOARD must be "both", "out", "in", or "disabled"' >&2; exit 1 ;;
esac

VNC_PORT=5901
NOVNC_PORT=6080
CTL_WS_PORT=6081
CTL_TCP_PORT=5902

export DISPLAY=:1
export WINEPREFIX=/tmp/wineprefix
export WINEDEBUG=-all
export HOME=/tmp/home
export XDG_RUNTIME_DIR=/tmp/runtime
mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

log() { echo "[session ${SESSION_ID}] $*"; }

cleanup() {
    log "shutting down"
    [ -n "${AUDIO_PID:-}" ] && kill "$AUDIO_PID" 2>/dev/null
    [ -n "${PULSE_PID:-}" ] && kill "$PULSE_PID" 2>/dev/null
    [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null
    [ -n "${PICOM_PID:-}" ] && kill "$PICOM_PID" 2>/dev/null
    [ -n "${WM_PID:-}" ] && kill "$WM_PID" 2>/dev/null
    [ -n "${XVNC_PID:-}" ] && kill "$XVNC_PID" 2>/dev/null
    exit 0
}
trap cleanup TERM INT

# A private virtual sound card, available before any Wine process starts.
if [ "$AUDIO_ENABLED" = "true" ]; then
export PULSE_SERVER=unix:/tmp/runtime/pulse/native
export PULSE_SINK=portos
mkdir -p /tmp/runtime/pulse
pulseaudio --daemonize=no --exit-idle-time=-1 --use-pid-file=no \
    --disallow-exit --disable-shm=yes -n \
    --file=/opt/portos/session/pulse.pa >/tmp/pulse.log 2>&1 &
PULSE_PID=$!
for _ in $(seq 1 50); do
    pactl info >/dev/null 2>&1 && break
    sleep 0.1
done
if ! pactl info >/dev/null 2>&1; then
    log "FATAL: virtual sound card did not start"
    cat /tmp/pulse.log
    exit 1
fi
python3 /opt/portos/session/audio.py >/tmp/audio.log 2>&1 &
AUDIO_PID=$!
else
    # An explicit unavailable server prevents libpulse from auto-spawning one.
    export PULSE_SERVER=unix:/tmp/runtime/pulse/disabled
    log "audio support disabled"
fi

# --------------------------------------------------------------- 1. X server
#
# -nocursor keeps the X server from drawing the pointer into the framebuffer.
# TigerVNC has no cursor pseudo-encoding, so a cursor in the picture is the
# only cursor there is - and it can only be drawn where the pointer *was* when
# the picture was put together, which is a round trip behind the visitor's
# mouse. Without it the picture carries no pointer at all, and the browser
# shows the pointer of the visitor's own machine instead. The pointer itself
# keeps working: apps still get the position, the clicks and the shape.
log "starting Xvnc on :${VNC_PORT} (max ${MAX_W}x${MAX_H})"
Xvnc :1 -geometry "${MAX_W}x${MAX_H}" -depth 24 -localhost \
      -SecurityTypes None -AlwaysShared -AcceptCutText=0 -SendCutText=0 \
      -AcceptKeyEvents -AcceptPointerEvents -nocursor \
      -desktop "PortOS" >/tmp/xvnc.log 2>&1 &
XVNC_PID=$!

for _ in $(seq 1 80); do
    if xdpyinfo >/dev/null 2>&1; then break; fi
    kill -0 "$XVNC_PID" 2>/dev/null || break
    sleep 0.25
done
if ! xdpyinfo >/dev/null 2>&1; then
    log "FATAL: the X server did not come up"
    cat /tmp/xvnc.log 2>/dev/null
    exit 1
fi
log "X server ready"

# ------------------------------------------------- 2. initial screen geometry
READ_W="$INIT_W"; READ_H="$INIT_H"
if OUT="$(/opt/portos/session/set-resolution.sh "$INIT_W" "$INIT_H" initial)"; then
    READ_W="$(echo "$OUT" | awk '{print $1}')"
    READ_H="$(echo "$OUT" | awk '{print $2}')"
    log "initial resolution ${READ_W}x${READ_H}"
else
    log "WARNING: could not set the initial resolution to ${INIT_W}x${INIT_H}"
fi

# ------------------------------------------------------- 3. window manager
# The configuration is patched first: a session has no desktop and no task
# bar, so a window that is minimised is a window that is gone for good. The
# minimise button comes off the title bar and the client menu loses its
# "Iconify" entry.
if [ "$WM" = "matchbox" ]; then
    log "starting window manager: matchbox"
    matchbox-window-manager -use_titlebar no >/tmp/wm.log 2>&1 &
else
    if python3 /opt/portos/session/openbox-config.py >/tmp/openbox-config.log 2>&1; then
        log "starting window manager: openbox (no minimise button)"
        openbox --config-file /tmp/openbox-rc.xml >/tmp/wm.log 2>&1 &
    else
        log "WARNING: could not patch the openbox configuration; starting it as it is"
        cat /tmp/openbox-config.log 2>/dev/null
        openbox >/tmp/wm.log 2>&1 &
    fi
fi
WM_PID=$!
sleep 1

# ------------------------------------------------- 3b. the compositor
#
# The X server keeps no copy of a window that is covered by another one.
# When a window is moved away, the area it covered is filled with the
# root window's black background, and the program underneath has to
# redraw itself. Wine's Explorer does not - it leaves the area black
# until it happens to refresh, so the screen keeps a black trail behind
# every window that is dragged away.
#
# A compositor does keep a copy of every window, so the area is put back
# from that copy instead of being left black.
#
# It has to start before the program does: a window that already exists
# when the compositor starts is not composited properly (its content goes
# blank). Matchbox is left alone - it is a fullscreen window manager with
# no decorations, and there is nothing behind the program to trail.
if [ "$WM" = "openbox" ]; then
    log "starting the compositor: picom"
    picom --backend xrender --no-fading-openclose >/tmp/picom.log 2>&1 &
    PICOM_PID=$!
    sleep 1
    if ! kill -0 "$PICOM_PID" 2>/dev/null; then
        log "WARNING: the compositor did not start; windows will leave a black trail"
        cat /tmp/picom.log 2>/dev/null
    fi
fi

# ----------------------------------------------------------- 4. wine prefix
# The prefix is pre-baked into the image (~1.3GB with 32-bit support), which is
# far too big to copy into a tmpfs per session. Instead the session uses the
# template directory directly: writes land in the container's own writable
# layer, which is discarded together with the container. Every session
# therefore still starts from a pristine prefix.
if [ -d /opt/wineprefix-template ]; then
    export WINEPREFIX=/opt/wineprefix-template
    log "using the pre-baked wine prefix"
else
    export WINEPREFIX=/tmp/wineprefix
    log "no wine prefix template found; wine will initialise on demand"
fi

# The program is drawn at the screen DPI, and Wine takes that from
# "HKCU\Control Panel\Desktop\LogPixels" (96 unless it says otherwise).
# The pre-baked prefix has the value - the image build puts it there - so
# this is only about saying it out loud and about a prefix that was created
# on the spot, which has none.
prefix_dpi() {
    awk '
        /^\[Control Panel.*Desktop\]/ { inside = 1; next }
        /^\[/ { inside = 0 }
        inside && /^"LogPixels"=dword:/ { sub(/^"LogPixels"=dword:/, ""); print; exit }
    ' "$1/user.reg" 2>/dev/null
}
DPI_HEX="$(prefix_dpi "$WINEPREFIX")"
if [ -n "$DPI_HEX" ]; then
    log "display DPI $((16#${DPI_HEX,,}))"
else
    log "setting the display DPI to ${WINE_DPI:-110}"
    wine reg add "HKCU\Control Panel\Desktop" /v LogPixels /t REG_DWORD \
        /d "${WINE_DPI:-110}" /f || true
fi

# ------------------------------------------------- 5. static files for noVNC
# The web root holds our viewer plus symlinks into the noVNC tree. A host
# mount at /opt/portos/web-host wins over the baked-in copy so that the
# viewer can be edited without rebuilding the image.
SERVE=/opt/portos/serve
mkdir -p "$SERVE"
if [ -n "$(ls -A /opt/portos/web-host 2>/dev/null)" ]; then
    cp -a /opt/portos/web-host/. "$SERVE"/ 2>/dev/null
    log "viewer assets served from the host mount"
else
    cp -a /opt/portos/web-baked/. "$SERVE"/ 2>/dev/null
    log "viewer assets served from the image"
fi
ln -sfn /usr/share/novnc/core "$SERVE/core"
ln -sfn /usr/share/novnc/vendor "$SERVE/vendor"
ln -sfn /usr/share/novnc/app "$SERVE/app"
printf 'export const audioEnabled = %s;\nexport const clipboardMode = "%s";\n' \
    "$AUDIO_ENABLED" "$SESSION_CLIPBOARD" > "$SERVE/js/session-config.js"

# ------------------------------------------------------- 7. websockify bridge
# One port serves everything the browser needs:
#   static files      /            (the viewer and noVNC's modules)
#   the picture       /websockify?token=vnc
#   the control line  /ctl?token=ctl
# The token plugin is what lets one websockify forward to two different
# local services.
cat > /tmp/tokens <<EOF
vnc: 127.0.0.1:${VNC_PORT}
ctl: 127.0.0.1:${CTL_TCP_PORT}
EOF
if [ "$AUDIO_ENABLED" = "true" ]; then
    echo 'audio: 127.0.0.1:5903' >> /tmp/tokens
fi

websockify --heartbeat 30 --web "$SERVE" \
    --token-plugin TokenFile --token-source /tmp/tokens \
    "${NOVNC_PORT}" >/tmp/websockify.log 2>&1 &
log "web + RFB + control on ${NOVNC_PORT}"

# ------------------------------------------------------------ 8. control loop
# control.py is started before the program so that the control channel is
# listening as early as possible: the browser opens it immediately, and a
# refused connection there would look like a broken session.
#
# It runs in the background and the entrypoint waits for it, so the container
# still stops the moment control.py decides the visitor is gone.
export SESSION_ID RESET_GRACE BOOT_GRACE INIT_W INIT_H MAX_W MAX_H
export CTL_TCP_PORT
python3 /opt/portos/session/control.py &
CONTROL_PID=$!

sleep 1

# ------------------------------------------------------------- 9. the program
APP_ARGV=()
while IFS= read -r arg; do
    APP_ARGV+=("$arg")
done < <(python3 -c 'import json,sys
for a in json.loads(sys.argv[1]):
    print(a)' "$APP_ARGS" 2>/dev/null)

for i in "${!APP_ARGV[@]}"; do
    APP_ARGV[$i]="${APP_ARGV[$i]//\{W\}/$READ_W}"
    APP_ARGV[$i]="${APP_ARGV[$i]//\{H\}/$READ_H}"
done

log "launching: ${APP_CMD} ${APP_ARGV[*]}"
cd /opt/apps 2>/dev/null || cd /tmp
setsid "$APP_CMD" "${APP_ARGV[@]}" >/tmp/app.log 2>&1 &
APP_PID=$!
echo "$APP_PID" > /tmp/app.pid
echo "${APP_CMD} ${APP_ARGV[*]}" > /tmp/app.cmd

# Keep the program's window sensible on the remote screen.
FIT_MODE="$FIT_MODE" setsid /opt/portos/session/fit-window.sh >/tmp/fit.log 2>&1 &

# Wait for the session to end. That happens when either
#   * the visitor leaves (the control channel decides so), or
#   * the program itself is closed - there is nothing left to show then.
GRACE=5
while kill -0 "$CONTROL_PID" 2>/dev/null; do
    if [ "$GRACE" -gt 0 ]; then
        GRACE=$(( GRACE - 1 ))
    elif ! kill -0 "$APP_PID" 2>/dev/null; then
        log "the program was closed; ending the session"
        # Tell the control channel, so the visitor learns why the session is
        # over before the container goes away. It would otherwise only see
        # the picture disappear and wonder what happened.
        python3 /opt/portos/session/notify.py program || true
        sleep 1
        break
    fi
    sleep 1
done

kill "$CONTROL_PID" 2>/dev/null
wait "$CONTROL_PID" 2>/dev/null
exit 0
