"""Hermes plugin — control a headless Android emulator via ADB.

Safety contract (post-2026-09-26 audit remediation):
- Every adb call is pinned to the emulator serial (`-s`). Commands refuse to run
  against a non-emulator serial unless the operator deliberately opts in with
  ANDROID_EMULATOR_ALLOW_NON_EMULATOR=1 (plus ANDROID_EMULATOR_SERIAL).
- All non-shell parameters are validated/escaped before reaching the device
  shell; `emu_shell` is the one intentional arbitrary-device-shell entry point.
- Handlers never raise: every call returns a JSON envelope
  ({"success": true|false, ...}).
- Host-side file writes are confined to $HOME, the cwd, or the system temp dir
  and never silently overwrite existing files.
"""

import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path

ADB = os.path.expanduser("~/Android/Sdk/platform-tools/adb")
_EMU_SERIAL = os.environ.get("ANDROID_EMULATOR_SERIAL", "emulator-5554")
# A non-emulator serial is only honoured with a deliberate second opt-in.
_SERIAL_OVERRIDDEN = bool(os.environ.get("ANDROID_EMULATOR_ALLOW_NON_EMULATOR"))
EMULATOR = os.path.expanduser("~/Android/Sdk/emulator/emulator")
AVDMANAGER = os.path.expanduser("~/Android/Sdk/cmdline-tools/latest/bin/avdmanager")

# ── Resource limits ─────────────────────────────────────────────────────────
TIMEOUT_MIN, TIMEOUT_MAX = 1, 600
LINES_MIN, LINES_MAX = 1, 5000
TEXT_MAX = 2000
COORD_MAX = 100000

