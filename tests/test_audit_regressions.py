"""Audit regressions F-01..F-12 — behavioral repros of the 2026-09-26 audit.

These tests are written against BOTH the pre-fix tree (where they are RED) and
the remediated tree (GREEN). Every subprocess is mocked; nothing touches a
device. Mapping to the closure matrix: F-01..F-12 each have at least one test.
"""

import importlib
import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT, load_module, make_avd


# ── F-01: every adb path pins the emulator serial ─────────────────────────

def test_f01_adb_shell_pins_serial(agent_tools, agent_mod):
    tools, calls = agent_tools
    tools["emu_shell"]({"command": "getprop ro.product.model"})
    argv = calls[-1]["argv"]
    assert "-s" in argv, f"serial not pinned: {argv}"
    assert agent_mod._EMU_SERIAL in argv
    assert argv[argv.index("-s") + 1] == agent_mod._EMU_SERIAL


def test_f01_install_pins_serial(agent_tools, agent_mod, tmp_path):
    tools, calls = agent_tools
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"dex")
    tools["emu_install"]({"apk_path": str(apk)})
    argv = calls[-1]["argv"]
    assert "-s" in argv, f"install not serial-pinned: {argv}"
    assert argv[argv.index("-s") + 1] == agent_mod._EMU_SERIAL


def test_f01_every_adb_helper_pins_serial(agent_mod):
    calls = []

    def fake_run(cmd, timeout=30):
        calls.append(list(cmd))
        return "", "", 0

    orig = agent_mod._run
    agent_mod._run = fake_run
    try:
        agent_mod._adb("devices")
        agent_mod._adb_shell("echo", "hi")
    finally:
        agent_mod._run = orig
    for argv in calls:
        assert "-s" in argv, f"unpinned adb call: {argv}"


def test_f01_api_device_online_uses_configured_serial(api_mod, monkeypatch):
    calls = []

    def fake_run(cmd, timeout=10):
        calls.append(list(cmd))
        return (f"List of devices attached\n{api_mod._EMU_SERIAL}\tdevice\n").encode(), 0

    monkeypatch.setattr(api_mod, "_run", fake_run)
    monkeypatch.setattr(api_mod, "_cache", {"ts": 0.0, "data": None,
                                            "online_ts": 0.0, "online": False})
    assert api_mod._device_online() is True
    assert api_mod._EMU_SERIAL == "emulator-5554" or api_mod._SERIAL_OVERRIDDEN


def test_f01_guard_refuses_physical_serial_without_optin(monkeypatch, tmp_path):
    """Non-emulator serial must be refused unless deliberately opted in."""
    monkeypatch.setenv("ANDROID_EMULATOR_SERIAL", "0123456789ABCDEF")
    monkeypatch.delenv("ANDROID_EMULATOR_ALLOW_NON_EMULATOR", raising=False)
    mod = load_module("ae_guard_test", ROOT / "__init__.py")
    calls = []
    monkeypatch.setattr(mod, "_run", lambda cmd, timeout=30: (calls.append(list(cmd)) or ("", "", 0)))
    res = json.loads(mod._guarded(lambda p: mod._handle_type({"text": "hi"}))({}))
    assert res["success"] is False
    assert res.get("code") == "refused_serial"
    assert calls == [], "refused call must not execute anything"


def test_f01_guard_allows_explicit_optin(monkeypatch):
    monkeypatch.setenv("ANDROID_EMULATOR_SERIAL", "0123456789ABCDEF")
    monkeypatch.setenv("ANDROID_EMULATOR_ALLOW_NON_EMULATOR", "1")
    mod = load_module("ae_guard_allow_test", ROOT / "__init__.py")
    calls = []
    monkeypatch.setattr(mod, "_run", lambda cmd, timeout=30: (calls.append(list(cmd)) or ("", "", 0)))
    res = json.loads(mod._guarded(lambda p: mod._handle_type({"text": "hi"}))({}))
    assert res["success"] is True
    assert calls, "explicit opt-in must execute"


# ── F-02: /avd/wipe path traversal ────────────────────────────────────────

