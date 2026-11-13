# syntax=docker/dockerfile:1
#
# PortOS session image — one disposable, per-visitor environment that runs a
# single Windows program under Wine and serves it to the browser over VNC.
#
# Design notes (validated empirically on this machine):
#   * Alpine instead of Ubuntu. This image is pulled once and started for
#     every visitor, so its size is the thing that matters most. Alpine's
#     Wine is one 64-bit loader that carries both the 32-bit and the 64-bit
#     Windows programs, where Ubuntu's wine + wine32:i386 pull in a second,
#     complete 32-bit Unix side.
#   * TigerVNC's Xvnc is used instead of Xvfb + x11vnc, because Xvfb's RandR
#     is pinned to its initial screen size ("maximum 1280 x 720") and therefore
#     cannot change resolution at runtime. Xvnc supports arbitrary live resize
#     via RandR output modes.
#   * Xvnc is a single process that is both the X server and the VNC server,
#     which keeps the image small and the startup fast.
#   * Wine 32-bit support: Alpine's Wine runs 32-bit Windows programs
#     through the new WoW64 mode, in a 64-bit prefix.
#
# ---------------------------------------------------------------------------
# Build wmctrl.
#
# wmctrl is not in Alpine, so it is built from source here. Only the binary
# ends up in the image; the compiler does not.
#
# It is the source that Debian and Ubuntu use as well: the 1.07 release
# miscounts the size of the window list on a 64-bit machine and lists no
# windows at all, and the session needs that list to place the program.
# ---------------------------------------------------------------------------
FROM alpine:3.23 AS wmctrl

RUN apk add --no-cache build-base pkgconf libx11-dev libxext-dev libxmu-dev glib-dev ca-certificates \
 && wget -qO /tmp/wmctrl.tar.xz \
      "https://deb.debian.org/debian/pool/main/w/wmctrl/wmctrl_1.07+git20240228.1105759.orig.tar.xz" \
 && echo "f92ba2547a3f1919ce441249867d8a695d859f4e104972800f974d7031a5be0a  /tmp/wmctrl.tar.xz" | sha256sum -c - \
 && tar -xf /tmp/wmctrl.tar.xz -C /tmp \
 && cd "/tmp/wmctrl-1.07+git20240228.1105759" \
 && ./configure --prefix=/usr \
 && make \
 && make install \
 && rm -rf /tmp/wmctrl*

# ---------------------------------------------------------------------------
# Build Openbox with a small compatibility fix for Wine's maximize hints.
# Keep Alpine's Openbox package for the matching libraries and configuration.
# ---------------------------------------------------------------------------
FROM wmctrl AS x11-build

RUN apk add --no-cache gettext-dev imlib2-dev librsvg-dev \
      libxml2-dev pango-dev libxcursor-dev libxinerama-dev libxrandr-dev \
      startup-notification-dev libsm-dev

FROM x11-build AS openbox
ADD --checksum=sha256:8b4ac0760018c77c0044fab06a4f0c510ba87eae934d9983b10878483bde7ef7 https://distfiles.alpinelinux.org/distfiles/v3.23/openbox-3.6.1.tar.gz /tmp/openbox.tar.gz
# Retain the crash fix from Alpine 3.23's Openbox package.
ADD --checksum=sha256:355a2ba6885d55534ae22939c0fd3a66994d449056886b95b6f2421afa7a1bbd https://raw.githubusercontent.com/alpinelinux/aports/3.23-stable/community/openbox/client_calc_segfault_fix.patch /tmp/openbox-layer.patch
COPY session/openbox-keep-maximized.patch /tmp/
RUN tar -xf /tmp/openbox.tar.gz -C /tmp \
 && cd /tmp/openbox-3.6.1 \
 && patch -p1 < /tmp/openbox-layer.patch \
 && patch -p1 < /tmp/openbox-keep-maximized.patch \
 && ./configure --prefix=/usr --sysconfdir=/etc --disable-static \
 && make -j"$(nproc)" openbox/openbox \
 && install -s -Dm755 openbox/.libs/openbox /out/openbox

COPY session/redraw-window.c /tmp/
RUN cc -O2 -Wall -Wextra -o /out/redraw-window /tmp/redraw-window.c -lX11

# ---------------------------------------------------------------------------
# Rebuild Wine 10.19's X11 driver with the upstream fix for Wine bug 59012.
# The driver must match the Wine version shipped by Alpine 3.23.
# ---------------------------------------------------------------------------
FROM x11-build AS wine-x11

