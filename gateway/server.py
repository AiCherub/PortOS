#!/usr/bin/env python3
"""
PortOS server.

Owns the life cycle of visitor sessions. One visitor == one disposable
container. The container is created when the visitor asks for a work, and it is
destroyed as soon as the visitor's browser goes away (the session container
notices and exits), which is what guarantees that the next visitor starts from
a completely clean environment.

HTTP API
    POST   /api/sessions          start a session for the configured program
    GET    /api/sessions/<sid>    status of one session
    DELETE /api/sessions/<sid>    destroy a session now
    GET    /healthz               liveness probe
"""

import configparser
import io
import json
import logging
import os
import re
import secrets
import select
import shlex
import socket
import tarfile
import threading
import time
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from string import Template

import docker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server")

DOCKER = docker.from_env()
SESSION_INTERNET = os.environ.get("SESSION_INTERNET", "true").strip().lower()
if SESSION_INTERNET not in ("true", "false"):
    raise ValueError('SESSION_INTERNET must be "true" or "false"')
NETWORK = os.environ.get("PORTOS_NETWORK", "portos")
if SESSION_INTERNET == "false":
    NETWORK += "-isolated"
IMAGE = os.environ.get("PORTOS_IMAGE", "portos/session:latest")
TIMEZONE = os.environ.get("TZ") or "Etc/UTC"
SESSION_AUDIO = os.environ.get("SESSION_AUDIO", "true").strip().lower()
if SESSION_AUDIO not in ("true", "false"):
    raise ValueError('SESSION_AUDIO must be "true" or "false"')
SESSION_CLIPBOARD = os.environ.get("SESSION_CLIPBOARD", "both").strip().lower()
if SESSION_CLIPBOARD not in ("both", "out", "in", "disabled"):
    raise ValueError('SESSION_CLIPBOARD must be "both", "out", "in", or "disabled"')
SESSION_WALLPAPER = os.environ.get("SESSION_WALLPAPER") or "PortOS-Wallpaper.png"
SESSION_WALLPAPER_MODE = (os.environ.get("SESSION_WALLPAPER_MODE") or "center").strip().lower()
if SESSION_WALLPAPER_MODE not in ("center", "fit", "fill", "stretch", "tile"):
    raise ValueError("SESSION_WALLPAPER_MODE must be center, fit, fill, stretch, or tile")
WALLPAPER_ENV = {
    "SESSION_WALLPAPER": SESSION_WALLPAPER,
    "SESSION_WALLPAPER_MODE": SESSION_WALLPAPER_MODE,
}
WALLPAPER_DIR = Path("/session")
APPS_DIR = Path(os.environ.get("PORTOS_APPS_DIR", "/apps"))
WEB_DIR = Path(os.environ.get("PORTOS_WEB_DIR", "/web"))
LAUNCH_FILE = APPS_DIR / "launch.ini"
PUBLIC_ASSETS = {
    "/branding/splash.css": "text/css; charset=utf-8",
    "/branding/PortOS-Icon.png": "image/png",
    "/branding/SourceSans3-Regular.woff2": "font/woff2",
    "/branding/OFL.txt": "text/plain; charset=utf-8",
    "/status/status.css": "text/css; charset=utf-8",
}

MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "4"))
SESSION_TTL = int(os.environ.get("SESSION_TTL", "3600"))       # hard cap, seconds
INIT_W = int(os.environ.get("INIT_W", "1280"))
INIT_H = int(os.environ.get("INIT_H", "720"))
MAX_W = int(os.environ.get("MAX_W", "2560"))
MAX_H = int(os.environ.get("MAX_H", "1440"))
RESET_GRACE = os.environ.get("RESET_GRACE", "90")
BOOT_GRACE = os.environ.get("BOOT_GRACE", "180")
BYE_GRACE = os.environ.get("BYE_GRACE", "15")
# Set PORTOS_TIMING=1 to log how long the proxy takes per request.
TIMING = os.environ.get("PORTOS_TIMING", "0").lower() not in ("0", "false", "no", "")

# ---------------------------------------------------------------- warm pool
# A "warm" container is fully started, with the program already running and
# visible, but nobody is connected to it yet. When a visitor arrives it is
# handed over straight away, so the first frame appears almost immediately.
# As soon as a visitor's session ends, the server replaces it with a fresh
# warm one.
WARM_POOL = int(os.environ.get("WARM_POOL", "1"))
WARM_MAX_AGE = int(os.environ.get("WARM_MAX_AGE", "1800"))      # recycle after this
WARM_INTERVAL = float(os.environ.get("WARM_INTERVAL", "3"))    # how often to top up
FIT_MODE = os.environ.get("FIT_MODE", "natural")                # natural | fullscreen
# A ceiling on all session containers at once, so a misbehaving pool can
# never take the machine down. 0 derives it from MAX_SESSIONS and WARM_POOL.
MAX_CONTAINERS = int(os.environ.get("MAX_CONTAINERS", "0"))

