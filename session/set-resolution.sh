#!/usr/bin/env bash
# Set the remote screen to an exact pixel size while keeping VNC-0 active.
# Resizing the framebuffer alone can detach the output. VNC still shows a
# picture in that state, but Wine loses its monitor and uses a fallback size.
#
# Print the real framebuffer size so the caller can report it to the browser.
set -uo pipefail

export DISPLAY="${DISPLAY:-:1}"
export LC_ALL=C

TARGET_W="${1:-0}"
TARGET_H="${2:-0}"
OUTPUT=VNC-0

if ! [[ "$TARGET_W" =~ ^[0-9]{1,5}$ && "$TARGET_H" =~ ^[0-9]{1,5}$ ]]; then
    echo "0 0"; exit 1
fi

TARGET_W=$((10#$TARGET_W))
TARGET_H=$((10#$TARGET_H))
SIZE="${TARGET_W}x${TARGET_H}"

framebuffer_size() {
    awk '/^Screen / { sub(/.*current /, ""); print $1, $3+0; exit }' <<< "$1"
}

output_matches() {
    [[ "$(framebuffer_size "$1")" == "$TARGET_W $TARGET_H" ]] &&
        awk -v output="$OUTPUT" -v geometry="$SIZE+0+0" '
            $1 == output && $2 == "connected" && ($3 == geometry || $4 == geometry) { found=1 }
            END { exit !found }
        ' <<< "$1"
}

add_mode() {
    # Xvnc has no physical display timings. Use short blanking intervals
    # at 60 Hz so odd browser dimensions are kept to the exact pixel.
    local htotal=$((TARGET_W + 3)) vtotal=$((TARGET_H + 3)) clock
    clock="$(awk -v w="$htotal" -v h="$vtotal" 'BEGIN { printf "%.3f", w * h * 60 / 1000000 }')"
    xrandr --newmode "$1" "$clock" \
        "$TARGET_W" "$((TARGET_W + 1))" "$((TARGET_W + 2))" "$htotal" \
        "$TARGET_H" "$((TARGET_H + 1))" "$((TARGET_H + 2))" "$vtotal" &&
        xrandr --addmode "$OUTPUT" "$1"
}

remove_mode() {
    xrandr --delmode "$OUTPUT" "$1" >/dev/null 2>&1
    xrandr --rmmode "$1" >/dev/null 2>&1
}

draw_wallpaper() {
    local mode="${SESSION_WALLPAPER_MODE:-center}"
    local wallpaper=/opt/portos/session-wallpaper option
    case "$mode" in
        fit) option=--maximize ;;
        fill) option=--zoom ;;
        stretch|tile) option="--$mode" ;;
        *) option=--center ;;
    esac
    if [[ ! -f "$wallpaper" ]] || ! xwallpaper --clear "$option" "$wallpaper"; then
        xwallpaper --clear "$option" /opt/portos/session/PortOS-Wallpaper.png
    fi
}

# Different control connections can request a resize at the same time.
exec 9>/tmp/portos-resolution.lock
flock -x 9 || { echo "0 0"; exit 1; }
state="$(xrandr --query 2>/dev/null)"
previous="$(framebuffer_size "$state")"
if (( TARGET_W >= 32 && TARGET_H >= 32 && TARGET_W <= 32768 && TARGET_H <= 32768 )) &&
   ! output_matches "$state"; then
    mode="$(awk -v size="$SIZE" '$1 == size || $1 == "portos-" size { print $1; exit }' <<< "$state")"
    if [ -z "$mode" ]; then
        mode="portos-$SIZE"
        add_mode "$mode" >&2 || { remove_mode "$mode"; mode=""; }
    fi
    if [ -n "$mode" ]; then
        xrandr --output "$OUTPUT" --mode "$mode" --pos 0x0 >&2
    fi
    state="$(xrandr --query 2>/dev/null)"

    # Keep only the active custom mode as the browser changes size.
    while read -r unused_mode; do
        remove_mode "$unused_mode"
    done < <(awk '$1 ~ /^portos-[0-9]+x[0-9]+$/ && !/\*/ { print $1 }' <<< "$state")
fi

actual="$(framebuffer_size "$state")"
if [[ -n "$actual" && ( "$actual" != "$previous" || "${3:-}" == initial ) ]]; then
    draw_wallpaper >&2
fi
echo "${actual:-0 0}"
output_matches "$state"