RUN apk add --no-cache bison flex-dev libxcomposite-dev libxi-dev libxkbcommon-dev \
      libxrender-dev libxxf86vm-dev mesa-dev vulkan-loader-dev
ADD --checksum=sha256:7cec58323c6f2aaee7aca93517379cbbfef96e2c2c580c68ff85dd000cbbdd46 https://distfiles.alpinelinux.org/distfiles/v3.23/wine-10.19.tar.xz /tmp/wine.tar.xz
ADD --checksum=sha256:9649777d6a1b33630c09efe61f028536733744b3868d5efaf0e7a32f3605902d https://raw.githubusercontent.com/alpinelinux/aports/3.23-stable/community/wine/rpath.patch /tmp/wine-rpath.patch
COPY session/wine-keep-minimized.patch /tmp/
RUN set -eux; \
    tar -xf /tmp/wine.tar.xz -C /tmp; \
    cd /tmp/wine-10.19; \
    patch -p1 < /tmp/wine-rpath.patch; \
    patch -p1 < /tmp/wine-keep-minimized.patch; \
    arch="$(apk --print-arch)"; \
    case "$arch" in x86_64|aarch64) set -- --enable-win64 ;; x86) arch=i386; set -- ;; esac; \
    ./configure "$@" --prefix=/usr --libdir=/usr/lib --disable-tests --without-mingw --with-x --with-vulkan; \
    make -j"$(nproc)" dlls/winex11.drv/winex11.so; \
    install -s -Dm755 dlls/winex11.drv/winex11.so "/out/wine/${arch}-unix/winex11.so"

# ---------------------------------------------------------------------------
# Gecko and the pre-baked wine prefix.
#
# Building the prefix once at image build time turns a ~15s wineboot into a
# ~1s start per session, which is what makes the first frame appear quickly.
#
# The program is drawn at the screen DPI, and Wine reads that from
# "HKCU\Control Panel\Desktop\LogPixels" - 96 unless it says otherwise.
# 110 makes the program a bit bigger, which suits a program that is looked
# at through a browser window. It is baked into the prefix here, so every
# session starts with it; WINE_DPI changes it.
# ---------------------------------------------------------------------------
# Wine expects a hyphenated filename and both architectures for WoW64.
# Alpine's wine_gecko package supplies only the host architecture and uses
# an underscored filename. Fetch the official MSIs with Wine's pinned hashes.
FROM alpine:3.23 AS gecko
ADD --checksum=sha256:26cecc47706b091908f7f814bddb074c61beb8063318e9efc5a7f789857793d6 https://dl.winehq.org/wine/wine-gecko/2.47.4/wine-gecko-2.47.4-x86.msi /gecko/wine-gecko-2.47.4-x86.msi
ADD --checksum=sha256:e590b7d988a32d6aa4cf1d8aa3aa3d33766fdd4cf4c89c2dcc2095ecb28d066f https://dl.winehq.org/wine/wine-gecko/2.47.4/wine-gecko-2.47.4-x86_64.msi /gecko/wine-gecko-2.47.4-x86_64.msi

FROM alpine:3.23 AS prefix
COPY --from=gecko /gecko/ /usr/share/wine/gecko/

ARG WINE_DPI=110
ENV WINE_DPI=${WINE_DPI} \
    WINEPREFIX=/opt/wineprefix-template \
    WINEDEBUG=-all \
    WINEDLLOVERRIDES="mscoree=" \
    DISPLAY=:99