SESSION_LABEL = "portos.session"
CLAIMED_MARKER = "/tmp/claimed"
# Written by the session when the visitor says goodbye or stops sending
# heartbeats. A browser that navigates away can leave its socket open for a
# while, so the socket on its own does not say whether the session is still
# good - this marker does.
ENDING_MARKER = "/tmp/session-ending"
# A container that belongs to the pool carries this label. One that is
# started for a visitor straight away does not, and the pool must never
# treat it as a spare: its label is set when the container is created, so
# there is no window in which it could be mistaken for one.
WARM_LABEL = "portos.warm"
SESSION_COOKIE = "portos_sid"
COOKIE_MAX_AGE = int(os.environ.get("COOKIE_MAX_AGE", "86400"))
VISITOR_MARKER = "/tmp/visitor-connected"
# A container that was handed out but that nobody opened goes back into the
# pool instead of being thrown away. A crawler that only fetches "/" must not
# squat the single warm container.
REQUEUE_GRACE = float(os.environ.get("REQUEUE_GRACE", "120"))
SID_RE = re.compile(r"^[0-9a-f]{32}$")

_pool_lock = threading.RLock()
_warm_thread = None
_warm_spares = set()
# Keep each cause until its refill succeeds or the pool is full again.
_warm_refills = {}


# ------------------------------------------------------------------ liveness
#
# The server relays every WebSocket the browser opens, so it knows exactly
# when a visitor is present and when the browser has gone away. That is what
# tells a returning visitor apart from a stale cookie: a session that nobody
# is looking at any more is on its way out and must not be handed out again.
#
# The session container has a watchdog of its own, but it can only notice a
# missing heartbeat a minute later. The server sees the visitor leave the
# moment the socket closes, so it can clean up much sooner and no leftover
# container sits in the way of the next visitor.
STALE_GRACE = float(os.environ.get("STALE_GRACE", "15"))

# How long a container that was created but never started is left alone
# before the reaper throws it away. It has to be long enough to cover the
# whole setup (copy the work in, start it, wait for it to report healthy),
# because a container that is removed while it is being set up cannot be
# started afterwards.
SETUP_GRACE = float(os.environ.get("SETUP_GRACE", "300"))

_live = {}
_live_lock = threading.Lock()


def mark_live(sid, ip, user_agent):
    """A visitor's socket has arrived."""
    with _live_lock:
        entry = _live.setdefault(sid, {"visitors": {}, "left_at": None, "removed": False})
        visit = entry["visitors"].get(ip)
        if visit is None:
            visit = {"sockets": 0, "started_at": time.monotonic(), "user_agent": user_agent}
            entry["visitors"][ip] = visit
            log.info("visitor arrived session=%s ip=%s ua=%r", sid, ip, user_agent)
        visit["sockets"] += 1
        entry["left_at"] = None


def clear_live(sid, ip):
    """A visitor's socket has gone."""
    with _live_lock:
        entry = _live.get(sid)
        if entry is None or ip not in entry["visitors"]:
            return
        visit = entry["visitors"][ip]
        visit["sockets"] -= 1
        if visit["sockets"]:
            return

        now = time.monotonic()
        log.info("visitor left session=%s ip=%s duration_seconds=%.3f ua=%r",
                 sid, ip, now - visit["started_at"], visit["user_agent"])
        del entry["visitors"][ip]
        if not entry["visitors"]:
            if entry["removed"]:
                del _live[sid]
            else:
                entry["left_at"] = now


def forget_live(sid):
    """Retain open visits until their relays finish logging them."""
    with _live_lock:
        entry = _live.get(sid)
        if entry and entry["visitors"]:
            entry["removed"] = True
        else:
            _live.pop(sid, None)


def visitor_present(sid):
    """Is somebody looking at this session right now?"""
    with _live_lock:
        entry = _live.get(sid)
        return bool(entry and entry["visitors"])


def left_since(sid):
    """When the last visitor left, or None if there never was one."""
    with _live_lock:
        entry = _live.get(sid)
        return entry["left_at"] if entry else None


# --------------------------------------------------------------------- helpers

