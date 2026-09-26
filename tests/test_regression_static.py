"""Static/structural regression tests (replaces upstream's line-number asserts).

Line-number asserts from upstream c894f0f were brittle; these assert behaviour
and structure instead. Covers F-01 (serial pinning structure), F-13 (tool and
route inventory) and F-15 (test coverage itself).
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "__init__.py"
API_SRC = ROOT / "dashboard" / "plugin_api.py"
JS_SRC = ROOT / "dashboard" / "plugin.js"

EXPECTED_TOOLS = [
    "emu_status",
    "emu_shell",
    "emu_install",
    "emu_uninstall",
    "emu_screenshot",
    "emu_tap",
    "emu_swipe",
    "emu_type",
    "emu_key",
    "emu_launch",
    "emu_packages",
    "emu_push",
    "emu_pull",
    "emu_logcat",
    # README-promised tools added in remediation
    "emu_gps",
    "emu_battery",
    "emu_network",
    "emu_deeplink",
]


def _register_tool_names():
    tree = ast.parse(SRC.read_text(encoding="utf-8"), filename=str(SRC))
    names = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_tool"
        ):
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.append(kw.value.value)
    return names


def test_plugin_registers_expected_tools():
    names = _register_tool_names()
    assert sorted(names) == sorted(EXPECTED_TOOLS), f"tool inventory drift: {names}"


def test_every_adb_helper_pins_serial_structurally():
    """No helper may build an adb argv without -s <serial> (F-01)."""
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(SRC))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("_adb", "_adb_shell"):
            body_src = ast.unparse(node)
            assert ("'-s'" in body_src or '"-s"' in body_src) and "_EMU_SERIAL" in body_src, \
                f"{node.name} does not pin the serial: {body_src}"


def test_api_serial_guard_present():
    src = API_SRC.read_text(encoding="utf-8")
    assert "_require_emulator" in src
    assert src.count("_require_emulator()") >= 20, "guard must cover the route surface"


def test_no_state_changing_get_routes_registered():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("ae_static_api", str(API_SRC))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ae_static_api"] = mod
    spec.loader.exec_module(mod)

    read_only = {"/status", "/screenshot", "/screenshot_b64", "/logcat", "/apps",
                 "/screenshot/gallery", "/screenshot/file/{filename}", "/shortcuts",
                 "/picker"}
    mutating = set()
    for route in mod.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if "GET" in methods:
            assert route.path in read_only, f"unexpected GET route: {route.path}"
        else:
            mutating.add(route.path)
    # the audit's CSRF offenders must be POST now
    assert "/input/tap/{x}/{y}" in mutating
    assert "/input/key/{keycode}" in mutating


def test_route_inventory_documented():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("ae_static_api2", str(API_SRC))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ae_static_api2"] = mod
    spec.loader.exec_module(mod)
    paths = sorted({r.path for r in mod.router.routes if getattr(r, "path", "")})
    assert len(paths) == 41, f"route count changed ({len(paths)}): update README + this test"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "41" in readme, "README must document the real route count"


def test_plugin_js_uses_post_for_input_routes():
    js = JS_SRC.read_text(encoding="utf-8")
    assert re.search(r"input/key/\$\{key\}.*method: 'POST'", js, re.S), \
        "sendKey must POST (F-04)"
    assert re.search(r"input/tap/.*method: 'POST'", js, re.S), \
        "sendTap must POST (F-04)"


def test_plugin_js_has_shortcut_handler():
    js = JS_SRC.read_text(encoding="utf-8")
    assert "addEventListener('keydown'" in js, "keyboard shortcuts must exist (F-13)"


def test_plugin_js_imports_stay_within_sdk_allowlist():
    js = JS_SRC.read_text(encoding="utf-8")
    for m in re.finditer(r"from\s+'([^']+)'", js):
        assert m.group(1) in ("@hermes/plugin-sdk", "react", "react/jsx-runtime"), \
            f"forbidden import: {m.group(1)}"


def test_versions_aligned():
    import json

    yaml = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    yv = re.search(r'version:\s*"([^"]+)"', yaml).group(1)
    assert yv == manifest["version"] == "1.1.0"


def test_never_throws_contract_decorator_present():
    src = API_SRC.read_text(encoding="utf-8")
    assert "@_safe" in src, "handlers must be wrapped in the never-throw envelope"