RUN apk add --no-cache wine xvfb
RUN set -eux; \
    mkdir -p "$WINEPREFIX"; \
    # A throwaway X server: Wine wants a display to settle the prefix.
    Xvfb :99 -screen 0 1024x768x24 >/tmp/xvfb.log 2>&1 & \
    xvfb_pid=$!; \
    sleep 3; \
    timeout 120 wineboot --init; \
    winecfg -v win10; \
    wine reg add "HKCU\Control Panel\Desktop" \
        /v LogPixels /t REG_DWORD /d "$WINE_DPI" /f; \
    # Settle the prefix here. Wine re-configures a prefix on its first start,
    # which costs about 25 seconds; doing that once at build time means every
    # session starts its program in about two seconds.
    wineboot -u; \
    # The Chinese interface of a Windows program is drawn with the font
    # "SimSun" - a font that only exists on Windows. Wine looks for it,
    # does not find it, and falls back to a Latin font, so every Chinese
    # character comes out as an empty box. The replacements below tell Wine
    # that "SimSun" means the Chinese font that is installed here (Noto
    # Sans CJK). Without them the program runs, but its menus are unreadable.
    wine reg add "HKCU\Software\Wine\Fonts\Replacements" \
        /v "SimSun" /t REG_SZ /d "Noto Sans CJK SC" /f; \
    wine reg add "HKCU\Software\Wine\Fonts\Replacements" \
        /v "NSimSun" /t REG_SZ /d "Noto Sans CJK SC" /f; \
    wine reg add "HKCU\Software\Wine\Fonts\Replacements" \
        /v "Microsoft YaHei" /t REG_SZ /d "Noto Sans CJK SC" /f; \
    wine reg add "HKCU\Software\Wine\Fonts\Replacements" \
        /v "Microsoft YaHei UI" /t REG_SZ /d "Noto Sans CJK SC" /f; \
    wine reg add "HKCU\Software\Wine\Fonts\Replacements" \
        /v "Malgun Gothic" /t REG_SZ /d "Noto Sans CJK KR" /f; \
    wine reg add "HKCU\Software\Wine\Fonts\Replacements" \
        /v "Segoe UI" /t REG_SZ /d "Liberation Sans" /f; \
    wine reg add "HKCU\Software\Wine\Fonts\Replacements" \
        /v "Segoe UI Symbol" /t REG_SZ /d "Noto Sans Symbols 2" /f; \
    wine reg add "HKCU\Software\Wine\Fonts\Replacements" \
        /v "Segoe UI Emoji" /t REG_SZ /d "Noto Color Emoji" /f; \
    # The wineserver writes the registry out when it exits, and every session
    # takes the DPI and the font replacements from the prefix file. Fail the
    # build if they did not stick.
    timeout 60 wineserver -w || true; \
    wineserver -k || true; \
    sleep 2; \
    grep -q "\"LogPixels\"=dword:$(printf '%08x' "$WINE_DPI")" \
        "$WINEPREFIX/user.reg"; \
    grep -q "\"SimSun\"=\"Noto Sans CJK SC\"" "$WINEPREFIX/user.reg"; \
    # The prefix carries a copy of every Wine DLL, and each of those copies
    # is the same file as the one in Wine's own library directory - about
    # 380MB of the same bytes twice. The prefix points at Wine's copy
    # instead of keeping its own: the prefix shrinks from 420MB to under
    # 100MB, and the image with it.
    cd "$WINEPREFIX/drive_c/windows/system32"; \
    for f in *; do \
        if [ -f "/usr/lib/wine/x86_64-windows/$f" ]; then \
            ln -sf "/usr/lib/wine/x86_64-windows/$f" "$f"; \
        fi; \
    done; \
    cd "$WINEPREFIX/drive_c/windows/syswow64"; \
    for f in *; do \
        if [ -f "/usr/lib/wine/i386-windows/$f" ]; then \
            ln -sf "/usr/lib/wine/i386-windows/$f" "$f"; \
        fi; \
    done; \
    # What the interrupted "wine.inf" install left behind: 25MB of temporary
    # files that no session will ever read.
    rm -f "$WINEPREFIX"/drive_c/windows/system32/*.tmp; \
    kill "$xvfb_pid" || true

# wineboot does not guarantee Gecko installation. Install both architectures
# explicitly so the first HTML control never needs an installer/download.
RUN set -eux; \
    export DISPLAY=:98; \
    Xvfb "$DISPLAY" -screen 0 1024x768x24 >/tmp/xvfb-gecko.log 2>&1 & \
    xvfb_pid=$!; \
    sleep 2; \
    for arch in x86 x86_64; do \
        timeout 120 wine msiexec /i "/usr/share/wine/gecko/wine-gecko-2.47.4-${arch}.msi" /qn; \
    done; \
    # Wine's HKCR lookup does not merge these HKCU class overrides as on
    # Windows. Write the effective class keys used by ShellExecute.
    for class in htmlfile http https; do \
        wine reg add "HKCR\\${class}\shell\open\command" \
            /ve /t REG_SZ /d '"C:\Program Files\Internet Explorer\iexplore.exe" "%1"' /f; \
    done; \
    timeout 60 wineserver -w; \
    test "$(find "$WINEPREFIX/drive_c/windows" -iname xul.dll | wc -l)" -eq 2; \
    kill "$xvfb_pid"; \
    wait "$xvfb_pid" || true

# Bake the palette and console font into each new session's prefix.
# For CP936, Wine uses the selected font's maximum glyph width for every cell.
# Droid Sans Mono keeps those cells compact and still permits CJK fallback.
COPY session/wine-breeze-dark.reg /tmp/wine-breeze-dark.reg
RUN DISPLAY= wine reg import /tmp/wine-breeze-dark.reg \
 && DISPLAY= wine reg add 'HKCU\Console' /v FaceName /t REG_SZ /d 'Droid Sans Mono' /f \
 && timeout 60 wineserver -w \
 && grep -Fxq '"FaceName"="Droid Sans Mono"' "$WINEPREFIX/user.reg" \
 && grep -Fxq '"ThemeActive"="0"' "$WINEPREFIX/user.reg" \
 && grep -Fxq '"Window"="35 38 41"' "$WINEPREFIX/user.reg" \
 && grep -Fxq '"WindowText"="219 220 222"' "$WINEPREFIX/user.reg"

# Assemble both Wine paths in one layer so the identical Gecko installers
# share an inode. Keep the Installer cache and the offline install sources.
FROM prefix AS runtime-prefix
RUN set -eu; \
    mkdir -p /out/opt /out/usr/share/wine/gecko; \
    cp -a "$WINEPREFIX" /out/opt/; \
    installer_dir="/out$WINEPREFIX/drive_c/windows/Installer"; \
    for source in /usr/share/wine/gecko/*.msi; do \
        for cached in "$installer_dir"/*.msi; do \
            if cmp -s "$source" "$cached"; then \
                ln "$cached" "/out$source"; \
                break; \
            fi; \
        done; \
        test -f "/out$source"; \
    done

# Extract just the console face from the same archive Alpine's font-droid uses.
FROM alpine:3.23 AS console-font
ADD --checksum=sha256:06d48cbaa60b4c99e590f8d35d259304baa742852e6077c50be49c523c316b10 https://distfiles.alpinelinux.org/distfiles/v3.23/android-fonts-4.2.2_r1.2.tar.gz /tmp/android-fonts.tar.gz
RUN mkdir /out \
 && tar -xzf /tmp/android-fonts.tar.gz -C /out DroidSansMono.ttf NOTICE

# ---------------------------------------------------------------------------
# The session image itself.
#
# Besides the obvious parts, two things here are there for a reason:
#
#   * picom, a compositor. The X server keeps no copy of a window that is
#     covered by another one, so when a window is moved away the area it
#     covered is filled with the root window's black background and the
#     program underneath has to redraw itself. Wine's Explorer does not
#     redraw itself, so a black trail follows every window that is dragged
#     away. A compositor keeps a copy of every window and puts it back.
#
#   * session/fonts.conf, the fontconfig settings. Wine does not decide
#     by itself how a letter is to be drawn - it asks fontconfig, and
#     fontconfig's answer is what ends up on the screen. The stock answer
#     is "no antialiasing", which makes the program's text come out with
#     hard edges.
# ---------------------------------------------------------------------------
FROM alpine:3.23

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:1 \
    WINEPREFIX=/tmp/wineprefix \
    WINEDEBUG=-all \
    # Suppress only Mono installation. MSHTML must remain enabled for
    # embedded HTML controls and iexplore; Gecko is installed offline below.
    WINEDLLOVERRIDES="mscoree=" \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    NO_AT_BRIDGE=1 \
    GDK_BACKEND=x11 \
    QT_QPA_PLATFORM=xcb \
    XDG_RUNTIME_DIR=/tmp/runtime

# ---------------------------------------------------------------- base packages
ARG ALPINE_MIRROR=https://dl-cdn.alpinelinux.org/alpine
RUN sed "s|https://dl-cdn.alpinelinux.org/alpine|${ALPINE_MIRROR%/}|" /etc/apk/repositories \
      > /tmp/portos-apk.repositories \
 && apk --repositories-file /tmp/portos-apk.repositories add --no-cache \
      wine \
      pulseaudio pulseaudio-utils \
      tigervnc \
      openbox \
      picom \
      xdotool xclip xrandr xprop xdpyinfo xwininfo \
      libxfixes \
      novnc websockify \
      python3 \
      bash \
      musl-locales tzdata ca-certificates \
      fontconfig font-liberation font-noto-cjk \
 && rm -f /tmp/portos-apk.repositories \
 # Wine reads the fonts of the system out of fontconfig's cache, and that
 # cache is empty until somebody builds it. Without this Wine sees no
 # fonts at all and every program comes up in its own built-in ones.
 && fc-cache -f

COPY --from=wmctrl /usr/bin/wmctrl /usr/bin/wmctrl
COPY --from=openbox /out/openbox /usr/bin/openbox
COPY --from=openbox /out/redraw-window /usr/bin/redraw-window
COPY --from=runtime-prefix /out/ /
COPY --from=console-font /out/DroidSansMono.ttf /usr/share/fonts/droid/DroidSansMono.ttf
COPY --from=console-font /out/NOTICE /usr/share/licenses/droid-sans-mono/NOTICE

# A file reader may always use the first face of a collection. Use standalone
# regional faces for the Windows filenames that should select SC or KR.
ADD --checksum=sha256:faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9 https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf /opt/portos/fonts/NotoSansSC-Regular.otf
ADD --checksum=sha256:69975a0ac8472717870aefeab0a4d52739308d90856b9955313b2ad5e0148d68 https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/SubsetOTF/KR/NotoSansKR-Regular.otf /opt/portos/fonts/NotoSansKR-Regular.otf
ADD --checksum=sha256:6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2 https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/LICENSE /opt/portos/fonts/OFL.txt

# Programs such as Maye Nano open Windows font files directly by name.
RUN set -eu; \
    apk add --no-cache font-noto-symbols font-noto-emoji; \
    cd /opt/wineprefix-template/drive_c/windows/Fonts; \
    link_font() { test -s "$1" && ln -s "$1" "$2"; }; \
    link_font /opt/portos/fonts/NotoSansSC-Regular.otf msyh.ttc; \
    link_font /opt/portos/fonts/NotoSansSC-Regular.otf msyh.ttf; \
    link_font /opt/portos/fonts/NotoSansKR-Regular.otf malgun.ttf; \
    link_font /usr/share/fonts/liberation/LiberationSans-Regular.ttf segoeui.ttf; \
    link_font /usr/share/fonts/noto/NotoSansSymbols2-Regular.ttf seguisym.ttf; \
    link_font /usr/share/fonts/noto/NotoColorEmoji.ttf seguiemj.ttf

# ------------------------------------------------------------------- app files
RUN apk add --no-cache xwallpaper
COPY session/*.sh session/*.py session/fonts.conf session/pulse.pa session/PortOS-Wallpaper.png /opt/portos/session/
COPY session/themes/Win10-Dark/ /usr/share/themes/Win10-Dark/
COPY web/ /opt/portos/web-baked/
# The image includes the apps directory at build time. Before each session
# starts, the server overlays the current program files from the host.
COPY apps/ /opt/apps/

RUN chmod +x /opt/portos/session/*.sh /opt/portos/session/*.py \
 && mkdir -p /opt/portos/serve /opt/portos/web-host /opt/apps /tmp/runtime \
 && chmod 700 /tmp/runtime \
 # How the fonts are drawn is decided by fontconfig, and Wine asks
 # fontconfig - not the registry - for the way a letter is to be drawn.
 # The stock answer is "no antialiasing", which makes the program's text
 # come out with hard edges. session/fonts.conf is the answer the FreeBSD
 # handbook recommends for Wine.
 && cp /opt/portos/session/fonts.conf /etc/fonts/local.conf \
 && fc-cache -f \
 && test "$(wine --version)" = wine-10.19

COPY --from=wine-x11 /out/wine/ /usr/lib/wine/
WORKDIR /opt/portos/session

# The server waits for this to report healthy before handing the URL to the
# browser, so it must reflect "the noVNC web server is answering and the
# control channel is up". The control channel is checked through its readiness
# marker rather than by connecting to it: a probe connection would look like a
# visitor arriving and leaving, and would reset the goodbye timer.
#
# For a pre-warmed container REQUIRE_WINDOW=1 additionally requires the
# program to have a window on screen, so the container is only handed to a
# visitor once the program is actually up.
HEALTHCHECK --interval=3s --timeout=5s --start-period=10s --retries=20 \
  CMD /opt/portos/session/healthcheck.sh

ENTRYPOINT ["/opt/portos/session/entrypoint.sh"]