def load_launch():
    """Read launch.ini and build the Wine launch environment."""
    config = configparser.ConfigParser(interpolation=None)
    try:
        with LAUNCH_FILE.open(encoding="utf-8-sig") as source:
            config.read_file(source)
        if "Launch" not in config:
            raise ValueError("missing [Launch] section")
        launch = config["Launch"]
        program = launch.get("Program", "")
        if not re.fullmatch(r'[^/\\:"\x00\r\n]+\.exe', program, re.IGNORECASE):
            raise ValueError("Program must be an .exe filename")

        # Double quotes group arguments. Keep backslashes literal for Windows paths.
        lexer = shlex.shlex(launch.get("Arguments", ""), posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.quotes = '"'
        lexer.escape = ""
        try:
            args = list(lexer)
        except ValueError as exc:
            raise ValueError(f"invalid Arguments: {exc}") from exc
        if any(re.search(r"[\x00\r\n]", arg) for arg in args):
            raise ValueError("Arguments cannot contain NUL or line breaks inside a value")
        fullscreen = launch.getboolean("Fullscreen", fallback=FIT_MODE == "fullscreen")
        # Prefer the local file even when Wine has a built-in with the same name.
        local = APPS_DIR / program
        if local.is_file() and local.resolve().is_relative_to(APPS_DIR.resolve()):
            program = f"./{program}"
    except (OSError, ValueError, configparser.Error) as exc:
        log.error("invalid launch configuration %s: %s", LAUNCH_FILE, exc)
        return None

    return {
        "APP_CMD": "wine",
        "APP_ARGS": json.dumps([program, *args]),
        "FIT_MODE": "fullscreen" if fullscreen else "natural",
    }


def session_containers(all_states=False):
    try:
        return DOCKER.containers.list(
            all=all_states,
            filters={"label": f"{SESSION_LABEL}=1"},
        )
    except docker.errors.APIError as exc:
        # A container can be removed between the listing and the inspection
        # that the SDK does for each one. That is not worth failing over.
        log.debug("could not list the session containers: %s", exc)
        return []


def running_count():
    """Containers that are actually serving a visitor.

    Pre-warmed containers do not count against the limit: they are not
    taking up a visitor's slot yet.
    """
    return len([c for c in session_containers()
                if c.status == "running" and session_in_use(c)])


def is_pool_member(container):
    """Was this container prepared for the pool?"""
    return (container.labels or {}).get(WARM_LABEL) == "1"


def session_in_use(container):
    """Is this container serving a visitor rather than waiting in the pool?"""
    if not is_pool_member(container):
        # Started for a visitor straight away.
        return True
    return container_is_claimed(container)


def session_is_ending(container):
    """Has the session been condemned?

    The session writes a marker as soon as the visitor says goodbye or stops
    sending heartbeats. A session that is on its way out must never be handed
    out again: the visitor would only see "this session has already ended".
    """
    try:
        result = container.exec_run(["test", "-f", ENDING_MARKER])
        return result.exit_code == 0
    except Exception:                               # noqa: BLE001
        return False


def container_age_seconds(container):
    raw = (container.attrs.get("State", {}) or {}).get("StartedAt") or ""
    # Docker reports nanosecond precision, which fromisoformat() rejects.
    try:
        started = datetime.fromisoformat(raw[:26]).replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return (datetime.now(timezone.utc) - started).total_seconds()


def container_created_seconds(container):
    """How long ago the container object was created, started or not.

    A container that has been created but not started has no start time, so
    this is the only clock available for it.
    """
    raw = container.attrs.get("Created") or ""
    try:
        created = datetime.fromisoformat(raw[:26]).replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return (datetime.now(timezone.utc) - created).total_seconds()


def find_session(sid):
    if not SID_RE.match(sid or ""):
        return None
    try:
        for container in session_containers(all_states=True):
            if container.name == f"portos-{sid}":
                return container
    except docker.errors.APIError as exc:
        log.debug("could not look up session %s: %s", sid, exc)
    return None


def clamp(value, low, high):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(value, high))


def parse_memory(value, default_mb=1200):
    """Turn "1200m" / "2g" / "536870912" into a number of bytes."""
    text = str(value).strip().lower()
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    if text and text[-1] in units:
        try:
            return int(float(text[:-1]) * units[text[-1]])
        except ValueError:
            pass
    try:
        return int(text)
    except ValueError:
        return default_mb * 1024 ** 2


# ------------------------------------------------------------------ file push
#
# Bind mount sources sent to the Docker API are resolved on the *daemon host*,
# not inside this container, so a path like /apps would point at the wrong
# place (and on Docker Desktop it would point at a path that does not exist at
# all). The portable way to hand files to a session container is to push them
# through the archive API once the container exists but before it starts.

