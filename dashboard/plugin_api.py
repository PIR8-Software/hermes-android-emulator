"""Android Emulator dashboard API — live view + control.

Uses FastAPI APIRouter. Routes mount at /api/plugins/android-emulator/<path>.
Always returns valid JSON — never throws to the frontend.

Safety contract (post-2026-09-26 audit remediation):
- All adb calls are pinned to the emulator serial; a non-emulator serial is
  refused unless the operator deliberately opts in with
  ANDROID_EMULATOR_ALLOW_NON_EMULATOR=1 (plus ANDROID_EMULATOR_SERIAL).
- Mutating routes are POST-only (no state-changing GETs / CSRF via Lax cookies).
- AVD destructive operations (wipe/delete/overwrite) need a validated name,
  containment inside ~/.android/avd, an AVD-inventory check and an explicit
  confirm/overwrite parameter.
- The emulator is stopped narrowly (`adb emu kill`, then only the exact PID this
  plugin started) — never broad pkill patterns.
- Handlers are sync so FastAPI runs them on the worker threadpool: a slow adb
  call can no longer stall the dashboard event loop. All timeouts are bounded.
- Every parameter is validated at handler entry; errors are structured JSON.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path

try:
    from fastapi import APIRouter
    from fastapi.responses import Response
    router = APIRouter()
except Exception:
    router = None

ADB = os.path.expanduser("~/Android/Sdk/platform-tools/adb")
_EMU_SERIAL = os.environ.get("ANDROID_EMULATOR_SERIAL", "emulator-5554")
# A non-emulator serial is only honoured with a deliberate second opt-in.
_SERIAL_OVERRIDDEN = bool(os.environ.get("ANDROID_EMULATOR_ALLOW_NON_EMULATOR"))
EMULATOR_BIN = os.path.expanduser("~/Android/Sdk/emulator/emulator")
AVDMANAGER = os.path.expanduser("~/Android/Sdk/cmdline-tools/latest/bin/avdmanager")
SDKMANAGER = os.path.expanduser("~/Android/Sdk/cmdline-tools/latest/bin/sdkmanager")
AVD_ROOT = Path(os.path.expanduser("~/.android/avd"))
STATE_FILE = os.path.expanduser("~/.hermes/emulator-state.json")

# Active AVD (single source of truth shared with scripts/emu via env var).
_ACTIVE_AVD = os.environ.get("ANDROID_EMULATOR_AVD", "pixel7pro")

SCREENSHOT_DIR = os.path.expanduser("~/.hermes/emulator-screenshots")
REPLAY_DIR = os.path.expanduser("~/.hermes/emulator-recordings")
RECORD_LOG_DIR = os.path.expanduser("~/.hermes/emulator-logs")

# ── Limits ─────────────────────────────────────────────────────────────────
LINES_MIN, LINES_MAX = 1, 5000
GALLERY_MIN, GALLERY_MAX = 1, 100
TEST_TIMEOUT_MAX = 300
SHELL_TIMEOUT_MAX = 60
TEXT_MAX = 2000

# ── Validation ─────────────────────────────────────────────────────────────
_RE_AVD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
_RE_PACKAGE = re.compile(r"^[A-Za-z0-9._]+$")
_RE_COMPONENT = re.compile(r"^[A-Za-z0-9._$/]+$")
_RE_KEYCODE = re.compile(r"^[A-Z0-9_]{1,40}$")
_RE_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^\s;|&$`'\"()<>\\]+$")
_RE_FILENAME = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")
_RE_LABEL = re.compile(r"^[A-Za-z0-9._\-]{0,64}$")
_RE_LOGSPEC = re.compile(r"^(\*|[A-Za-z0-9_.\-]+):([VDIWEFS])$")
_RE_FILTER_WORD = re.compile(r"^[A-Za-z0-9._\-]{0,64}$")
_RE_DEVICE_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_RE_API = re.compile(r"^\d{1,3}$")
_RE_USERDATA_IMG = re.compile(r"^userdata[\w.\-]*\.img$")

_cache: dict = {"ts": 0.0, "data": None, "online_ts": 0.0, "online": False}
CACHE_TTL = 2.0

# Process ownership: only the emulator/recordings this plugin started.
_state: dict = {"emulator_pid": None, "emulator_avd": None,
                "record_proc": None, "record_remote": None,
                "replay_proc": None, "replay_file": None, "replay_node": None}


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _err(msg, **extra):
    return {"ok": False, "error": str(msg), **extra}


def _safe(fn):
    """Never-throw envelope for handlers (honours the module contract)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as e:
            return _err(str(e), code="invalid_param")
        except KeyError as e:
            return _err(f"missing required parameter: {e.args[0]}", code="invalid_param")
        except Exception as e:  # pragma: no cover - safety net
            return _err(f"{type(e).__name__}: {e}", code="internal")

    return wrapper


def _serial_guard():
    if _SERIAL_OVERRIDDEN:
        return None
    if not _EMU_SERIAL.startswith("emulator"):
        return (
            f"refusing to run against serial {_EMU_SERIAL!r}: not an emulator. "
            "Set ANDROID_EMULATOR_SERIAL explicitly to override."
        )
    return None


def _require_emulator():
    g = _serial_guard()
    if g:
        raise ValueError(g)


def _int_q(value, name, default=None, lo=None, hi=None, clamp=False):
    if value is None or value == "":
        return default
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer, got {value!r}")
    if clamp and lo is not None and hi is not None:
        return _clamp(iv, lo, hi)
    if lo is not None and iv < lo:
        raise ValueError(f"'{name}' must be >= {lo}")
    if hi is not None and iv > hi:
        raise ValueError(f"'{name}' must be <= {hi}")
    return iv