# ── Input validation ────────────────────────────────────────────────────────
_RE_PACKAGE = re.compile(r"^[A-Za-z0-9._]+$")
_RE_ACTIVITY = re.compile(r"^[A-Za-z0-9._$]+$")
_RE_KEYCODE = re.compile(r"^[A-Z0-9_]{1,40}$")
_RE_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^\s;|&$`'\"()<>\\]+$")
_RE_REMOTE = re.compile(r"^/[A-Za-z0-9._\-/]+$")
_RE_LOGSPEC = re.compile(r"^(\*|[A-Za-z0-9_.\-]+):([VDIWEFS])$")
_RE_FILTER_WORD = re.compile(r"^[A-Za-z0-9._\-]{0,64}$")


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _int_param(params, name, default=None, lo=None, hi=None, required=False):
    """Coerce a parameter to int with bounds; raises ValueError for bad input."""
    value = params.get(name, default)
    if value is None or value == "":
        if required:
            raise ValueError(f"'{name}' is required")
        return default
    if isinstance(value, bool):
        raise ValueError(f"'{name}' must be an integer")
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer, got {value!r}")
    if lo is not None and hi is not None:
        iv = _clamp(iv, lo, hi)
    return iv


def _text_param(params, name, required=True, max_len=TEXT_MAX):
    value = params.get(name)
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
    """Escape free text for `adb shell input text` (device-shell-safe).

    `input text` maps %s to a space and adb joins argv with spaces, so spaces
    are pre-escaped as %s; shlex.quote neutralises shell metacharacters before
    the string reaches the device shell. Quirk: a literal '%s' typed by the user
    renders as a space.
    """
    return shlex.quote(text.replace(" ", "%s"))


def _safe_host_write_path(raw_path, default=None, overwrite=False):
    """Resolve a host write target and confine it to allowed roots.

    Allowed roots: $HOME, the current working directory, the system temp dir,
    or ANDROID_EMULATOR_OUTPUT_ROOT when set. Never silently overwrites.
    """
    if not raw_path:
        raw_path = default
    if not raw_path:
        raise ValueError("no output path given")
    p = Path(os.path.expanduser(str(raw_path)))
    if not p.is_absolute():
        p = Path.cwd() / p
    resolved = p.resolve()
    roots = [Path.home().resolve(), Path.cwd().resolve(),
             Path(tempfile.gettempdir()).resolve()]
    extra = os.environ.get("ANDROID_EMULATOR_OUTPUT_ROOT")
    if extra:
        roots.append(Path(os.path.expanduser(extra)).resolve())
    if not any(resolved.is_relative_to(r) for r in roots):
        raise ValueError(
            f"output path must be under $HOME, the cwd, or the temp dir: {resolved}"
        )
    if resolved.exists() and not overwrite:
        raise ValueError(f"refusing to overwrite existing file: {resolved} (pass overwrite=true)")
    if not resolved.parent.exists():
        raise ValueError(f"parent directory does not exist: {resolved.parent}")
    return str(resolved)


def _serial_guard():
    """Refuse to act on a non-emulator serial unless explicitly overridden."""
    if _SERIAL_OVERRIDDEN:
        return None
    if not _EMU_SERIAL.startswith("emulator"):
        return (
            f"refusing to run against serial {_EMU_SERIAL!r}: not an emulator. "
            "Set ANDROID_EMULATOR_SERIAL explicitly to override."
        )
    return None


def _run(cmd, timeout=30):
    """Run a command, return (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "Command timed out", -1
    except Exception as e:
        return "", f"{type(e).__name__}: {e}", -1


def _adb(*args, timeout=30):
    """Run an adb command targeting the emulator."""
    return _run([ADB, "-s", _EMU_SERIAL] + list(args), timeout=timeout)


def _adb_shell(*args, timeout=30):
    """Run adb shell <args> on the emulator (serial-pinned)."""
    return _run([ADB, "-s", _EMU_SERIAL, "shell"] + list(args), timeout=timeout)


def _ok(data):
    return json.dumps({"success": True, **data})


def _err(msg, code="error"):
    return json.dumps({"success": False, "error": str(msg), "code": code})


def _guarded(fn):
    """Wrap a handler: serial guard + never-raise error envelope."""

    def inner(params, **kw):
        try:
            guard = _serial_guard()
            if guard:
                return _err(guard, code="refused_serial")
            return fn(params if isinstance(params, dict) else {})
        except KeyError as e:
            return _err(f"missing required parameter: {e.args[0]}", code="invalid_param")
        except (TypeError, ValueError) as e:
            return _err(str(e), code="invalid_param")
        except Exception as e:  # pragma: no cover - safety net
            return _err(f"{type(e).__name__}: {e}", code="internal")

    return inner


def register(ctx):
    # ── emu_status ──────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_status",
        toolset="android_emulator",
        schema={
            "name": "emu_status",
            "description": "Check emulator status: device list, Android version, model, boot state, screen size, available storage.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=_guarded(lambda p: _handle_status()),
    )

    # ── emu_shell ───────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_shell",
        toolset="android_emulator",
        schema={
            "name": "emu_shell",
            "description": "Run an arbitrary adb shell command on the emulator (intentional raw device shell). Returns stdout/stderr/exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run (e.g. 'ls /sdcard', 'pm list packages', 'dumpsys battery')",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds, clamped to 1-600 (default 30)",
                    },
                },
                "required": ["command"],
            },
        },
        handler=_guarded(lambda p: _handle_shell(p)),
    )

    # ── emu_install ─────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_install",
        toolset="android_emulator",
        schema={
            "name": "emu_install",
            "description": "Install an APK file on the emulator. Pass the local file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "apk_path": {
                        "type": "string",
                        "description": "Absolute path to the APK file on the host",
                    },
                    "replace": {
                        "type": "boolean",
                        "description": "Replace existing app (default true)",
                    },
                    "grant_permissions": {
                        "type": "boolean",
                        "description": "Grant all runtime permissions (default true)",
                    },
                },
                "required": ["apk_path"],
            },
        },
        handler=_guarded(lambda p: _handle_install(p)),
    )

    # ── emu_uninstall ───────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_uninstall",
        toolset="android_emulator",
        schema={
            "name": "emu_uninstall",
            "description": "Uninstall an app by package name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "Package name (e.g. com.example.app)",
                    },
                },
                "required": ["package"],
            },
        },
        handler=_guarded(lambda p: _handle_uninstall(p)),
    )

    # ── emu_screenshot ──────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_screenshot",
        toolset="android_emulator",
        schema={
            "name": "emu_screenshot",
            "description": "Take a screenshot of the emulator display. Returns the local file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Where to save the PNG (default: /tmp/emu_screenshot.png). Must be under $HOME, the cwd, or the temp dir.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Allow replacing an existing file (default false)",
                    },
                },
                "required": [],
            },
        },
        handler=_guarded(lambda p: _handle_screenshot(p)),
    )

    # ── emu_tap ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_tap",
        toolset="android_emulator",
        schema={
            "name": "emu_tap",
            "description": "Tap the screen at (x, y) coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate"},
                    "y": {"type": "integer", "description": "Y coordinate"},
                },
                "required": ["x", "y"],
            },
        },
        handler=_guarded(lambda p: _handle_tap(p)),
    )

    # ── emu_swipe ───────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_swipe",
        toolset="android_emulator",
        schema={
            "name": "emu_swipe",
            "description": "Swipe from (x1,y1) to (x2,y2) over duration_ms milliseconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer", "description": "Start X"},
                    "y1": {"type": "integer", "description": "Start Y"},
                    "x2": {"type": "integer", "description": "End X"},
                    "y2": {"type": "integer", "description": "End Y"},
                    "duration_ms": {
                        "type": "integer",
                        "description": "Duration in ms, clamped to 1-10000 (default 300)",
                    },
                },
                "required": ["x1", "y1", "x2", "y2"],
            },
        },
        handler=_guarded(lambda p: _handle_swipe(p)),
    )

    # ── emu_type ────────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_type",
        toolset="android_emulator",
        schema={
            "name": "emu_type",
            "description": "Type text on the emulator (uses adb shell input text). Spaces and shell metacharacters are escaped automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to type (spaces are handled for you; a literal '%s' renders as a space)",
                    },
                },
                "required": ["text"],
            },
        },
        handler=_guarded(lambda p: _handle_type(p)),
    )

    # ── emu_key ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_key",
        toolset="android_emulator",
        schema={
            "name": "emu_key",
            "description": "Press a key event. Common codes: HOME, BACK, ENTER, DEL, POWER, VOLUME_UP, VOLUME_DOWN, TAB, ESCAPE, DPAD_UP/DOWN/LEFT/RIGHT, MENU.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keycode": {
                        "type": "string",
                        "description": "Android keycode name (e.g. KEYCODE_HOME, HOME, BACK, ENTER)",
                    },
                },
                "required": ["keycode"],
            },
        },
        handler=_guarded(lambda p: _handle_key(p)),
    )

    # ── emu_launch ──────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_launch",
        toolset="android_emulator",
        schema={
            "name": "emu_launch",
            "description": "Launch an app by package name, or open a URL in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "Package name to launch (e.g. com.android.chrome). If omitted, use url.",
                    },
                    "activity": {
                        "type": "string",
                        "description": "Specific activity (e.g. com.android.chrome.Main). Optional — uses default launcher if omitted.",
                    },
                    "url": {
                        "type": "string",
                        "description": "URL to open in browser (if package is omitted)",
                    },
                },
                "required": [],
            },
        },
        handler=_guarded(lambda p: _handle_launch(p)),
    )

    # ── emu_packages ────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_packages",
        toolset="android_emulator",
        schema={
            "name": "emu_packages",
            "description": "List installed packages. Optionally filter by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional package-name filter (e.g. 'google', 'com.example')",
                    },
                },
                "required": [],
            },
        },
        handler=_guarded(lambda p: _handle_packages(p)),
    )

    # ── emu_push ────────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_push",
        toolset="android_emulator",
        schema={
            "name": "emu_push",
            "description": "Push a file from the host to the emulator.",
            "parameters": {
                "type": "object",
                "properties": {
                    "local_path": {
                        "type": "string",
                        "description": "Local file path on the host",
                    },
                    "remote_path": {
                        "type": "string",
                        "description": "Destination path on the emulator (e.g. /sdcard/file.txt)",
                    },
                },
                "required": ["local_path", "remote_path"],
            },
        },
        handler=_guarded(lambda p: _handle_push(p)),
    )

    # ── emu_pull ────────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_pull",
        toolset="android_emulator",
        schema={
            "name": "emu_pull",
            "description": "Pull a file from the emulator to the host. The destination must be under $HOME, the cwd, or the temp dir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "remote_path": {
                        "type": "string",
                        "description": "Path on the emulator",
                    },
                    "local_path": {
                        "type": "string",
                        "description": "Destination path on the host",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Allow replacing an existing file (default false)",
                    },
                },
                "required": ["remote_path", "local_path"],
            },
        },
        handler=_guarded(lambda p: _handle_pull(p)),
    )

    # ── emu_logcat ──────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_logcat",
        toolset="android_emulator",
        schema={
            "name": "emu_logcat",
            "description": "Get recent logcat output. Optionally filter by tag/priority.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Logcat filter expression (e.g. 'ActivityManager:I *:S', '*:E').",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of lines to retrieve, clamped to 1-5000 (default 50)",
                    },
                },
                "required": [],
            },
        },
        handler=_guarded(lambda p: _handle_logcat(p)),
    )

    # ── emu_gps ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_gps",
        toolset="android_emulator",
        schema={
            "name": "emu_gps",
            "description": "Set a fake GPS location on the emulator (e.g. 'Set GPS to San Francisco'), or clear the override with clear=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude (-90..90). Omit when clear=true."},
                    "lng": {"type": "number", "description": "Longitude (-180..180). Omit when clear=true."},
                    "clear": {"type": "boolean", "description": "Clear the GPS override instead of setting it"},
                },
                "required": [],
            },
        },
        handler=_guarded(lambda p: _handle_gps(p)),
    )

    # ── emu_battery ─────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_battery",
        toolset="android_emulator",
        schema={
            "name": "emu_battery",
            "description": "Simulate battery state: set level 0-100 (e.g. 'Set battery to 25%'), unplug (draining), or reset to real values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "description": "Battery level 0-100"},
                    "unplug": {"type": "boolean", "description": "Simulate unplugged (draining)"},
                    "reset": {"type": "boolean", "description": "Reset to real battery values"},
                },
                "required": [],
            },
        },
        handler=_guarded(lambda p: _handle_battery(p)),
    )

    # ── emu_network ─────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_network",
        toolset="android_emulator",
        schema={
            "name": "emu_network",
            "description": "Simulate network conditions: offline, slow, or fast.",
            "parameters": {
                "type": "object",
                "properties": {
                    "condition": {
                        "type": "string",
                        "description": "One of: offline, slow, fast",
                    },
                },
                "required": ["condition"],
            },
        },
        handler=_guarded(lambda p: _handle_network(p)),
    )

    # ── emu_deeplink ────────────────────────────────────────────────────
    ctx.register_tool(
        name="emu_deeplink",
        toolset="android_emulator",
        schema={
            "name": "emu_deeplink",
            "description": "Open a deep link / URL scheme on the emulator (e.g. 'Open deep link myapp://path').",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL or deep link to open (e.g. myapp://path, https://example.com)",
                    },
                },
                "required": ["url"],
            },
        },
        handler=_guarded(lambda p: _handle_deeplink(p)),
    )


# ── Handlers ──────────────────────────────────────────────────────────────

def _handle_status():
    # Read-only device listing (not device-targeted); info is gathered only from
    # the pinned emulator serial, so a physical phone is never reported on.
    out, err, rc = _run([ADB, "devices", "-l"])
    if rc != 0:
        return _err(f"adb devices failed: {err}")

    devices = []
    for line in out.splitlines()[1:]:
        if line.strip() and "attached" not in line:
            devices.append(line.strip())

    tail = out.split(_EMU_SERIAL, 1)[-1].split("\n", 1)[0] if _EMU_SERIAL in out else ""
    connected = bool(tail) and "device" in tail
    if not connected:
        return _ok({"connected": False, "serial": _EMU_SERIAL, "devices": devices, "info": {}})

    # Gather device info (all pinned to the emulator serial)
    info = {}
    for key, cmd in [
        ("android_version", ["getprop", "ro.build.version.release"]),
        ("sdk", ["getprop", "ro.build.version.sdk"]),
        ("model", ["getprop", "ro.product.model"]),
        ("screen_size", ["wm", "size"]),
        ("screen_density", ["wm", "density"]),
        ("boot_completed", ["getprop", "sys.boot_completed"]),
    ]:
        o, _, _ = _adb_shell(*cmd)
        info[key] = o

    # Disk space
    o, _, _ = _adb_shell("df", "-h", "/data")
    info["disk"] = o

    return _ok({"connected": True, "serial": _EMU_SERIAL, "devices": devices, "info": info})


def _handle_shell(params):
    cmd = _text_param(params, "command", max_len=8000)
    timeout = _clamp(_int_param(params, "timeout_seconds", default=30), TIMEOUT_MIN, TIMEOUT_MAX)
    # Intentional arbitrary device shell — the one explicitly intended shell feature.
    # adb joins argv with spaces before the device shell re-tokenizes them, so the
    # script must be single-quoted to reach `sh -c` as ONE argument. Unquoted, any
    # multi-word command silently ran only its first word (live-found 2026-09-26;
    # regression: test_f05_agent_shell_multiword_survives_wire).
    out, err, rc = _adb_shell("sh", "-c", shlex.quote(cmd), timeout=timeout)
    return _ok({"stdout": out, "stderr": err, "exit_code": rc})


def _handle_install(params):
    apk = _text_param(params, "apk_path", max_len=4096)
    if not os.path.isfile(apk):
        return _err(f"APK not found: {apk}", code="invalid_param")
    if not apk.lower().endswith(".apk"):
        return _err(f"not an .apk file: {apk}", code="invalid_param")
    args = [ADB, "-s", _EMU_SERIAL, "install"]
    if params.get("replace", True):
        args.append("-r")
    if params.get("grant_permissions", True):
        args.append("-g")
    args.append(apk)
    out, err, rc = _run(args, timeout=120)
    return _ok({"stdout": out, "stderr": err, "exit_code": rc})


def _handle_uninstall(params):
    pkg = _text_param(params, "package", max_len=255)
    if not _RE_PACKAGE.match(pkg):
        return _err(f"invalid package name: {pkg}", code="invalid_param")
    out, err, rc = _adb("uninstall", pkg)
    return _ok({"stdout": out, "stderr": err, "exit_code": rc})


def _handle_screenshot(params):
    out_path = _safe_host_write_path(
        params.get("output_path"), "/tmp/emu_screenshot.png",
        overwrite=bool(params.get("overwrite", False)),
    )
    remote = f"/sdcard/hermes_screenshot_{uuid.uuid4().hex[:8]}.png"
    _adb_shell("screencap", "-p", remote)
    out, err, rc = _adb("pull", remote, out_path)
    _adb_shell("rm", "-f", remote)
    if rc != 0:
        return _err(f"Screenshot failed: {err}")
    return _ok({"path": out_path, "size_bytes": os.path.getsize(out_path) if os.path.exists(out_path) else 0})


def _handle_tap(params):
    x = _int_param(params, "x", required=True, lo=0, hi=COORD_MAX)
    y = _int_param(params, "y", required=True, lo=0, hi=COORD_MAX)
    out, err, rc = _adb_shell("input", "tap", str(x), str(y))
    return _ok({"tapped": [x, y], "exit_code": rc})


def _handle_swipe(params):
    x1 = _int_param(params, "x1", required=True, lo=0, hi=COORD_MAX)
    y1 = _int_param(params, "y1", required=True, lo=0, hi=COORD_MAX)
    x2 = _int_param(params, "x2", required=True, lo=0, hi=COORD_MAX)
    y2 = _int_param(params, "y2", required=True, lo=0, hi=COORD_MAX)
    duration = _clamp(_int_param(params, "duration_ms", default=300), 1, 10000)
    args = ["input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)]
    out, err, rc = _adb_shell(*args)
    return _ok({"swiped": True, "exit_code": rc})


def _handle_type(params):
    text = _text_param(params, "text")
    out, err, rc = _adb_shell("input", "text", _device_text(text))
    return _ok({"typed": text, "exit_code": rc})


def _handle_key(params):
    keycode = _text_param(params, "keycode", max_len=48).upper()
    # Normalize — accept both "HOME" and "KEYCODE_HOME"
    if not keycode.startswith("KEYCODE_"):
        keycode = f"KEYCODE_{keycode}"
    if not _RE_KEYCODE.match(keycode):
        return _err(f"invalid keycode: {keycode}", code="invalid_param")
    out, err, rc = _adb_shell("input", "keyevent", keycode)
    return _ok({"key": keycode, "exit_code": rc})


def _handle_launch(params):
    url = params.get("url")
    pkg = params.get("package")
    activity = params.get("activity")

    if url and not pkg:
        url = _text_param(params, "url", max_len=2048)
        if not _RE_URL.match(url):
            return _err(f"invalid url: {url}", code="invalid_param")
        out, err, rc = _adb_shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url)
        return _ok({"launched": url, "stdout": out, "stderr": err, "exit_code": rc})

    if pkg:
        pkg = _text_param(params, "package", max_len=255)
        if not _RE_PACKAGE.match(pkg):
            return _err(f"invalid package name: {pkg}", code="invalid_param")
        if activity:
            activity = _text_param(params, "activity", max_len=255)
            if not _RE_ACTIVITY.match(activity):
                return _err(f"invalid activity: {activity}", code="invalid_param")
        component = f"{pkg}/{activity}" if activity else pkg
        out, err, rc = _adb_shell("am", "start", "-n", component)
        return _ok({"launched": component, "stdout": out, "stderr": err, "exit_code": rc})

    return _err("Provide either 'package' or 'url'", code="invalid_param")


def _handle_packages(params):
    filt = params.get("filter", "")
    if filt:
        filt = _text_param(params, "filter", required=False, max_len=64)
        if not _RE_FILTER_WORD.match(filt):
            return _err(f"invalid filter: {filt}", code="invalid_param")
    cmd = ["pm", "list", "packages"]
    if filt:
        cmd.append(filt)
    out, err, rc = _adb_shell(*cmd)
    packages = [line.replace("package:", "").strip() for line in out.splitlines() if line.startswith("package:")]
    return _ok({"count": len(packages), "packages": packages})


def _handle_push(params):
    local = _text_param(params, "local_path", max_len=4096)
    remote = _text_param(params, "remote_path", max_len=512)
    if not _RE_REMOTE.match(remote):
        return _err(f"invalid remote path: {remote}", code="invalid_param")
    if not os.path.isfile(local):
        return _err(f"Local file not found: {local}", code="invalid_param")
    out, err, rc = _adb("push", local, remote)
    return _ok({"pushed": local, "to": remote, "stdout": out, "exit_code": rc})


def _handle_pull(params):
    remote = _text_param(params, "remote_path", max_len=512)
    if not _RE_REMOTE.match(remote):
        return _err(f"invalid remote path: {remote}", code="invalid_param")
    local = _safe_host_write_path(
        params.get("local_path"), overwrite=bool(params.get("overwrite", False)),
    )
    out, err, rc = _adb("pull", remote, local)
    return _ok({"pulled": remote, "to": local, "stdout": out, "exit_code": rc})


def _logcat_specs(filt):
    """Validate a logcat filter expression into safe argv specs."""
    if not filt:
        return []
    specs = []
    for part in filt.split():
        if not _RE_LOGSPEC.match(part):
            raise ValueError(f"invalid logcat filter spec: {part!r}")
        specs.append(part)
    return specs


def _handle_logcat(params):
    lines = _clamp(_int_param(params, "lines", default=50), LINES_MIN, LINES_MAX)
    filt = params.get("filter", "")
    specs = _logcat_specs(_text_param(params, "filter", required=False, max_len=256) if filt else "")
    args = ["logcat", "-d", "-t", str(lines)] + specs
    out, err, rc = _adb_shell(*args)
    return _ok({"lines": out.splitlines()[-lines:], "exit_code": rc})


def _handle_gps(params):
    if params.get("clear"):
        _adb("emu", "geo", "nmea", "$GPGGA,,,,,,0,,,,,,,,*66")
        return _ok({"cleared": True})
    lat = float(params.get("lat"))
    lng = float(params.get("lng"))
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lng <= 180.0:
        raise ValueError("lat must be -90..90 and lng -180..180")
    _adb("emu", "geo", "fix", f"{lng:.6f}", f"{lat:.6f}")
    return _ok({"lat": lat, "lng": lng})


def _handle_battery(params):
    if params.get("reset"):
        _adb_shell("dumpsys", "battery", "reset")
        return _ok({"reset": True})
    if params.get("unplug"):
        _adb_shell("dumpsys", "battery", "unplug")
        return _ok({"unplugged": True})
    level = _int_param(params, "level", required=True, lo=0, hi=100)
    _adb_shell("dumpsys", "battery", "set", "level", str(level))
    return _ok({"level": level})


def _handle_network(params):
    condition = _text_param(params, "condition", max_len=16)
    if condition not in ("offline", "slow", "fast"):
        raise ValueError(f"unknown condition: {condition}")
    if condition == "offline":
        _adb_shell("svc", "wifi", "disable")
        _adb_shell("svc", "data", "disable")
    elif condition == "slow":
        _adb_shell("svc", "wifi", "enable")
        _adb_shell("svc", "data", "enable")
        _adb_shell("tc", "qdisc", "add", "dev", "wlan0", "root", "netem", "delay", "500ms", "loss", "10%")
    else:
        _adb_shell("tc", "qdisc", "del", "dev", "wlan0", "root")
        _adb_shell("svc", "wifi", "enable")
        _adb_shell("svc", "data", "enable")
    return _ok({"condition": condition})


def _handle_deeplink(params):
    url = _text_param(params, "url", max_len=2048)
    if not _RE_URL.match(url):
        return _err(f"invalid url: {url}", code="invalid_param")
    out, err, rc = _adb_shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url)
    return _ok({"url": url, "stdout": out, "stderr": err, "exit_code": rc})