def _tar_bytes(entries):
    """Build an in-memory tar archive from {name: bytes}."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755 if name.endswith((".sh", ".exe", ".bat", ".cmd")) else 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def collect_app_files():
    """Collect the program and its supporting files from apps/."""
    entries = {}
    root = APPS_DIR.resolve()
    for path in APPS_DIR.rglob("*"):
        if path == LAUNCH_FILE or not path.is_file():
            continue
        source = path.resolve()
        if not source.is_relative_to(root):
            log.warning("refusing to ship %s: outside the apps directory", path)
            continue
        entries[path.relative_to(APPS_DIR).as_posix()] = source.read_bytes()
    return entries


def collect_web_files():
    entries = {}
    if not WEB_DIR.is_dir():
        return entries
    for path in WEB_DIR.rglob("*"):
        relative = path.relative_to(WEB_DIR)
        if relative.parts[0] != "status" and path.is_file():
            entries[relative.as_posix()] = path.read_bytes()
    return entries


def collect_wallpaper_files():
    try:
        root = WALLPAPER_DIR.resolve()
        source = (root / SESSION_WALLPAPER).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError("expected a file inside the session directory")
        return {"session-wallpaper": source.read_bytes()}
    except (OSError, ValueError, RuntimeError) as exc:
        log.warning("could not read wallpaper %r; using the image default: %s", SESSION_WALLPAPER, exc)
        return {}


# ---------------------------------------------------------------- session work

def create_session(launch, width=None, height=None, warm=False):
    if running_count() >= MAX_SESSIONS:
        raise RuntimeError("too many sessions are running, please try again later")

    sid = secrets.token_hex(16)
    name = f"portos-{sid}"

    # A warm container is prepared before anybody asks for it, so it uses the
    # default geometry; the viewer's first message corrects it to whatever
    # the visitor's browser window happens to be.
    init_w = clamp(width or INIT_W, 640, MAX_W)
    init_h = clamp(height or INIT_H, 480, MAX_H)

    environment = {
        **launch,
        **WALLPAPER_ENV,
        "TZ": TIMEZONE,
        "SESSION_ID": sid,
        "AUDIO_ENABLED": SESSION_AUDIO,
        "SESSION_CLIPBOARD": SESSION_CLIPBOARD,
        "INIT_W": str(init_w),
        "INIT_H": str(init_h),
        "MAX_W": str(MAX_W),
        "MAX_H": str(MAX_H),
        "RESET_GRACE": RESET_GRACE,
        "BOOT_GRACE": BOOT_GRACE,
        "BYE_GRACE": BYE_GRACE,
        # A warm container is only handed out once its program has a window.
        "REQUIRE_WINDOW": "1" if warm else "0",
    }

    try:
        container = DOCKER.containers.create(
            image=IMAGE,
            name=name,
            network=NETWORK,
            hostname=sid[:12],
            environment=environment,
            tmpfs={
                "/tmp": "rw,nosuid,nodev,size=768m",
                "/opt/portos/serve": "rw,nosuid,nodev,size=16m",
                "/run": "rw,nosuid,nodev,size=8m",
            },
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=512,
            mem_limit=parse_memory(os.environ.get("SESSION_MEMORY", "1200m")),
            labels={
                SESSION_LABEL: "1",
                WARM_LABEL: "1" if warm else "0",
            },
        )
    except docker.errors.APIError as exc:
        log.error("could not create the session container: %s", exc)
        raise RuntimeError("the session could not be started") from exc

    # Ship the work, viewer and wallpaper before the entrypoint runs.
    try:
        app_files = collect_app_files()
        if app_files:
            container.put_archive("/opt/apps", _tar_bytes(app_files))
            log.info("session %s received %d file(s)", sid, len(app_files))
        web_files = collect_web_files()
        if web_files:
            container.put_archive("/opt/portos/web-host", _tar_bytes(web_files))
        wallpaper_files = collect_wallpaper_files()
        if wallpaper_files:
            container.put_archive("/opt/portos", _tar_bytes(wallpaper_files))
    except (OSError, docker.errors.APIError) as exc:
        log.error("could not copy files into session %s: %s", sid, exc)
        try:
            container.remove(force=True)
        except docker.errors.APIError:
            pass
        raise RuntimeError("the session files could not be delivered") from exc

    try:
        container.start()
    except docker.errors.APIError as exc:
        log.error("could not start session %s: %s", sid, exc)
        try:
            container.remove(force=True)
        except docker.errors.APIError:
            pass
        raise RuntimeError("the session could not be started") from exc

    # A session that is started for a visitor straight away is spoken for.
    # The marker tells the container that somebody is supposed to arrive (it
    # then gives up if nobody does) and tells the server that this container
    # is not a spare in the pool.
    if not warm:
        try:
            container.exec_run(
                ["sh", "-c", f"echo {time.time():.3f} > {CLAIMED_MARKER}"])
        except Exception as exc:                    # noqa: BLE001
            log.warning("could not claim %s: %s", name, exc)

    # Only hand out the URL once the session is genuinely ready. The health
    # check covers both the noVNC web server and the control channel, so a
    # browser that connects straight away never meets a refused connection.
    if not wait_until_ready(container, timeout=int(os.environ.get("READY_TIMEOUT", "90"))):
        log.error("session %s did not become ready", sid)
        try:
            container.remove(force=True)
        except docker.errors.APIError:
            pass
        raise RuntimeError("the session did not become ready in time")

    log.info("session %s started (%dx%d)%s",
             sid, init_w, init_h, " [warm]" if warm else "")
    session = {
        "sid": sid,
        "url": f"/s/{sid}/viewer.html",
        "container": container.id[:12],
        "warm": warm,
    }
    return session


# --------------------------------------------------------------- warm pool

def container_is_claimed(container):
    """Has this container been handed to a visitor already?

    The marker is a file inside the container, because a container's labels
    cannot be changed after it is created. None means the probe failed.
    """
    try:
        result = container.exec_run(["test", "-f", CLAIMED_MARKER])
        if result.exit_code in (0, 1):
            return result.exit_code == 0
    except Exception:                               # noqa: BLE001
        pass
    return None


def warm_containers():
    """Running containers that nobody has claimed yet."""
    found = []
    try:
        # A list of label filters is ANDed by the daemon. A single string
        # with commas is not, so the comma form would silently match nothing.
        candidates = DOCKER.containers.list(
            filters={"label": [f"{SESSION_LABEL}=1", f"{WARM_LABEL}=1"]})
    except docker.errors.APIError as exc:
        log.warning("could not list the warm pool: %s", exc)
        return None

    for container in candidates:
        if container.status != "running" or not session_address(container):
            continue
        if container_is_claimed(container) is not False:
            continue
        found.append(container)
    return found


def session_environment(container):
    return (container.attrs.get("Config") or {}).get("Env") or []


def matches_clipboard(environment):
    values = dict(entry.split("=", 1) for entry in environment if "=" in entry)
    # Containers created before the rename still report the old key.
    mode = values.get("SESSION_CLIPBOARD", values.get("PORTOS_SESSION_CLIPBOARD", "both"))
    return mode == SESSION_CLIPBOARD


def changed_launch_settings(container, launch):
    environment = session_environment(container)
    expected = {**launch, **WALLPAPER_ENV, "AUDIO_ENABLED": SESSION_AUDIO, "TZ": TIMEZONE}
    changed = [key for key, value in expected.items() if f"{key}={value}" not in environment]
    if not matches_clipboard(environment):
        changed.append("SESSION_CLIPBOARD")
    return changed


def matches_launch(container, launch):
    return not changed_launch_settings(container, launch)


def claim_warm_session(launch):
    """Hand a pre-warmed container to a visitor, if one is ready."""
    if WARM_POOL <= 0:
        return None

    with _pool_lock:
        for container in warm_containers() or ():
            state = container.attrs.get("State") or {}
            if (state.get("Health", {}).get("Status") != "healthy"
                    or not matches_launch(container, launch)):
                continue
            try:
                # The claim time lets the reaper detect when nobody showed up.
                result = container.exec_run(
                    ["sh", "-c", f"echo {time.time():.3f} > {CLAIMED_MARKER}"])
                if result.exit_code != 0:
                    continue
            except Exception as exc:               # noqa: BLE001
                log.warning("could not claim %s: %s", container.name, exc)
                continue

            sid = container.name[len("portos-"):]
            _warm_spares.discard(container.name)
            _warm_refills[container.name] = "claimed_by_visitor"
            log.info("warm pool container claimed container=%s reason=claimed_by_visitor", container.name)
            return {
                "sid": sid,
                "url": f"/s/{sid}/viewer.html",
                "container": container.id[:12],
                "warm": True,
            }
    return None


def start_session(launch, width=None, height=None):
    session = claim_warm_session(launch)
    if session is None:
        session = create_session(launch, width=width, height=height)
    return session


def container_ceiling():
    """The most containers that may exist at the same time."""
    if MAX_CONTAINERS > 0:
        return MAX_CONTAINERS
    return MAX_SESSIONS + max(0, WARM_POOL) + 2


def warm_worker(launch, previous, reason, available):
    """Prepare one warm container."""
    previous_name = previous or "-"
    try:
        if len(session_containers()) >= container_ceiling():
            log.warning("warm pool refill skipped reason=%s previous_container=%s container_ceiling=%d",
                        reason, previous_name, container_ceiling())
            return
        log.info("warm pool refill started reason=%s previous_container=%s available=%d target=%d",
                 reason, previous_name, available, WARM_POOL)
        session = create_session(launch, warm=True)
    except Exception as exc:                       # noqa: BLE001
        log.error("warm pool refill failed reason=%s previous_container=%s error=%s",
                  reason, previous_name, exc)
        return

    name = f"portos-{session['sid']}"
    with _pool_lock:
        # The container can be claimed as soon as Docker reports it healthy.
        if name not in _warm_refills:
            _warm_spares.add(name)
        _warm_refills.pop(previous, None)
        log.info("warm pool refill completed reason=%s previous_container=%s container=%s",
                 reason, previous_name, name)


def maintain_pool():
    """Replace expired or outdated spares and replenish the warm pool."""
    global _warm_thread
    if WARM_POOL <= 0:
        return

    launch = load_launch()
    if launch is None:
        return

    with _pool_lock:
        spares = warm_containers()
        if spares is None:
            return
        names = {container.name for container in spares}
        for name in names - _warm_spares:
            if _warm_refills.get(name) == "spare_unavailable":
                del _warm_refills[name]
        for name in sorted(_warm_spares - names):
            _warm_refills[name] = "spare_unavailable"
            log.info("warm pool container unavailable container=%s reason=spare_unavailable", name)
        _warm_spares.clear()
        _warm_spares.update(names)

        ready = []
        for container in spares:
            changed = changed_launch_settings(container, launch)
            age = container_age_seconds(container)
            reasons = []
            details = ""
            if changed:
                reasons.append("config_changed")
                details = f" changed={','.join(changed)}"
            if age > WARM_MAX_AGE:
                reasons.append("max_age_exceeded")
                details += f" age_seconds={age:.0f} max_age_seconds={WARM_MAX_AGE}"
            if reasons:
                remove_session_container(container, "+".join(reasons), details)
            else:
                ready.append(container)

        # A container came back from the pool (its visitor never showed up),
        # so there can be too many. Keep the youngest.
        for extra in sorted(ready, key=container_age_seconds)[WARM_POOL:]:
            remove_session_container(extra, "pool_above_target", f" target={WARM_POOL}",
                                     replace_spare=False)

        if _warm_thread is not None and _warm_thread.is_alive():
            return
        if len(ready) >= WARM_POOL:
            _warm_refills.clear()
            return
        previous, reason = next(iter(_warm_refills.items()), (None, "pool_below_target"))
        _warm_thread = threading.Thread(
            target=warm_worker, args=(launch, previous, reason, len(ready)), daemon=True)
        _warm_thread.start()


def session_networks(container):
    return (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}


def session_address(container):
    """Route viewers to sessions attached only to the selected Docker network."""
    networks = session_networks(container)
    if networks.keys() == {NETWORK}:
        return networks[NETWORK].get("IPAddress") or None
    return None


def retire_mismatched_session(container):
    """Replace sessions when the configured network or clipboard mode changes."""
    changes = []
    if session_networks(container).keys() != {NETWORK}:
        changes.append("network")
    if not matches_clipboard(session_environment(container)):
        changes.append("SESSION_CLIPBOARD")
    if not changes:
        return False
    with _pool_lock:
        if (WARM_POOL > 0 and is_pool_member(container) and container.status == "running"
                and container_is_claimed(container) is False):
            _warm_spares.add(container.name)
        remove_session_container(container, "config_changed", f" changed={','.join(changes)}")
    return True


def wait_until_ready(container, timeout=90):
    """Block until the session container reports healthy, or the timeout hits.

    A session is only useful to a visitor once both the noVNC web server and
    the control channel are answering, so the server waits for that before it
    hands out the URL.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            container.reload()
        except docker.errors.NotFound:
            # Pool maintenance may have removed a warm container after an edit.
            return False
        except docker.errors.APIError:
            time.sleep(1)
            continue
        state = (container.attrs.get("State") or {})
        if state.get("Status") == "exited":
            log.error("session container exited during startup: %s",
                      state.get("Error") or state.get("ExitCode"))
            return False
        if state.get("Health", {}).get("Status") == "healthy":
            return True
        time.sleep(1)
    return False