def test_f02_wipe_traversal_refused(client, api_env):
    victim = api_env["avd_root"].parent / "victim-avd.avd"
    victim.mkdir()
    precious = victim / "userdata.img"
    precious.write_text("precious user data")
    r = client.post("/api/plugins/android-emulator/avd/wipe",
                    params={"name": "../../victim-avd"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False, f"traversal must be refused: {body}"
    assert precious.exists(), "fixture outside the AVD root must survive"


def test_f02_wipe_requires_confirm_and_known_avd(client, api_env):
    avd_dir = make_avd(api_env, "legit")
    (avd_dir / "userdata.img").write_text("data")
    (avd_dir / "userdata-qemu.img").write_text("data")
    (avd_dir / "keepme.img").write_text("keep")

    r = client.post("/api/plugins/android-emulator/avd/wipe", params={"name": "legit"})
    assert r.json().get("ok") is False, "wipe without confirm must be refused"
    assert (avd_dir / "userdata.img").exists()

    r = client.post("/api/plugins/android-emulator/avd/wipe",
                    params={"name": "nope", "confirm": "nope"})
    assert r.json().get("ok") is False, "unknown AVD must be refused"

    r = client.post("/api/plugins/android-emulator/avd/wipe",
                    params={"name": "legit", "confirm": "legit"})
    body = r.json()
    assert body.get("ok") is True, body
    assert not (avd_dir / "userdata.img").exists()
    assert not (avd_dir / "userdata-qemu.img").exists()
    assert (avd_dir / "keepme.img").exists(), "non-userdata files must survive"


# ── F-03: destructive lifecycle guards; narrow stop ───────────────────────

def test_f03_stop_never_pkills(client, api_mod, api_calls):
    r = client.post("/api/plugins/android-emulator/stop")
    assert r.status_code == 200
    argvs = [c["argv"] for c in api_calls]
    assert argvs, "stop must do something (adb emu kill)"
    for argv in argvs:
        assert "pkill" not in argv, f"broad pkill is forbidden: {argv}"
    assert any("emu" in argv and "kill" in argv for argv in argvs)


def test_f03_create_refuses_overwrite_without_flag(client, api_env, api_mod, api_calls):
    make_avd(api_env, "existing")
    r = client.post("/api/plugins/android-emulator/create",
                    params={"name": "existing", "device": "pixel_6", "api": "34"})
    body = r.json()
    assert body.get("ok") is False and body.get("code") == "avd_exists"
    for c in api_calls:
        assert "--force" not in c["argv"], "no --force without overwrite=true"


def test_f03_create_force_only_with_overwrite(client, api_env, api_mod, api_calls):
    make_avd(api_env, "existing")
    r = client.post("/api/plugins/android-emulator/create",
                    params={"name": "existing", "device": "pixel_6", "api": "34",
                            "overwrite": "true"})
    create_calls = [c for c in api_calls if "create" in c["argv"]]
    assert create_calls, r.json()
    assert "--force" in create_calls[-1]["argv"]


def test_f03_delete_requires_confirm(client, api_env, api_mod, api_calls):
    make_avd(api_env, "doomed")
    r = client.post("/api/plugins/android-emulator/avd/delete", params={"name": "doomed"})
    assert r.json().get("code") == "confirm_required"
    assert api_calls == [] or all("delete" not in c["argv"] for c in api_calls)


# ── F-04: no state-changing GET routes ────────────────────────────────────

def test_f04_input_routes_reject_get(client):
    r1 = client.get("/api/plugins/android-emulator/input/tap/1/2")
    r2 = client.get("/api/plugins/android-emulator/input/key/HOME")
    assert r1.status_code == 405, f"tap must not accept GET: {r1.status_code}"
    assert r2.status_code == 405, f"key must not accept GET: {r2.status_code}"


def test_f04_mutating_routes_are_post_only(api_mod):
    mutating_paths = ["/input/tap/{x}/{y}", "/input/key/{keycode}", "/type",
                      "/apps/launch", "/apps/uninstall", "/shell", "/record/start",
                      "/record/stop", "/stop", "/start", "/avd/wipe", "/avd/delete",
                      "/create", "/notification", "/test/run"]
    for route in api_mod.router.routes:
        path = getattr(route, "path", "")
        if path in mutating_paths:
            assert "GET" not in getattr(route, "methods", set()), \
                f"state-changing GET: {path}"


# ── F-05: device-side injection via unquoted params ───────────────────────

def test_f05_type_text_is_escaped(agent_tools, agent_mod):
    tools, calls = agent_tools
    tools["emu_type"]({"text": "hello; reboot"})
    argv = calls[-1]["argv"]
    text_arg = argv[-1]
    assert text_arg.startswith("'") and text_arg.endswith("'"), \
        f"text must be shell-quoted: {text_arg}"
    assert ";" not in text_arg.replace("';'", "") or "'" in text_arg


def test_f05_api_type_text_is_escaped(client, api_calls):
    r = client.post("/api/plugins/android-emulator/type", params={"text": "a; rm -rf /"})
    assert r.json().get("ok") is True
    argv = api_calls[-1]["argv"]
    assert argv[-1].startswith("'"), f"unescaped text arg: {argv[-1]}"


def test_f05_test_run_package_injection_rejected(client, api_calls):
    r = client.post("/api/plugins/android-emulator/test/run",
                    params={"package": "com.x;reboot.test"})
    body = r.json()
    assert body.get("ok") is False, body
    assert api_calls == [] or all("instrument" not in c["argv"] for c in api_calls)


def test_f05_keycode_injection_rejected(agent_tools):
    tools, _ = agent_tools
    res = json.loads(tools["emu_key"]({"keycode": "HOME; rm -rf /sdcard/*"}))
    assert res["success"] is False


def test_f05_packages_filter_injection_rejected(agent_tools):
    tools, _ = agent_tools
    res = json.loads(tools["emu_packages"]({"filter": "x; am force-stop com.victim"}))
    assert res["success"] is False


def test_f05_deeplink_url_validated(agent_tools):
    tools, _ = agent_tools
    res = json.loads(tools["emu_deeplink"]({"url": "javascript:alert(1)"}))
    assert res["success"] is False
    res = json.loads(tools["emu_deeplink"]({"url": "myapp://path/ok"}))
    assert res["success"] is True


# ── F-05 addendum (live-found 2026-09-26): the raw device shell must survive
# adb's wire semantics. adb joins argv with spaces and the device shell then
# re-tokenizes the joined string, so an unquoted `sh -c <cmd>` ran only the
# FIRST WORD of any multi-word command (live repro: POST /shell?command=echo
# smoke-ok -> empty stdout, exit 0). The script must be quoted so it reaches
# `sh -c` as one device-side argument.

def _wire_sh_c_script(argv):
    """Model the device-side parse: argv after `shell` is joined with spaces,
    then tokenized by the device shell; return what `sh -c` actually runs."""
    tail = list(argv)[list(argv).index("shell") + 1:]
    tokens = shlex.split(" ".join(str(a) for a in tail))
    return tokens[tokens.index("-c") + 1]


def test_f05_api_shell_multiword_survives_wire(client, api_calls):
    r = client.post("/api/plugins/android-emulator/shell",
                    params={"command": "echo hello world"})
    assert r.json().get("exit_code") == 0
    assert _wire_sh_c_script(api_calls[-1]["argv"]) == "echo hello world"


def test_f05_api_shell_quoting_survives_wire(client, api_calls):
    cmd = "printf '%s' \"a b\" | tr a-z A-Z"
    client.post("/api/plugins/android-emulator/shell", params={"command": cmd})
    assert _wire_sh_c_script(api_calls[-1]["argv"]) == cmd


def test_f05_agent_shell_multiword_survives_wire(agent_tools):
    tools, calls = agent_tools
    res = json.loads(tools["emu_shell"]({"command": "echo hello world"}))
    assert res["success"] is True
    assert _wire_sh_c_script(calls[-1]["argv"]) == "echo hello world"


# ── F-06: unvalidated inputs crash handlers ───────────────────────────────

def test_f06_timeout_bad_type_returns_structured_error(agent_tools):
    tools, _ = agent_tools
    res = json.loads(tools["emu_shell"]({"command": "id", "timeout_seconds": "abc"}))
    assert res["success"] is False
    assert "error" in res


def test_f06_timeout_zero_and_negative_clamped(agent_tools, agent_mod):
    tools, calls = agent_tools
    for bad in (-1, 0, 10**9):
        res = json.loads(tools["emu_shell"]({"command": "id", "timeout_seconds": bad}))
        assert res["success"] is True, (bad, res)
        assert agent_mod.TIMEOUT_MIN <= calls[-1]["timeout"] <= agent_mod.TIMEOUT_MAX


def test_f06_missing_tap_coords_structured_error(agent_tools):
    tools, _ = agent_tools
    res = json.loads(tools["emu_tap"]({}))
    assert res["success"] is False
    assert res.get("code") == "invalid_param"


def test_f06_logcat_lines_bad_type_structured_error(agent_tools):
    tools, _ = agent_tools
    res = json.loads(tools["emu_logcat"]({"lines": "50; id"}))
    assert res["success"] is False


def test_f06_screenshot_label_traversal_no_500(client, api_env, api_calls):
    api_calls.append({"argv": [b"exec-out", b"screencap", b"-p"], "timeout": 5})
    r = client.post("/api/plugins/android-emulator/screenshot/save",
                    params={"label": "../../pwned"})
    assert r.status_code == 200, "must not raise/500"
    body = r.json()
    assert body.get("ok") is False, body
    assert not (api_env["shots"].parent / "pwned.png").exists()


# ── F-07: async handlers must not block the dashboard event loop ──────────

def test_f07_handlers_are_sync_threadpool_functions(api_mod):
    import asyncio

    for route in api_mod.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        assert not asyncio.iscoroutinefunction(endpoint), \
            f"blocking endpoint on the event loop: {getattr(route, 'path', '?')}"


def test_f07_slow_request_does_not_stall_others(api_mod, api_env, api_calls, monkeypatch):
    import asyncio

    import httpx

    def slow_run(cmd, timeout=10):
        if "instrument" in cmd:
            time.sleep(1.2)
        return b"ok", 0

    monkeypatch.setattr(api_mod, "_run", slow_run)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(api_mod.router, prefix="/api/plugins/android-emulator")

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            start = time.time()
            slow = asyncio.create_task(
                c.post("/api/plugins/android-emulator/test/run",
                       params={"package": "com.example.test"}))
            await asyncio.sleep(0.1)
            fast = await c.get("/api/plugins/android-emulator/status")
            fast_done = time.time() - start
            await slow
            return fast_done, fast.status_code

    fast_done, status = asyncio.run(main())
    assert status == 200
    assert fast_done < 1.0, f"/status stalled behind /test/run: {fast_done:.2f}s"


# ── F-08: recording lifecycle is truthful; replay is usable ───────────────

def test_f08_record_start_detached_and_stop_truthful(client, api_mod, api_env,
                                                     api_calls, monkeypatch):
    monkeypatch.setattr(api_mod, "_device_online", lambda: True)
    procs = []

    class FakeProc:
        def __init__(self, argv, **kw):
            self.argv = argv
            self.pid = 4242
            procs.append(self)

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(api_mod.subprocess, "Popen", FakeProc)

    r = client.post("/api/plugins/android-emulator/record/start")
    body = r.json()
    assert body.get("ok") is True, body
    assert procs, "screenrecord must be spawned detached (Popen), not awaited"
    assert "screenrecord" in procs[-1].argv
    assert "--time-limit" in procs[-1].argv

    # Second start must refuse (no double-launch).
    r2 = client.post("/api/plugins/android-emulator/record/start")
    assert r2.json().get("ok") is False

    # Stop with pull failing -> must NOT claim success.
    def fail_pull(cmd, timeout=10):
        api_calls.append({"argv": list(cmd), "timeout": timeout})
        return b"", 1

    monkeypatch.setattr(api_mod, "_run", fail_pull)
    r3 = client.post("/api/plugins/android-emulator/record/stop")
    assert r3.json().get("ok") is False, "failed pull must not report success"

    # Now a working pull -> success with size.
    def ok_pull(cmd, timeout=10):
        api_calls.append({"argv": list(cmd), "timeout": timeout})
        if "pull" in cmd:
            dest = cmd[-1]
            Path(dest).write_bytes(b"mp4data")
        return b"ok", 0

    monkeypatch.setattr(api_mod, "_run", ok_pull)
    client.post("/api/plugins/android-emulator/record/start")
    r4 = client.post("/api/plugins/android-emulator/record/stop")
    body4 = r4.json()
    assert body4.get("ok") is True, body4
    assert body4.get("size", 0) > 0


def test_f08_record_stop_without_recording_is_error(client):
    r = client.post("/api/plugins/android-emulator/record/stop")
    assert r.json().get("ok") is False


# ── F-09: notification reports the real result ────────────────────────────

def test_f09_notification_reports_failure_truthfully(client, api_mod, monkeypatch):
    monkeypatch.setattr(api_mod, "_run", lambda cmd, timeout=10: (b"error: unknown", 1))
    r = client.post("/api/plugins/android-emulator/notification",
                    params={"title": "t", "body": "b"})
    body = r.json()
    assert body.get("ok") is False, f"rc=1 must not report success: {body}"
    assert "error" in body


def test_f09_notification_success_when_delivered(client, api_mod, monkeypatch):
    monkeypatch.setattr(api_mod, "_run", lambda cmd, timeout=10: (b"", 0))
    r = client.post("/api/plugins/android-emulator/notification",
                    params={"title": "t", "body": "b"})
    assert r.json().get("ok") is True


# ── F-10: resource limits ─────────────────────────────────────────────────

def test_f10_logcat_lines_clamped(client, api_mod, api_calls):
    r = client.get("/api/plugins/android-emulator/logcat", params={"lines": 99999999})
    assert r.status_code == 200
    argv = api_calls[-1]["argv"]
    assert str(api_mod.LINES_MAX) in argv, f"lines not clamped: {argv}"


def test_f10_agent_logcat_lines_clamped(agent_tools, agent_mod):
    tools, calls = agent_tools
    tools["emu_logcat"]({"lines": 99999999})
    argv = calls[-1]["argv"]
    assert str(agent_mod.LINES_MAX) in argv


def test_f10_gallery_limit_clamped(client, api_env, api_calls):
    r = client.get("/api/plugins/android-emulator/screenshot/gallery",
                   params={"limit": 99999999})
    assert r.status_code == 200
    assert isinstance(r.json().get("screenshots"), list)


# ── F-11: filesystem traversal / host-write confinement ───────────────────

def test_f11_screenshot_file_traversal_is_404(client, api_env):
    (api_env["shots"] / "real.png").write_bytes(b"png")
    # encoded traversal attempts must 404
    r = client.get("/api/plugins/android-emulator/screenshot/file/..%2F..%2Fsecret.txt")
    assert r.status_code == 404
    r = client.get("/api/plugins/android-emulator/screenshot/file/%2e%2e%2fsecret")
    assert r.status_code == 404
    # a symlink planted inside the dir must not escape the containment check
    secret = api_env["shots"].parent / "outside.png"
    secret.write_bytes(b"secret")
    (api_env["shots"] / "link.png").symlink_to(secret)
    r = client.get("/api/plugins/android-emulator/screenshot/file/link.png")
    assert r.status_code == 404, "symlink escape must be refused"
    assert r.content != b"secret"


def test_f11_agent_pull_confined_and_no_overwrite(agent_tools, tmp_path, monkeypatch):
    tools, _ = agent_tools
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    res = json.loads(tools["emu_pull"]({"remote_path": "/sdcard/x",
                                        "local_path": "/etc/pwned"}))
    assert res["success"] is False, f"write outside allowed roots must fail: {res}"

    target = tmp_path / "home" / "out.bin"
    target.write_text("old")
    res = json.loads(tools["emu_pull"]({"remote_path": "/sdcard/x",
                                        "local_path": str(target)}))
    assert res["success"] is False, "silent overwrite must be refused"

    res = json.loads(tools["emu_pull"]({"remote_path": "/sdcard/x",
                                        "local_path": str(target),
                                        "overwrite": True}))
    assert res["success"] is True, res


# ── F-12: real device picker identity and switching ───────────────────────

def test_f12_picker_reports_configured_active_avd(client, api_mod, api_calls):
    r = client.get("/api/plugins/android-emulator/picker")
    body = r.json()
    assert body.get("active_avd") == api_mod._ACTIVE_AVD
    assert "running" in body


def test_f12_picker_switch_validates_name(client, api_env):
    r = client.post("/api/plugins/android-emulator/picker/switch",
                    params={"name": "../../evil"})
    assert r.json().get("ok") is False
    r = client.post("/api/plugins/android-emulator/picker/switch",
                    params={"name": "nonexistent-avd"})
    assert r.json().get("ok") is False


def test_f12_swipe_uses_screen_size_not_hardcode(client, api_mod, api_calls, monkeypatch):
    monkeypatch.setattr(api_mod, "_screen_size", lambda: (720, 1280))
    r = client.post("/api/plugins/android-emulator/swipe/up")
    assert r.json().get("ok") is True
    argv = api_calls[-1]["argv"]
    assert "360" in argv, f"swipe should use computed center: {argv}"