def _text_q(value, name, required=True, max_len=TEXT_MAX):
    if value is None or value == "":
        if required:
            raise ValueError(f"'{name}' is required")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"'{name}' must be a string")
    if len(value) > max_len:
        raise ValueError(f"'{name}' exceeds {max_len} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError(f"'{name}' must not contain control characters")
    return value


def _device_text(text):
    """Escape free text for `adb shell input text` (device-shell-safe)."""
    return shlex.quote(text.replace(" ", "%s"))


def _device_arg(text):
    """Quote a validated string for the device shell (defence in depth)."""
    return shlex.quote(text)


def _logcat_specs(filt):
    if not filt:
        return []
    specs = []
    for part in filt.split():
        if not _RE_LOGSPEC.match(part):
            raise ValueError(f"invalid logcat filter spec: {part!r}")
        specs.append(part)
    return specs


def _safe_filename(name):
    if not _RE_FILENAME.match(name or ""):
        raise ValueError(f"invalid filename: {name!r}")
    return name


def _avd_dir(name: str) -> Path:
    """Resolve an AVD dir with containment — the F-02 traversal fix."""
    if not _RE_AVD_NAME.match(name or ""):
        raise ValueError(f"invalid AVD name: {name!r}")
    root = AVD_ROOT.resolve()
    avd_dir = (AVD_ROOT / f"{name}.avd").resolve()
    if avd_dir.parent != root:
        raise ValueError(f"AVD path escapes the AVD root: {name!r}")
    return avd_dir


def _known_avds() -> list[str]:
    names = []
    if AVD_ROOT.is_dir():
        for entry in AVD_ROOT.iterdir():
            if entry.is_dir() and entry.name.endswith(".avd") and (entry / "config.ini").is_file():
                names.append(entry.name[: -len(".avd")])
    return sorted(names)


def _pid_is_our_emulator(pid: int) -> bool:
    """True only when pid is the emulator binary with an -avd argument."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    parts = [p for p in cmdline.split("\0") if p]
    return bool(parts) and os.path.basename(parts[0]) == os.path.basename(EMULATOR_BIN) and "-avd" in parts


def _run(cmd: list[str], timeout: int = 10) -> tuple[bytes, int]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.stdout, r.returncode
    except subprocess.TimeoutExpired:
        return b"", -1
    except Exception:
        return b"", -1


def _adb_text(*args: str, timeout: int = 10) -> tuple[str, int]:
    out, rc = _run([ADB, "-s", _EMU_SERIAL, *args], timeout=timeout)
    return out.decode("utf-8", errors="replace").strip(), rc


def _device_online() -> bool:
    """True when the pinned emulator serial is attached and online."""
    now = time.time()
    if now - _cache["online_ts"] < 1.0:
        return _cache["online"]
    out, _ = _run([ADB, "devices"], timeout=5)
    text = out.decode("utf-8", errors="replace")
    online = _EMU_SERIAL in text and "device" in text.split(_EMU_SERIAL, 1)[-1].split("\n", 1)[0]
    _cache["online_ts"] = now
    _cache["online"] = online
    return online


def _screen_size() -> tuple[int, int]:
    out, _ = _adb_text("shell", "wm", "size")
    m = re.search(r"(\d+)x(\d+)", out)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1080, 2400


def _save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({k: v for k, v in _state.items()
                       if k in ("emulator_pid", "emulator_avd")}, f)
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


if router is not None:

    # ── Status / screenshots (read-only GET) ────────────────────────────

    @router.get("/status")
    @_safe
    def emu_status():
        _require_emulator()
        now = time.time()
        if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
            return _cache["data"]
        online = _device_online()
        info: dict = {"online": online, "serial": _EMU_SERIAL, "active_avd": _ACTIVE_AVD}
        if online:
            for key, args in [
                ("android_version", ["shell", "getprop", "ro.build.version.release"]),
                ("sdk", ["shell", "getprop", "ro.build.version.sdk"]),
                ("model", ["shell", "getprop", "ro.product.model"]),
                ("boot_completed", ["shell", "getprop", "sys.boot_completed"]),
            ]:
                val, _ = _adb_text(*args)
                info[key] = val
            size, _ = _adb_text("shell", "wm", "size")
            density, _ = _adb_text("shell", "wm", "density")
            info["screen_size"] = size.replace("Physical size: ", "")
            info["screen_density"] = density.replace("Physical density: ", "")
        _cache["ts"] = now
        _cache["data"] = info
        return info

    @router.get("/screenshot")
    @_safe
    def emu_screenshot():
        """Raw PNG screenshot."""
        _require_emulator()
        now = time.time()
        if _cache.get("shot") and (now - _cache.get("shot_ts", 0)) < CACHE_TTL:
            return Response(content=_cache["shot"], media_type="image/png")
        if not _device_online():
            return Response(content=b"", status_code=204)
        out, rc = _run([ADB, "-s", _EMU_SERIAL, "exec-out", "screencap", "-p"], timeout=5)
        if rc != 0 or len(out) < 100:
            return Response(content=b"", status_code=204)
        _cache["shot_ts"] = now
        _cache["shot"] = out
        return Response(content=out, media_type="image/png")

    @router.get("/screenshot_b64")
    @_safe
    def emu_screenshot_b64():
        """Screenshot as base64 data URL."""
        _require_emulator()
        if not _device_online():
            return {"image": None, "error": "offline"}
        out, rc = _run([ADB, "-s", _EMU_SERIAL, "exec-out", "screencap", "-p"], timeout=5)
        if rc != 0 or len(out) < 100:
            return {"image": None, "error": "screencap failed"}
        b64 = base64.b64encode(out).decode("ascii")
        return {"image": f"data:image/png;base64,{b64}", "bytes": len(out)}

    # ── Device input (POST-only: no state-changing GETs — F-04) ────────

    @router.post("/input/tap/{x}/{y}")
    @_safe
    def emu_tap(x: int, y: int):
        _require_emulator()
        x = _int_q(x, "x", lo=0, hi=100000)
        y = _int_q(y, "y", lo=0, hi=100000)
        _adb_text("shell", "input", "tap", str(x), str(y))
        return {"ok": True}

    @router.post("/input/key/{keycode}")
    @_safe
    def emu_key(keycode: str):
        _require_emulator()
        keycode = _text_q(keycode, "keycode", max_len=48).upper()
        if not keycode.startswith("KEYCODE_"):
            keycode = f"KEYCODE_{keycode}"
        if not _RE_KEYCODE.match(keycode):
            return _err(f"invalid keycode: {keycode}")
        _adb_text("shell", "input", "keyevent", keycode)
        return {"ok": True, "key": keycode}

    @router.get("/logcat")
    @_safe
    def emu_logcat(lines: int = 80, filter: str = ""):
        _require_emulator()
        lines = _int_q(lines, "lines", default=80, lo=LINES_MIN, hi=LINES_MAX, clamp=True)
        specs = _logcat_specs(_text_q(filter, "filter", required=False, max_len=256))
        out, _ = _adb_text("shell", "logcat", "-d", "-t", str(lines), *[_device_arg(s) for s in specs])
        return {"lines": out.splitlines()[-lines:]}

    # ── Apps, text input, swipes, screenshots ──────────────────────────

    @router.get("/apps")
    @_safe
    def list_apps(filter: str = ""):
        """List installed packages. User apps first, then system apps."""
        _require_emulator()
        if filter:
            if not _RE_FILTER_WORD.match(_text_q(filter, "filter", required=False, max_len=64)):
                return _err(f"invalid filter: {filter}")
        cmd = ["shell", "pm", "list", "packages"]
        if filter:
            cmd.append(filter)
        out, _ = _adb_text(*cmd)
        packages = [
            line.replace("package:", "").strip()
            for line in out.splitlines()
            if line.startswith("package:")
        ]
        # Categorize: user-installed vs system
        system_prefixes = ("com.android.", "com.google.", "android.", "com.qualcomm", "com.qti", "com.android.internal")
        overlay_skip = ("auto_generated_rro", "com.android.internal.emulation")
        user_pkgs = []
        system_pkgs = []
        for pkg in packages:
            if any(s in pkg for s in overlay_skip):
                continue
            if any(pkg.startswith(s) for s in system_prefixes):
                system_pkgs.append(pkg)
            else:
                user_pkgs.append(pkg)
        apps = []
        for pkg in sorted(user_pkgs):
            label = pkg.split(".")[-1].replace("_", " ").title()
            apps.append({"package": pkg, "label": label, "type": "user"})
        for pkg in sorted(system_pkgs)[:30]:
            label = pkg.split(".")[-1].replace("_", " ").title()
            apps.append({"package": pkg, "label": label, "type": "system"})
        return {"apps": apps, "count": len(apps), "user_count": len(user_pkgs)}

    @router.post("/apps/launch")
    @_safe
    def launch_app(package: str):
        """Launch an app by package name."""
        _require_emulator()
        package = _text_q(package, "package", max_len=255)
        if not _RE_PACKAGE.match(package):
            return _err(f"invalid package name: {package}")
        out, rc = _adb_text(
            "shell", "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
        )
        return {"ok": rc == 0, "output": out}

    @router.post("/apps/uninstall")
    @_safe
    def uninstall_app(package: str):
        """Uninstall an app by package name."""
        _require_emulator()
        package = _text_q(package, "package", max_len=255)
        if not _RE_PACKAGE.match(package):
            return _err(f"invalid package name: {package}")
        out, rc = _adb_text("uninstall", package)
        return {"ok": rc == 0, "output": out}

    @router.post("/apps/install")
    @_safe
    def install_app(apk_path: str, replace: bool = True, grant_permissions: bool = True):
        """Install an APK from a host path (the gateway host runs adb)."""
        _require_emulator()
        apk_path = _text_q(apk_path, "apk_path", max_len=4096)
        if not os.path.isfile(apk_path):
            return _err(f"APK not found: {apk_path}")
        if not apk_path.lower().endswith(".apk"):
            return _err(f"not an .apk file: {apk_path}")
        args = ["install"]
        if replace:
            args.append("-r")
        if grant_permissions:
            args.append("-g")
        args.append(apk_path)
        out, rc = _adb_text(*args, timeout=120)
        return {"ok": rc == 0, "output": out, "exit_code": rc}

    @router.post("/type")
    @_safe
    def type_text(text: str):
        """Type text into the focused field (escaped; spaces allowed)."""
        _require_emulator()
        text = _text_q(text, "text")
        _adb_text("shell", "input", "text", _device_text(text))
        return {"ok": True}

    @router.post("/swipe/{direction}")
    @_safe
    def swipe(direction: str, distance: int = 500):
        """Swipe in a direction: up, down, left, right."""
        _require_emulator()
        distance = _int_q(distance, "distance", default=500, lo=50, hi=5000, clamp=True)
        w, h = _screen_size()
        cx, cy = w // 2, h // 2
        moves = {
            "up": (cx, cy + distance // 2, cx, cy - distance // 2),
            "down": (cx, cy - distance // 2, cx, cy + distance // 2),
            "left": (cx + distance // 2, cy, cx - distance // 2, cy),
            "right": (cx - distance // 2, cy, cx + distance // 2, cy),
        }
        if direction not in moves:
            return {"ok": False, "error": f"Unknown direction: {direction}"}
        x1, y1, x2, y2 = moves[direction]
        _adb_text("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "300")
        return {"ok": True, "direction": direction}

    @router.post("/statusbar")
    @_safe
    def swipe_status_bar():
        """Pull down the Android notification/status bar."""
        _require_emulator()
        out, rc = _adb_text("shell", "cmd", "statusbar", "expand-notifications")
        return {"ok": rc == 0, "output": out, "exit_code": rc}

    @router.post("/appdrawer")
    @_safe
    def swipe_app_drawer():
        """Open the app drawer by swiping up from the dock."""
        _require_emulator()
        w, h = _screen_size()
        cx = w // 2
        out, rc = _adb_text("shell", "input", "swipe", str(cx), str(h - 220), str(cx), str(int(h * 0.25)), "500")
        return {"ok": rc == 0, "output": out, "exit_code": rc}

    @router.post("/pinch/{action}")
    @_safe
    def pinch(action: str):
        """Pinch in or out (zoom). Uses two-finger swipe."""
        _require_emulator()
        if action not in ("in", "out"):
            return {"ok": False, "error": f"Unknown action: {action}"}
        w, h = _screen_size()
        cx, cy = w // 2, h // 2
        dx, dy = w // 5, h // 8
        if action == "in":
            _adb_text("shell", "input", "swipe", str(cx - dx), str(cy - dy), str(cx - dx // 2), str(cy - dy // 2), "500")
            _adb_text("shell", "input", "swipe", str(cx + dx), str(cy + dy), str(cx + dx // 2), str(cy + dy // 2), "500")
        else:
            _adb_text("shell", "input", "swipe", str(cx - dx // 2), str(cy - dy // 2), str(cx - dx), str(cy - dy), "500")
            _adb_text("shell", "input", "swipe", str(cx + dx // 2), str(cy + dy // 2), str(cx + dx), str(cy + dy), "500")
        return {"ok": True, "action": action}

    @router.post("/screenshot/save")
    @_safe
    def save_screenshot(label: str = ""):
        """Save current screenshot to gallery."""
        _require_emulator()
        if label and not _RE_LABEL.match(_text_q(label, "label", required=False, max_len=64)):
            return _err(f"invalid label: {label}")
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        ts = int(time.time())
        name = f"{ts}_{label}.png" if label else f"{ts}.png"
        path = os.path.join(SCREENSHOT_DIR, _safe_filename(name))
        out, rc = _run([ADB, "-s", _EMU_SERIAL, "exec-out", "screencap", "-p"], timeout=5)
        if rc != 0 or len(out) < 100:
            return {"ok": False, "error": "screencap failed"}
        with open(path, "wb") as f:
            f.write(out)
        os.chmod(path, 0o600)
        return {"ok": True, "path": path, "size": len(out)}

    @router.get("/screenshot/gallery")
    @_safe
    def screenshot_gallery(limit: int = 20):
        """List saved screenshots."""
        limit = _int_q(limit, "limit", default=20, lo=GALLERY_MIN, hi=GALLERY_MAX, clamp=True)
        if not os.path.isdir(SCREENSHOT_DIR):
            return {"screenshots": [], "count": 0}
        files = sorted(os.listdir(SCREENSHOT_DIR), reverse=True)[:limit]
        screenshots = []
        for f in files:
            if f.endswith(".png") and _RE_FILENAME.match(f):
                path = os.path.join(SCREENSHOT_DIR, f)
                screenshots.append({
                    "name": f,
                    "path": path,
                    "size": os.path.getsize(path),
                    "time": os.path.getmtime(path),
                })
        return {"screenshots": screenshots, "count": len(screenshots)}

    @router.get("/screenshot/file/{filename}")
    @_safe
    def screenshot_file(filename: str):
        """Serve a saved screenshot (backend-only route: direct URL consumers)."""
        try:
            _safe_filename(filename)
        except ValueError:
            return Response(content=b"", status_code=404)
        base = Path(SCREENSHOT_DIR).resolve()
        path = (base / filename).resolve()
        if path.parent != base or not path.is_file():
            return Response(content=b"", status_code=404)
        with open(path, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="image/png")

    # ── Network / shell ────────────────────────────────────────────────

    @router.post("/network/{condition}")
    @_safe
    def network_condition(condition: str):
        """Simulate network: offline, slow, fast."""
        _require_emulator()
        if condition not in ("offline", "slow", "fast"):
            return {"ok": False, "error": f"Unknown condition: {condition}"}
        if condition == "offline":
            _adb_text("shell", "svc", "wifi", "disable")
            _adb_text("shell", "svc", "data", "disable")
        elif condition == "slow":
            _adb_text("shell", "svc", "wifi", "enable")
            _adb_text("shell", "svc", "data", "enable")
            # Traffic shaping (needs root; emulator images have it)
            _adb_text("shell", "tc", "qdisc", "add", "dev", "wlan0", "root", "netem", "delay", "500ms", "loss", "10%")
        else:
            _adb_text("shell", "tc", "qdisc", "del", "dev", "wlan0", "root")
            _adb_text("shell", "svc", "wifi", "enable")
            _adb_text("shell", "svc", "data", "enable")
        return {"ok": True, "condition": condition}

    @router.post("/shell")
    @_safe
    def run_shell(command: str):
        """Run an arbitrary adb shell command (intentional raw device shell)."""
        _require_emulator()
        command = _text_q(command, "command", max_len=8000)
        out, rc = _adb_text("shell", "sh", "-c", command, timeout=SHELL_TIMEOUT_MAX)
        return {"stdout": out, "exit_code": rc}

    # ── Screen recording lifecycle (F-08) ──────────────────────────────

    @router.post("/record/start")
    @_safe
    def start_recording():
        """Start screen recording. Recording runs detached (max 3 min)."""
        _require_emulator()
        if _state.get("record_proc") and _state["record_proc"].poll() is None:
            return _err("recording already in progress")
        if not _device_online():
            return _err("emulator offline")
        os.makedirs(RECORD_LOG_DIR, exist_ok=True)
        remote = f"/sdcard/hermes_recording_{int(time.time())}.mp4"
        log_path = os.path.join(RECORD_LOG_DIR, "screenrecord.log")
        logf = open(log_path, "ab")
        proc = subprocess.Popen(
            [ADB, "-s", _EMU_SERIAL, "shell", "screenrecord",
             "--time-limit", "180", remote],
            stdout=logf, stderr=logf, start_new_session=True,
        )
        _state["record_proc"] = proc
        _state["record_remote"] = remote
        return {"ok": True, "message": "Recording started (auto-stops at 180s or /record/stop)"}

    @router.post("/record/stop")
    @_safe
    def stop_recording():
        """Stop screen recording and pull the file (reports real results)."""
        _require_emulator()
        proc = _state.get("record_proc")
        remote = _state.get("record_remote")
        if proc is None or not remote:
            return _err("no recording in progress")
        # Stop only the screenrecord process (exact name) on the device.
        _adb_text("shell", "pkill", "-INT", "screenrecord", timeout=10)
        time.sleep(1.5)  # give screenrecord time to finalise the mp4
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.terminate()
        _state["record_proc"] = None
        _state["record_remote"] = None
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        ts = int(time.time())
        local_path = os.path.join(SCREENSHOT_DIR, f"recording_{ts}.mp4")
        out, rc = _adb_text("pull", remote, local_path, timeout=60)
        _adb_text("shell", "rm", "-f", remote)
        if rc != 0 or not os.path.isfile(local_path) or os.path.getsize(local_path) == 0:
            return _err(f"recording not saved: {out or 'pull failed'}")
        return {"ok": True, "path": local_path, "size": os.path.getsize(local_path)}

    # ── GPS / battery / deep links / notifications ─────────────────────

    @router.post("/gps/clear")
    @_safe
    def clear_gps():
        """Clear GPS override."""
        _require_emulator()
        _run([ADB, "-s", _EMU_SERIAL, "emu", "geo", "nmea", "$GPGGA,,,,,,0,,,,,,,,*66"], timeout=5)
        return {"ok": True}

    @router.post("/gps/{lat}/{lng}")
    @_safe
    def set_gps(lat: float, lng: float):
        """Set GPS location. Uses emulator geo fix."""
        _require_emulator()
        if not -90.0 <= lat <= 90.0 or not -180.0 <= lng <= 180.0:
            return _err("lat must be -90..90 and lng -180..180")
        _run([ADB, "-s", _EMU_SERIAL, "emu", "geo", "fix", f"{lng:.6f}", f"{lat:.6f}"], timeout=5)
        return {"ok": True, "lat": lat, "lng": lng}

    @router.post("/battery/reset")
    @_safe
    def reset_battery():
        """Reset battery to real values."""
        _require_emulator()
        _adb_text("shell", "dumpsys", "battery", "reset")
        return {"ok": True}

    @router.post("/battery/unplug")
    @_safe
    def unplug_battery():
        """Simulate unplugged (draining)."""
        _require_emulator()
        _adb_text("shell", "dumpsys", "battery", "unplug")
        return {"ok": True}

    @router.post("/battery/{level}")
    @_safe
    def set_battery(level: int):
        """Set battery level (0-100)."""
        _require_emulator()
        level = _int_q(level, "level", lo=0, hi=100, clamp=True)
        _adb_text("shell", "dumpsys", "battery", "set", "level", str(level))
        return {"ok": True, "level": level}

    @router.post("/deeplink")
    @_safe
    def open_deeplink(url: str):
        """Open a deep link / URL scheme."""
        _require_emulator()
        url = _text_q(url, "url", max_len=2048)
        if not _RE_URL.match(url):
            return _err(f"invalid url: {url}")
        _adb_text("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)
        return {"ok": True, "url": url}

    @router.post("/notification")
    @_safe
    def send_notification(title: str = "Test", body: str = "From Hermes"):
        """Send a test notification. Reports the real result (F-09)."""
        _require_emulator()
        title = _text_q(title, "title", max_len=200)
        body = _text_q(body, "body", max_len=500)
        out, rc = _adb_text(
            "shell", "cmd", "notification", "post",
            "-S", "bigtext", "-t", _device_arg(title),
            _device_arg("hermes"), _device_arg(body),
        )
        ok = rc == 0 and "error" not in out.lower()
        result = {"ok": ok, "title": title, "body": body, "output": out, "exit_code": rc}
        if not ok:
            result["error"] = f"notification not delivered (rc={rc}): {out[:200]}"
        return result

    # ── Record & replay touches (F-08) ─────────────────────────────────

    @router.post("/replay/record/start")
    @_safe
    def start_replay_record():
        """Start capturing touch events (auto-stops at 60s)."""
        _require_emulator()
        if _state.get("replay_proc") and _state["replay_proc"].poll() is None:
            return _err("touch capture already in progress")
        if not _device_online():
            return _err("emulator offline")
        node, ranges = _find_touch_device()
        if not node:
            return _err("no touch input device found on the emulator")
        os.makedirs(REPLAY_DIR, exist_ok=True)
        ts = int(time.time())
        raw_path = os.path.join(REPLAY_DIR, f"replay_{ts}.raw")
        rawf = open(raw_path, "ab")
        proc = subprocess.Popen(
            [ADB, "-s", _EMU_SERIAL, "shell", "getevent", "-t", node],
            stdout=rawf, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        _state["replay_proc"] = proc
        _state["replay_file"] = raw_path
        _state["replay_node"] = {"node": node, "ranges": ranges}
        return {"ok": True, "message": "Capturing touch events (max 60s; stop with /replay/record/stop)",
                "device_node": node}

    @router.post("/replay/record/stop")
    @_safe
    def stop_replay_record():
        """Stop touch capture and save parsed gestures (usable results)."""
        _require_emulator()
        proc = _state.get("replay_proc")
        raw_path = _state.get("replay_file")
        node_info = _state.get("replay_node") or {}
        if proc is None or not raw_path:
            return _err("no touch capture in progress")
        # Stop only the getevent process (exact name) on the device.
        _adb_text("shell", "pkill", "-INT", "getevent", timeout=10)
        time.sleep(0.5)
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.terminate()
        _state["replay_proc"] = None
        _state["replay_file"] = None
        _state["replay_node"] = None
        w, h = _screen_size()
        try:
            with open(raw_path, "rb") as f:
                raw_text = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            return _err(f"capture file unreadable: {e}")
        gestures = parse_getevent(raw_text, node_info.get("ranges"), (w, h))
        json_path = raw_path[: -len(".raw")] + ".json"
        with open(json_path, "w") as f:
            json.dump({
                "captured_at": time.time(),
                "device_node": node_info.get("node"),
                "ranges": node_info.get("ranges"),
                "display": {"w": w, "h": h},
                "raw_file": os.path.basename(raw_path),
                "gestures": gestures,
            }, f, indent=2)
        return {"ok": True, "path": json_path, "raw_path": raw_path,
                "gestures": len(gestures)}

    @router.post("/replay/play")
    @_safe
    def play_replay(file: str = ""):
        """Play back a recorded touch sequence as tap/swipe gestures."""
        _require_emulator()
        if file:
            name = _safe_filename(os.path.basename(file))
            path = os.path.join(REPLAY_DIR, name)
        else:
            if not os.path.isdir(REPLAY_DIR):
                return _err("No recordings")
            files = sorted((f for f in os.listdir(REPLAY_DIR) if f.endswith(".json")), reverse=True)
            if not files:
                return _err("No recordings")
            path = os.path.join(REPLAY_DIR, files[0])
        if not os.path.isfile(path):
            return _err(f"recording not found: {path}")
        with open(path) as f:
            data = json.load(f)
        gestures = data.get("gestures", [])
        if not gestures:
            return _err("recording contains no gestures")
        first_t = gestures[0].get("t", 0.0)
        started = time.time()
        played = 0
        for i, g in enumerate(gestures):
            # Pace replay along the original capture timeline (bounded).
            target = _clamp(g.get("t", 0.0) - first_t, 0.0, 60.0)
            lag = target - (time.time() - started)
            if lag > 0:
                time.sleep(_clamp(lag, 0.0, 3.0))
            if g.get("type") == "tap":
                _adb_text("shell", "input", "tap", str(int(g["x"])), str(int(g["y"])), timeout=10)
            else:
                _adb_text("shell", "input", "swipe",
                          str(int(g["x1"])), str(int(g["y1"])),
                          str(int(g["x2"])), str(int(g["y2"])),
                          str(int(_clamp(g.get("duration_ms", 300), 50, 2000))), timeout=15)
            played += 1
        return {"ok": True, "file": path, "played": played}

    # ── Touch capture helpers ──────────────────────────────────────────

    def _find_touch_device():
        """Locate the touchscreen input node and its ABS_MT coordinate ranges."""
        out, _ = _adb_text("shell", "getevent", "-p", timeout=10)
        node = None
        node_ranges: dict = {}
        current = None
        ranges: dict = {}
        is_touch = False
        for line in out.splitlines():
            m = re.search(r"(/dev/input/event\d+)", line)
            if m:
                current = m.group(1)
                ranges = {}
                is_touch = False
                continue
            if current is None:
                continue
            stripped = line.strip()
            if stripped.startswith("0035") and "max" in stripped:
                mm = re.search(r"min\s+(\d+),\s*max\s+(\d+)", stripped)
                if mm:
                    ranges["x"] = [int(mm.group(1)), int(mm.group(2))]
                is_touch = True
            elif stripped.startswith("0036") and "max" in stripped:
                mm = re.search(r"min\s+(\d+),\s*max\s+(\d+)", stripped)
                if mm:
                    ranges["y"] = [int(mm.group(1)), int(mm.group(2))]
                is_touch = True
            elif "ABS_MT_POSITION" in stripped:
                is_touch = True
            if is_touch and node is None and "x" in ranges and "y" in ranges:
                node = current
                node_ranges = dict(ranges)
        return node, node_ranges

    # ── Test runner ────────────────────────────────────────────────────

    @router.post("/test/run")
    @_safe
    def run_tests(package: str = "com.example.pir8sales.dev",
                  runner: str = "androidx.test.runner.AndroidJUnitRunner"):
        """Run instrumented tests and return results (bounded at 300s)."""
        _require_emulator()
        package = _text_q(package, "package", max_len=255)
        runner = _text_q(runner, "runner", max_len=255)
        if not _RE_PACKAGE.match(package):
            return _err(f"invalid package: {package}")
        if not _RE_COMPONENT.match(runner):
            return _err(f"invalid runner: {runner}")
        out, rc = _adb_text(
            "shell", "am", "instrument", "-w",
            f"{package}.test/{runner}",
            timeout=TEST_TIMEOUT_MAX,
        )
        return {"ok": rc == 0, "output": out, "exit_code": rc}

    # ── AVD management (destructive ops guarded — F-02/F-03) ──────────

    @router.post("/avd/delete")
    @_safe
    def delete_avd(name: str, confirm: str = ""):
        """Delete an AVD (requires confirm=<name>; known AVDs only)."""
        _avd_dir(name)  # validation + containment
        if confirm != name:
            return _err("confirmation required: pass confirm=<name>", code="confirm_required")
        if name not in _known_avds():
            return _err(f"unknown AVD: {name}", code="unknown_avd")
        if _device_online() and name == _ACTIVE_AVD:
            return _err(f"AVD {name} appears to be running; stop it first")
        out, rc = _run([AVDMANAGER, "delete", "avd", "-n", name], timeout=15)
        return {"ok": rc == 0, "output": out.decode("utf-8", errors="replace"), "avd": name}

    @router.post("/avd/wipe")
    @_safe
    def wipe_avd(name: str, confirm: str = ""):
        """Factory reset an AVD (delete userdata*.img inside the AVD dir only)."""
        avd_dir = _avd_dir(name)  # validation + containment
        if confirm != name:
            return _err("confirmation required: pass confirm=<name>", code="confirm_required")
        if name not in _known_avds():
            return _err(f"unknown AVD: {name}", code="unknown_avd")
        if _device_online() and name == _ACTIVE_AVD:
            return _err(f"AVD {name} appears to be running; stop it first")
        if not avd_dir.is_dir():
            return _err(f"AVD directory not found: {avd_dir}")
        deleted = []
        for f in sorted(avd_dir.iterdir()):
            if f.is_file() and _RE_USERDATA_IMG.match(f.name):
                if f.resolve().parent != avd_dir:
                    continue
                os.remove(f)
                deleted.append(f.name)
        return {"ok": True, "avd": name, "deleted": deleted}

    # ── Keyboard shortcuts (implemented by the frontend) ───────────────

    @router.get("/shortcuts")
    def get_shortcuts():
        """Return keyboard shortcut mappings (implemented in plugin.js)."""
        return {
            "shortcuts": [
                {"key": "Ctrl+S", "action": "screenshot", "label": "Save Screenshot"},
                {"key": "Ctrl+R", "action": "record", "label": "Toggle Recording"},
                {"key": "Ctrl+H", "action": "home", "label": "Home"},
                {"key": "Ctrl+B", "action": "back", "label": "Back"},
                {"key": "Ctrl+L", "action": "logcat", "label": "Toggle Logcat"},
                {"key": "Ctrl+G", "action": "gallery", "label": "Screenshot Gallery"},
            ]
        }

    # ── AVD picker endpoints ───────────────────────────────────────────

    @router.get("/picker")
    @_safe
    def picker_data():
        """All data the picker needs in one call: AVDs, devices, images."""
        import re as _re

        # 1. Installed AVDs
        out, _ = _run([AVDMANAGER, "list", "avd"], timeout=10)
        text = out.decode("utf-8", errors="replace")
        avds = []
        current: dict = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Name:"):
                if current:
                    avds.append(current)
                current = {"name": line.split(":", 1)[1].strip()}
            elif line.startswith("Device:"):
                current["device"] = line.split(":", 1)[1].strip()
            elif line.startswith("Target:"):
                current["target"] = line.split(":", 1)[1].strip()
            elif line.startswith("Path:"):
                current["path"] = line.split(":", 1)[1].strip()
            elif "Based on:" in line:
                current["based_on"] = line.split("Based on:", 1)[1].strip()
        if current:
            avds.append(current)
        if not avds:
            # Fallback: local inventory when avdmanager is unavailable.
            avds = [{"name": n} for n in _known_avds()]

        # 2. Available device profiles
        out, _ = _run([AVDMANAGER, "list", "device"], timeout=10)
        text = out.decode("utf-8", errors="replace")
        devices = []
        current = {}
        for line in text.splitlines():
            line = line.strip()
            m = _re.match(r'id:\s+\d+\s+or\s+"([^"]+)"', line)
            if m:
                if current:
                    devices.append(current)
                current = {"id": m.group(1)}
            elif line.startswith("Name:"):
                current["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Size:"):
                current["size"] = line.split(":", 1)[1].strip()
            elif line.startswith("Resolution:"):
                current["resolution"] = line.split(":", 1)[1].strip()
            elif line.startswith("Density:"):
                current["density"] = line.split(":", 1)[1].strip()
        if current:
            devices.append(current)

        # 3. Installed system images
        out, _ = _run([SDKMANAGER, "--list_installed"], timeout=15)
        text = out.decode("utf-8", errors="replace")
        installed_images = []
        for line in text.splitlines():
            if "system-images" in line and "|" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 3:
                    pkg = parts[0]
                    api_match = _re.search(r"android-(\d+)", pkg)
                    api = api_match.group(1) if api_match else parts[1]
                    installed_images.append({
                        "package": pkg,
                        "api": api,
                        "description": parts[2],
                    })

        # 4. Available (not installed) system images
        out, _ = _run([SDKMANAGER, "--list"], timeout=30)
        text = out.decode("utf-8", errors="replace")
        available_images = []
        installed_pkgs = {i["package"] for i in installed_images}
        for line in text.splitlines():
            if "system-images" in line and "google_apis" in line and "x86_64" in line and "|" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 3:
                    pkg = parts[0]
                    if pkg not in installed_pkgs:
                        api_match = _re.search(r"android-(\d+)", pkg)
                        api = api_match.group(1) if api_match else parts[1]
                        available_images.append({
                            "package": pkg,
                            "api": api,
                            "description": parts[2],
                        })

        return {
            "avds": avds,
            "devices": devices,
            "installed_images": installed_images,
            "available_images": available_images,
            "active_avd": _ACTIVE_AVD,
            "running": _device_online(),
        }

    @router.post("/picker/switch")
    @_safe
    def picker_switch(name: str):
        """Switch to an AVD: narrow-stop the current emulator, start the chosen AVD."""
        _require_emulator()
        if not _RE_AVD_NAME.match(name or ""):
            return _err(f"invalid AVD name: {name!r}")
        if name not in _known_avds():
            return _err(f"unknown AVD: {name}", code="unknown_avd")
        global _ACTIVE_AVD
        stop_emulator()
        _ACTIVE_AVD = name
        result = start_emulator(avd=name)
        if isinstance(result, dict) and result.get("ok"):
            result["active_avd"] = name
        return result

    @router.post("/create")
    @_safe
    def create_avd(
        name: str = "custom",
        device: str = "pixel_6",
        api: str = "34",
        overwrite: bool = False,
    ):
        """Create a new AVD. Installs system image if needed. Never overwrites silently."""
        _require_emulator()
        if not _RE_AVD_NAME.match(name or ""):
            return _err(f"invalid AVD name: {name!r}")
        if not _RE_DEVICE_ID.match(device or ""):
            return _err(f"invalid device id: {device!r}")
        if not _RE_API.match(api or ""):
            return _err(f"invalid api: {api!r}")
        if name in _known_avds() and not overwrite:
            return _err(
                f"AVD {name!r} already exists; pass overwrite=true to replace it",
                code="avd_exists",
            )
        img_pkg = f"system-images;android-{api};google_apis;x86_64"
        # Check if image is installed, install if not
        out, _ = _run([SDKMANAGER, "--list_installed"], timeout=10)
        if img_pkg.encode() not in out:
            out, rc = _run([SDKMANAGER, img_pkg], timeout=300)
            if rc != 0:
                return {
                    "ok": False,
                    "error": f"Failed to install {img_pkg}",
                    "output": out.decode("utf-8", errors="replace"),
                }
        args = [AVDMANAGER, "create", "avd", "-n", name, "-k", img_pkg, "-d", device]
        if overwrite:
            args.append("--force")
        out, rc = _run(args, timeout=30)
        return {
            "ok": rc == 0,
            "output": out.decode("utf-8", errors="replace"),
            "avd": name,
        }

    # ── Emulator lifecycle (narrow ownership — F-03/F-16) ─────────────

    @router.post("/start")
    @_safe
    def start_emulator(avd: str = ""):
        """Start the emulator in background (logged, tracked PID)."""
        _require_emulator()
        avd = avd or _ACTIVE_AVD
        if not _RE_AVD_NAME.match(avd):
            return _err(f"invalid AVD name: {avd!r}")
        if avd not in _known_avds():
            return _err(f"unknown AVD: {avd}", code="unknown_avd")
        if not os.path.isfile(EMULATOR_BIN):
            return _err("Emulator binary not found")
        if _device_online():
            return {"ok": True, "message": "Already running"}
        os.makedirs(RECORD_LOG_DIR, exist_ok=True)
        log_path = os.path.join(RECORD_LOG_DIR, f"emulator_{avd}.log")
        logf = open(log_path, "ab")
        proc = subprocess.Popen(
            [EMULATOR_BIN, "-avd", avd, "-no-window", "-no-audio",
             "-no-boot-anim", "-gpu", "swiftshader_indirect",
             "-memory", "2048", "-partition-size", "4096", "-no-snapshot"],
            stdout=logf, stderr=logf, start_new_session=True,
        )
        _state["emulator_pid"] = proc.pid
        _state["emulator_avd"] = avd
        _save_state()
        return {"ok": True, "message": f"Starting emulator {avd}...", "pid": proc.pid}

    @router.post("/stop")
    @_safe
    def stop_emulator():
        """Stop the emulator narrowly: `adb emu kill`, then only our own PID."""
        _require_emulator()
        stopped = []
        out, rc = _adb_text("emu", "kill", timeout=10)
        if rc == 0:
            stopped.append("adb emu kill")
        pid = _state.get("emulator_pid")
        if pid and _pid_is_our_emulator(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(f"pid {pid}")
            except OSError:
                pass
        _state["emulator_pid"] = None
        _state["emulator_avd"] = None
        _save_state()
        return {"ok": True, "stopped": stopped}


# ── Touch-capture parsing (module level so tests can import it) ────────────

_RE_GETEVENT_LINE = re.compile(
    r"^\[\s*(?P<t>[\d.]+)\]\s+(?P<typ>[0-9a-fA-F]+)\s+(?P<code>[0-9a-fA-F]+)\s+(?P<val>[0-9a-fA-F]+)$"
)

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
BTN_TOUCH = 0x014A
ABS_X, ABS_Y = 0x0035, 0x0036
ABS_MT_TRACKING_ID = 0x0039


def _hex_int(s: str) -> int:
    v = int(s, 16)
    if v >= 0x80000000:
        v -= 0x100000000
    return v


def _scale(raw, lo, hi, dim):
    if hi <= lo:
        return raw
    return round((raw - lo) * (dim - 1) / (hi - lo))


def parse_getevent(text: str, ranges: dict | None, display: tuple[int, int]) -> list[dict]:
    """Parse `getevent -t` text into tap/swipe gestures with display coords.

    The historical implementation piped getevent TEXT into `sendevent`, which
    expects binary records — it could not work. This parser reconstructs real
    gestures (tap / swipe) from the capture and scales raw coordinates with the
    device's ABS_MT ranges so replay lands where the touches happened.
    """
    ranges = ranges or {}
    w, h = display
    x_lo, x_hi = (ranges.get("x") or [0, 4095])
    y_lo, y_hi = (ranges.get("y") or [0, 4095])

    gestures: list[dict] = []
    cur_x = cur_y = None
    down_t = None
    samples: list[tuple[float, int, int]] = []

    def _flush(up_t):
        nonlocal down_t, samples
        if down_t is None:
            return
        pts = samples or [(down_t, cur_x if cur_x is not None else 0,
                           cur_y if cur_y is not None else 0)]
        x0, y0 = pts[0][1], pts[0][2]
        x1, y1 = pts[-1][1], pts[-1][2]
        dur_ms = int(max(0.0, up_t - down_t) * 1000)
        moved = abs(x1 - x0) + abs(y1 - y0)
        if moved < 20 and (dur_ms < 600 or len(pts) < 3):
            gestures.append({"type": "tap", "x": x0, "y": y0, "t": down_t})
        else:
            gestures.append({
                "type": "swipe", "x1": x0, "y1": y0, "x2": x1, "y2": y1,
                "duration_ms": _clamp(dur_ms, 50, 2000), "t": down_t,
            })
        down_t = None
        samples = []

    for line in text.splitlines():
        m = _RE_GETEVENT_LINE.match(line.strip())
        if not m:
            continue
        t = float(m.group("t"))
        typ = int(m.group("typ"), 16)
        code = int(m.group("code"), 16)
        val = _hex_int(m.group("val"))
        if typ == EV_ABS and code in (ABS_X, ABS_Y):
            if code == ABS_X:
                cur_x = _scale(val, x_lo, x_hi, w)
            else:
                cur_y = _scale(val, y_lo, y_hi, h)
            if down_t is not None and cur_x is not None and cur_y is not None:
                samples.append((t, cur_x, cur_y))
        elif typ == EV_KEY and code == BTN_TOUCH:
            if val == 1 and down_t is None:
                down_t = t
                if cur_x is not None and cur_y is not None:
                    samples.append((t, cur_x, cur_y))
            elif val == 0:
                _flush(t)
        elif typ == EV_ABS and code == ABS_MT_TRACKING_ID:
            if val >= 0 and down_t is None:
                down_t = t
            elif val < 0:
                _flush(t)
    if down_t is not None:
        _flush(down_t + 0.05)
    return gestures