def remove_session_container(container, reason, details="", *, replace_spare=True):
    with _pool_lock:
        name = container.name
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            return False
        except docker.errors.APIError as exc:
            log.error("could not remove container=%s reason=%s%s error=%s", name, reason, details, exc)
            return False

        if name in _warm_spares:
            _warm_spares.remove(name)
            if replace_spare:
                _warm_refills[name] = reason
        forget_live(name[len("portos-"):])
        kind = "warm pool container" if is_pool_member(container) else "session container"
        log.info("%s removed container=%s reason=%s%s", kind, name, reason, details)
        return True


def destroy_session(sid):
    container = find_session(sid)
    return container is not None and remove_session_container(container, "api_delete")


def reap_stale(container):
    """Destroy a session whose visitor has gone away for good.

    The container notices the missing heartbeat a minute later, but by then
    the session is still sitting there, holding on to its place in the
    session limit. The server saw the visitor's socket close, so it can
    clean up straight away.
    """
    sid = container.name[len("portos-"):]
    if visitor_present(sid):
        return
    left = left_since(sid)
    if left is None:
        # Nobody ever connected. requeue_unused() deals with that case: the
        # container goes back into the pool instead of being thrown away.
        return
    waited = time.monotonic() - left
    if waited <= STALE_GRACE:
        return
    remove_session_container(container, "visitor_disconnected",
                             f" disconnected_seconds={waited:.0f}")


