#!/usr/bin/env bash
#
# Keep the showcased program's window sensible on the remote screen.
#
# FIT_MODE=natural  (default)
#     The window keeps the size the program asked for and is centred on the
#     screen. It is only resized when it is larger than the screen, in which
#     case it is shrunk to fit.
#
# FIT_MODE=fullscreen
#     The window is stretched to cover the whole screen, the way a kiosk does.
#
# The window is put back in the middle only when there is a reason to:
#   * the session just opened and the program's window showed up
#   * the remote resolution changed
#   * the program resized its own window while the session was still settling
# Once the visitor has moved the window, it stays where they put it - this
# loop must never drag it back to the middle on its own.
#
# The loop also publishes /tmp/app-ready as soon as the program has a window.
# The server uses that to know that a pre-warmed container is really ready to
# be handed to a visitor.
#
set -uo pipefail

export DISPLAY="${DISPLAY:-:1}"
FIT_MODE="${FIT_MODE:-natural}"

# How long after the session opens (and after a resolution change) the
# program's own resizes are still allowed to move the window. Windows
# programs like Notepad draw their window in a couple of steps, and the
# window would end up off-centre if we only ever centred it once.
SETTLE_OPEN="${SETTLE_OPEN:-10}"
SETTLE_RES="${SETTLE_RES:-3}"

# When the session opened. The program's own window comes up right after
# it, and that window is put in the middle of the screen. A window that
# shows up later is one the visitor opened, and that one is left exactly
# where it is.
SESSION_START="$(date +%s)"

screen_size() {
    xrandr --query 2>/dev/null | grep -o 'current [0-9]* x [0-9]*' | head -1 \
        | awk '{print $2, $4}'
}

# How far the window manager's frame sticks out around the client window, on
# the left and on the top.
#
# wmctrl -l -G prints the client's position with the frame added on top of
# it, so this is what tells us where the window really is.
frame_extents() {
    xprop -id "$1" -notype _NET_FRAME_EXTENTS 2>/dev/null \
        | sed -n 's/^_NET_FRAME_EXTENTS = *\([0-9]*\), *\([0-9]*\), *\([0-9]*\), *\([0-9]*\).*/\1 \3/p'
}

window_has_managed_size() {
    grep -Eq '_NET_WM_STATE_(MAXIMIZED_(HORZ|VERT)|FULLSCREEN)(,|$)' <<< "$1"
}

redraw_maximized_wine_window() {
    local wid="$1" properties="$2" pid
    [[ "$properties" == *"_NET_WM_STATE_MAXIMIZED_VERT"* &&
       "$properties" == *"_NET_WM_STATE_MAXIMIZED_HORZ"* &&
       "$properties" != *"_NET_WM_STATE_HIDDEN"* ]] || return
    pid="$(awk '/^_NET_WM_PID = [0-9]+$/ { print $3 }' <<< "$properties")"
    case "$(readlink "/proc/$pid/exe" 2>/dev/null)" in
        /usr/lib/wine/*/wine|/usr/lib/wine/*/wine-preloader)
            redraw-window "$wid" >/dev/null 2>&1
            ;;
    esac
}

# What the screen looked like the last time round.
LAST_SCREEN_W=""
LAST_SCREEN_H=""
# The window we were watching, and the size it had then.
LAST_WID=""
LAST_CW=""
LAST_CH=""
# Where we asked the window to go last time, and for which window.
LAST_FX=""
LAST_FY=""
LAST_FWID=""
# How far the window manager puts the window from where we asked for it. It
# differs between window managers and between windows, so it is measured
# instead of guessed.
KX=""
KY=""
# Where the window is expected to be, and whether the offset still has to be
# measured (the first look after a move).
EXP_X=""
EXP_Y=""
NEED_MEASURE=0
# Set once the visitor moves the window themselves; from then on we only
# touch it when the resolution changes.
USER_MOVED=0
# Until when the program is still allowed to drag the window around itself.
SETTLE_UNTIL=0