def reap():
    """Remove finished containers and enforce the hard session TTL."""
    try:
        containers = session_containers(all_states=True)
    except Exception as exc:                       # noqa: BLE001
        log.error("reaper: %s", exc)
        return

    for container in containers:
        if container.status == "created":
            # The container exists but has not been started yet: the server is
            # in the middle of setting it up (copying the work in, then
            # starting it). Reaping it now would make that start fail with
            # "container is marked for removal and cannot be started", and
            # the visitor would get nothing. Only a container that never came
            # up is thrown away.
            age = container_created_seconds(container)
            if age < SETUP_GRACE:
                continue
            remove_session_container(container, "setup_timeout",
                                     f" age_seconds={age:.0f} setup_grace_seconds={SETUP_GRACE:g}")
            continue
        if container.status != "running":
            state = container.attrs.get("State") or {}
            remove_session_container(container, "container_stopped",
                                     f" status={container.status} exit_code={state.get('ExitCode')}"
                                     f" oom_killed={state.get('OOMKilled', False)}")
            continue
        if retire_mismatched_session(container):
            continue
        age = container_age_seconds(container)
        if age > SESSION_TTL:
            remove_session_container(container, "session_ttl_exceeded",
                                     f" age_seconds={age:.0f} ttl_seconds={SESSION_TTL}")
            continue
        requeue_unused(container)
        reap_stale(container)


def requeue_unused(container):
    """Deal with a container that was handed out but that nobody opened.

    A crawler that fetches "/" gets a session but never connects, and would
    otherwise hold the only warm container hostage. One that came from the
    pool goes back into the pool; one that was started for a visitor is
    thrown away, because the pool never had it in the first place.
    """
    try:
        if not container_is_claimed(container):
            return
        if container.exec_run(["test", "-f", VISITOR_MARKER]).exit_code == 0:
            return                                  # somebody really is using it
        claimed = container.exec_run(["cat", CLAIMED_MARKER]).output.decode().strip()
        claimed_at = float(claimed)
    except Exception:                               # noqa: BLE001
        return

    if time.time() - claimed_at < REQUEUE_GRACE:
        return

    if is_pool_member(container):
        with _pool_lock:
            try:
                result = container.exec_run(["rm", "-f", CLAIMED_MARKER])
                if result.exit_code == 0:
                    _warm_refills.pop(container.name, None)
                    log.info("warm pool container returned container=%s reason=visitor_never_connected",
                             container.name)
            except docker.errors.APIError:
                pass
    else:
        # It is only holding a session slot, and there is nothing to keep.
        remove_session_container(container, "visitor_never_connected")


def reaper_loop():
    """Remove finished containers and keep the warm pool topped up.

    The two are done in the same loop on purpose: when a visitor's session
    ends its container is removed here, and the very next cycle puts a fresh
    warm container in its place.
    """
    while True:
        time.sleep(WARM_INTERVAL if WARM_POOL > 0 else 10)
        try:
            reap()
        except Exception as exc:                   # noqa: BLE001
            # The reaper must never die: it is what cleans up after a
            # visitor has gone.
            log.error("reaper: %s", exc)
        try:
            maintain_pool()
        except Exception as exc:                   # noqa: BLE001
            log.error("pool maintenance failed: %s", exc)


# ----------------------------------------------------------------- http layer

class Handler(BaseHTTPRequestHandler):
    server_version = "PortOS/1.0"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _visitor_ip(self):
        address = self.headers.get("X-Real-IP", "").strip()
        if address and "%" not in address:
            try:
                return str(ip_address(address))
            except ValueError:
                pass
        return self.client_address[0]

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        if isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode()
        else:
            body = str(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            return body if isinstance(body, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _page(self, code, title, detail, *, retry=False):
        """A small page for the visitor, instead of a bare error."""
        try:
            template = Template((WEB_DIR / "status/status.html").read_text(encoding="utf-8"))
        except OSError as exc:
            log.warning("cannot read status page template: %s", exc)
            return self._send(code, f"{title}\n{detail}\n", "text/plain; charset=utf-8")

        retry_link = '<a href="/">重新进入</a>' if retry else ""
        body = template.substitute(title=escape(title), detail=escape(detail), retry_link=retry_link)
        return self._send(code, body, "text/html; charset=utf-8")

    def _redirect(self, location, sid=None):
        """Send the visitor onwards to their session."""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if sid:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={sid}; Path=/; SameSite=Lax; Max-Age={COOKIE_MAX_AGE}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _clear_cookie(self):
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}=; Path=/; SameSite=Lax; Max-Age=0")

    def _cookie_session(self):
        """The session this browser already has, if any."""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and SID_RE.match(value):
                return value
        return None

    def _location(self, session):
        """Where the visitor should go to reach this session."""
        return session["url"]

    def _start_session(self):
        """The single door: hand the visitor a session and send them to it."""
        # A visitor who opens a second tab keeps the session they already
        # have, instead of taking another one out of the pool. The session is
        # only handed out again when somebody is still looking at it: one
        # that nobody is looking at is either dying or already gone, and
        # sending the visitor back into it would only show them the message
        # "this session has already ended". Such a visitor gets a fresh
        # environment instead, which is also what the reset rule asks for.
        ip = self._visitor_ip()
        user_agent = self.headers.get("User-Agent", "")
        sid = self._cookie_session()
        if sid:
            container = find_session(sid)
            if (container is not None and container.status == "running"
                    and visitor_present(sid)
                    and not session_is_ending(container)):
                log.info("visitor entry ip=%s session=%s result=reused ua=%r", ip, sid, user_agent)
                return self._redirect(f"/s/{sid}/viewer.html", sid)

        launch = load_launch()
        if launch is None:
            log.warning("visitor entry ip=%s result=invalid_launch_config ua=%r", ip, user_agent)
            return self._page(503, "程序暂不可用", "请检查服务端的 launch.ini 配置。")

        try:
            session = start_session(launch)
        except RuntimeError as exc:
            log.warning("visitor entry ip=%s result=session_unavailable ua=%r error=%s",
                        ip, user_agent, exc)
            return self._page(503, "现在访客较多", "运行环境正在准备中，请稍候再试。", retry=True)

        log.info("visitor entry ip=%s session=%s result=assigned ua=%r", ip, session["sid"], user_agent)
        return self._redirect(self._location(session), session["sid"])

    def _proxy_session(self, sid, path):
        """Relay this request to the session container.

        Everything a browser needs from a session - the viewer, noVNC's
        modules, the picture and the control channel - comes through here.
        That way the session containers never need a port of their own and
        the whole service is reachable on a single port.
        """
        container = find_session(sid)
        if container is None:
            # The session is gone - it was destroyed when the visitor left.
            # Rather than showing a bare error, send the visitor to the
            # front door: it hands them a fresh environment.
            if path.endswith("/viewer.html") or path == f"/s/{sid}":
                return self._redirect("/")
            return self._send(404, {"error": "session not found"})

        address = session_address(container)
        if not address:
            return self._send(502, {"error": "the session has no address"})

        # Strip the /s/<sid> prefix: the container serves from its own root.
        target = path[len(f"/s/{sid}"):] or "/"
        is_websocket = self.headers.get("Upgrade", "").lower() == "websocket"

        lines = [f"{self.command} {target} {self.request_version}"]
        for key, value in self.headers.items():
            if key.lower() in ("host", "connection", "content-length"):
                continue
            lines.append(f"{key}: {value}")
        lines.append(f"Host: {address}:6080")
        if is_websocket:
            lines.append("Connection: Upgrade")
        else:
            # One request per connection keeps the relay simple.
            lines.append("Connection: close")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode()

        try:
            upstream = socket.create_connection((address, 6080), timeout=30)
        except OSError as exc:
            log.warning("cannot reach session %s: %s", sid, exc)
            return self._send(502, {"error": "the session is not reachable"})

        # Set when the browser's side of the socket goes away.
        client_gone = threading.Event()

        def pump_client():
            try:
                while True:
                    data = self.connection.recv(65536)
                    if not data:
                        break
                    upstream.sendall(data)
            except OSError:
                pass
            finally:
                client_gone.set()
                try:
                    upstream.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        try:
            upstream.sendall(request)
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                upstream.sendall(self.rfile.read(length))
            if TIMING:
                log.info("proxy %s: sent the request", sid)

            threading.Thread(target=pump_client, daemon=True).start()

            head = b""
            while b"\r\n\r\n" not in head:
                data = upstream.recv(4096)
                if not data:
                    break
                head += data
            if not head:
                return

            if is_websocket:
                # Relay the handshake untouched. Count the socket only after
                # the upstream accepted the upgrade and the reply was sent.
                self.connection.sendall(head)
                status = head.split(b"\r\n", 1)[0].split()
                upgraded = b"\r\n\r\n" in head and status[1:2] == [b"101"]
                if upgraded:
                    ip = self._visitor_ip()
                    mark_live(sid, ip, self.headers.get("User-Agent", ""))
                try:
                    while True:
                        if client_gone.is_set():
                            break
                        ready, _, _ = select.select([upstream], [], [], 1.0)
                        if not ready:
                            continue
                        data = upstream.recv(65536)
                        if not data:
                            break
                        self.connection.sendall(data)
                finally:
                    if upgraded:
                        clear_live(sid, ip)
            else:
                # The container answers with keep-alive, but we close after
                # every response, so say so instead of leaving the browser
                # with a connection that will not be there.
                head = re.sub(rb"(?im)^Connection:.*$", b"Connection: close", head)

                # Pull the whole body in as well, so the answer goes out in a
                # single write. Two small writes would let Nagle hold the
                # second one back for a fifth of a second.
                length_match = re.search(rb"(?im)^Content-Length:\s*(\d+)", head)
                if length_match:
                    want = int(length_match.group(1))
                    body = b""
                    while len(body) < want:
                        data = upstream.recv(65536)
                        if not data:
                            break
                        body += data
                    if TIMING:
                        log.info("proxy %s: got the response, %d bytes", sid, len(body))
                    self.connection.sendall(head + body)
                else:
                    self.connection.sendall(head)
                    while True:
                        data = upstream.recv(65536)
                        if not data:
                            break
                        self.connection.sendall(data)
        except OSError:
            pass
        finally:
            try:
                upstream.close()
            except OSError:
                pass
            self.close_connection = True

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        # Everything that belongs to a session goes straight through to it.
        match = re.match(r"^/s/([0-9a-f]{32})(/|$)", path)
        if match:
            return self._proxy_session(match.group(1), self.path)

        if path == "/":
            return self._start_session()

        if path in PUBLIC_ASSETS:
            try:
                payload = (WEB_DIR / path.lstrip("/")).read_bytes()
            except FileNotFoundError:
                return self._send(404, {"error": "not found"})
            return self._send(200, payload, PUBLIC_ASSETS[path])

        if path == "/healthz":
            return self._send(200, {"ok": True})

        match = re.match(r"^/api/sessions/([0-9a-f]{32})$", path)
        if match:
            sid = match.group(1)
            container = find_session(sid)
            if container is None:
                return self._send(404, {"error": "session not found"})
            return self._send(200, {
                "sid": sid,
                "status": container.status,
                "age": int(container_age_seconds(container)),
            })

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/sessions":
            return self._send(404, {"error": "not found"})

        body = self._read_json()
        launch = load_launch()
        if launch is None:
            return self._send(503, {"error": "invalid launch.ini configuration"})

        try:
            session = start_session(launch, width=body.get("w"), height=body.get("h"))
        except RuntimeError as exc:
            return self._send(503, {"error": str(exc)})

        return self._send(201, session)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        match = re.match(r"^/api/sessions/([0-9a-f]{32})$", path)
        if not match:
            return self._send(404, {"error": "not found"})
        if destroy_session(match.group(1)):
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # The browser should forget this session, so that the next visit
            # starts a fresh one.
            self._clear_cookie()
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send(404, {"error": "session not found"})


def main():
    network = DOCKER.networks.get(NETWORK)
    if SESSION_INTERNET == "false" and not network.attrs.get("Internal"):
        raise RuntimeError(f"network {NETWORK} must be internal when internet access is disabled")
    containers = DOCKER.containers.list(
        all=True, filters={"label": f"{SESSION_LABEL}=1"}, ignore_removed=True)
    for container in containers:
        retire_mismatched_session(container)

    threading.Thread(target=reaper_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8081"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("server listening on %d (image=%s network=%s max_sessions=%d)",
             port, IMAGE, NETWORK, MAX_SESSIONS)
    server.serve_forever()


if __name__ == "__main__":
    main()