while true; do
    read -r W H <<< "$(screen_size)"
    if [ -z "${W:-}" ] || [ "$W" = "0" ]; then
        sleep 1
        continue
    fi

    # Set when the screen is a different size than it was the last time
    # round. The program has to be made to redraw itself then - see below.
    screen_changed=0
    if [ "$W" != "$LAST_SCREEN_W" ] || [ "$H" != "$LAST_SCREEN_H" ]; then
        screen_changed=1
    fi

    # Largest visible client window: wmctrl -l -G prints
    #   <id> <desktop> <x> <y> <width> <height> <host> <title>
    line="$(wmctrl -l -G 2>/dev/null \
        | awk 'NF >= 8 && $5+0 > 0 && $6+0 > 0 { print $5*1024+$6, $1, $5, $6, $3, $4 }' \
        | sort -rn | head -1)"
    if [ -n "$line" ]; then
        wid="$(echo "$line" | awk '{print $2}')"
        cw="$(echo "$line" | awk '{print $3}')"
        ch="$(echo "$line" | awk '{print $4}')"
        cx="$(echo "$line" | awk '{print $5}')"
        cy="$(echo "$line" | awk '{print $6}')"

        if [ ! -f /tmp/app-ready ]; then
            : > /tmp/app-ready
        fi

        if [ "$FIT_MODE" = "fullscreen" ]; then
            if [ "$cw" -ne "$W" ] || [ "$ch" -ne "$H" ]; then
                # Note the -i: without it wmctrl matches windows by title.
                wmctrl -i -r "$wid" -b add,fullscreen >/dev/null 2>&1
                wmctrl -i -r "$wid" -e 0,0,0,"$W","$H" >/dev/null 2>&1
            fi
        elif window_has_managed_size "$(xprop -id "$wid" -notype _NET_WM_STATE 2>/dev/null)"; then
            # Openbox sizes and repositions these windows when the display
            # changes. Preserve its saved geometry for restoring the window.
            USER_MOVED=1
            NEED_MEASURE=0
        else
            now="$(date +%s)"

            read -r FE_L FE_T <<< "$(frame_extents "$wid")"
            FE_L="${FE_L:-0}"
            FE_T="${FE_T:-0}"

            # The program's own size, but never let it stick out of the screen.
            # The window manager's frame counts too: the title bar sits above
            # the program, and a title bar that hangs off the top of the
            # screen is a title bar the visitor cannot click - no moving, no
            # minimising, no closing.
            nw="$cw"; nh="$ch"
            [ "$nw" -gt $(( W - FE_L )) ] && nw=$(( W - FE_L ))
            [ "$nh" -gt $(( H - FE_T )) ] && nh=$(( H - FE_T ))
            [ "$nw" -lt 64 ] && nw=64
            [ "$nh" -lt 48 ] && nh=48

            too_big=0
            if [ "$cw" -ne "$nw" ] || [ "$ch" -ne "$nh" ]; then
                too_big=1
            fi

            recentre=0

            # Has the window moved away from where we left it? We only ever
            # move it when we asked for it, so a window that is not where it
            # should be was moved by the visitor - and from then on we leave
            # it be.
            if [ -n "$LAST_FX" ] && [ "$wid" = "$LAST_FWID" ]; then
                if [ "$NEED_MEASURE" = "1" ]; then
                    # The first look after our own move: this is where we
                    # learn how far the window manager puts the window from
                    # where we asked for it.
                    m_x=$(( cx - LAST_FX ))
                    m_y=$(( cy - LAST_FY ))
                    [ "$m_x" -lt 0 ] && m_x=$(( -m_x ))
                    [ "$m_y" -lt 0 ] && m_y=$(( -m_y ))

                    # A window manager puts the window a frame's worth of
                    # pixels away from the request, no further. Anything
                    # beyond that means the window was moved by hand.
                    if [ "$m_x" -le $(( 2 * FE_L + 8 )) ] \
                       && [ "$m_y" -le $(( 2 * FE_T + 8 )) ]; then
                        KX=$(( cx - LAST_FX ))
                        KY=$(( cy - LAST_FY ))
                        # Our first request was a guess; now that we know how
                        # the window manager moves things, do it properly.
                        recentre=1
                    else
                        USER_MOVED=1
                    fi
                    EXP_X="$cx"
                    EXP_Y="$cy"
                    NEED_MEASURE=0
                elif [ $(( cx - EXP_X )) -gt 3 ] || [ $(( EXP_X - cx )) -gt 3 ] \
                     || [ $(( cy - EXP_Y )) -gt 3 ] || [ $(( EXP_Y - cy )) -gt 3 ]; then
                    # The visitor moved the window. From now on it stays where
                    # they put it.
                    USER_MOVED=1
                    EXP_X="$cx"
                    EXP_Y="$cy"
                fi
            fi

            # Reasons to put the window back in the middle of the screen.
            if [ "$screen_changed" = "1" ]; then
                recentre=1                  # the resolution changed
                USER_MOVED=0               # a new screen is a fresh start
                SETTLE_UNTIL=$(( now + SETTLE_RES ))
            fi
            if [ "$wid" != "$LAST_WID" ]; then
                # A window showed up.
                #
                # While the session is still opening, that is the program's
                # own window coming up, and it goes to the middle of the
                # screen. A window that shows up later is one the visitor
                # opened, and it is left exactly where it is - the visitor
                # put it there, and moving it would take it away from under
                # their hands.
                KX=""
                KY=""
                NEED_MEASURE=0
                if [ $(( now - SESSION_START )) -lt "$SETTLE_OPEN" ]; then
                    recentre=1
                    USER_MOVED=0
                    SETTLE_UNTIL=$(( now + SETTLE_OPEN ))
                fi
            elif [ "$USER_MOVED" = "0" ] && [ "$now" -lt "$SETTLE_UNTIL" ] \
                 && { [ "$cw" != "$LAST_CW" ] || [ "$ch" != "$LAST_CH" ]; }; then
                recentre=1                  # the program resized the window itself
            fi

            KX="${KX:-0}"
            KY="${KY:-0}"
            if [ "$too_big" = "1" ]; then
                # Shrink it, but keep the corner the visitor chose - only pull
                # it back if it would end up off the screen.
                fx=$(( cx - KX ))
                fy=$(( cy - KY ))
                [ "$fx" -gt $(( W - nw )) ] && fx=$(( W - nw ))
                [ "$fy" -gt $(( H - nh )) ] && fy=$(( H - nh ))
                [ "$fx" -lt 0 ] && fx=0
                # The title bar is above the program, so the program may not
                # start higher up than the title bar is tall.
                [ "$fy" -lt "$FE_T" ] && fy="$FE_T"
            elif [ "$recentre" = "1" ]; then
                # Put the client - the part the visitor sees - in the middle.
                fx=$(( (W - nw) / 2 + FE_L - KX ))
                fy=$(( (H - nh) / 2 + FE_T - KY ))
            fi

            # Only touch the window when there is really something to change.
            if [ "$too_big" = "1" ] || [ "$recentre" = "1" ]; then
                if [ "$fx" != "$LAST_FX" ] || [ "$fy" != "$LAST_FY" ] \
                   || [ "$wid" != "$LAST_FWID" ] || [ "$nw" != "$cw" ] \
                   || [ "$nh" != "$ch" ]; then
                    wmctrl -i -r "$wid" -e 0,"$fx","$fy","$nw","$nh" >/dev/null 2>&1
                    LAST_FX="$fx"
                    LAST_FY="$fy"
                    LAST_FWID="$wid"
                    EXP_X=$(( fx + KX ))
                    EXP_Y=$(( fy + KY ))
                    NEED_MEASURE=1
                fi
            fi
        fi

        # Resizing the screen can leave Wine drawing with its old bounds.
        # Refresh maximized Wine windows without changing their X geometry.
        if [ "$FIT_MODE" != "fullscreen" ] && [ "$screen_changed" = "1" ]; then
            # Give Wine time to pick up the new monitor size before repainting.
            sleep 0.15
            for entry in $(wmctrl -l -G 2>/dev/null \
                | awk 'NF >= 8 && $5+0 > 0 && $6+0 > 0 { print $1 ":" $5 ":" $6 }'); do
                w_id="${entry%%:*}"
                properties="$(xprop -id "$w_id" -notype _NET_WM_STATE _NET_WM_PID 2>/dev/null)"
                if window_has_managed_size "$properties"; then
                    if [ -n "$LAST_SCREEN_W" ]; then
                        redraw_maximized_wine_window "$w_id" "$properties"
                    fi
                    continue
                fi
                w_rest="${entry#*:}"
                w_w="${w_rest%%:*}"
                w_h="${w_rest##*:}"
                xdotool windowsize "$w_id" "$(( w_w + 1 ))" "$w_h" >/dev/null 2>&1
                xdotool windowsize "$w_id" "$w_w" "$w_h" >/dev/null 2>&1
            done
        fi

        LAST_WID="$wid"
        LAST_CW="$cw"
        LAST_CH="$ch"
        LAST_SCREEN_W="$W"
        LAST_SCREEN_H="$H"
    fi
    sleep 1.5
done
