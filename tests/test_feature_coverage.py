"""Feature-coverage tests: the gaps the audit found (README promises vs reality)."""

import json

import pytest

from conftest import make_avd


# ── Agent tools the README promised but did not exist (F-13) ──────────────

def test_agent_gps_tool_works(agent_tools):
    tools, calls = agent_tools
    res = json.loads(tools["emu_gps"]({"lat": 37.7749, "lng": -122.4194}))
    assert res["success"] is True, res
    argv = calls[-1]["argv"]
    assert "geo" in argv and "fix" in argv
    assert any(a.startswith("-") and len(a) > 2 for a in argv)  # formatted coords

    res = json.loads(tools["emu_gps"]({"clear": True}))
    assert res["success"] is True


def test_agent_gps_validates_ranges(agent_tools):
    tools, _ = agent_tools
    res = json.loads(tools["emu_gps"]({"lat": 500, "lng": 0}))
    assert res["success"] is False


def test_agent_battery_tool_works(agent_tools):
    tools, calls = agent_tools
    res = json.loads(tools["emu_battery"]({"level": 25}))
    assert res["success"] is True and res["level"] == 25
    assert "dumpsys" in calls[-1]["argv"]
    res = json.loads(tools["emu_battery"]({"unplug": True}))
    assert res["success"] is True
    res = json.loads(tools["emu_battery"]({"reset": True}))
    assert res["success"] is True


def test_agent_network_tool_works(agent_tools):
    tools, calls = agent_tools
    res = json.loads(tools["emu_network"]({"condition": "offline"}))
    assert res["success"] is True
    assert "svc" in calls[-1]["argv"]
    res = json.loads(tools["emu_network"]({"condition": "banana"}))
    assert res["success"] is False


# ── Dashboard features the README advertised (F-13) ───────────────────────

def test_apps_install_route_exists_and_validates(client, api_env, api_calls):
    r = client.post("/api/plugins/android-emulator/apps/install",
                    params={"apk_path": "/nonexistent/app.apk"})
    body = r.json()
    assert body.get("ok") is False
    assert "install" not in [a for c in api_calls for a in c["argv"]]


def test_apps_install_works_with_real_file(client, api_env, api_calls, tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"dex")
    r = client.post("/api/plugins/android-emulator/apps/install",
                    params={"apk_path": str(apk)})
    body = r.json()
    assert body.get("ok") is True, body
    assert any("install" in c["argv"] for c in api_calls)


def test_apps_uninstall_validates_package(client, api_calls):
    r = client.post("/api/plugins/android-emulator/apps/uninstall",
                    params={"package": "com.x;reboot"})
    assert r.json().get("ok") is False
    assert all("uninstall" not in c["argv"] for c in api_calls)


def test_picker_switch_stops_and_starts(client, api_env, api_mod, api_calls, monkeypatch):
    make_avd(api_env, "hermes-test")
    monkeypatch.setattr(api_mod, "EMULATOR_BIN", str(api_env["avd_root"].parent / "emulator"))
    started = []

    class FakeProc:
        pid = 777

        def __init__(self, argv, **kw):
            started.append(argv)

        def poll(self):
            return None

    monkeypatch.setattr(api_mod.subprocess, "Popen", FakeProc)
    r = client.post("/api/plugins/android-emulator/picker/switch",
                    params={"name": "hermes-test"})
    body = r.json()
    assert body.get("ok") is True, body
    assert body.get("active_avd") == "hermes-test"
    assert started and "-avd" in started[-1]
    assert started[-1][started[-1].index("-avd") + 1] == "hermes-test"
    assert api_mod._ACTIVE_AVD == "hermes-test"
    # restore for other tests
    api_mod._ACTIVE_AVD = "pixel7pro"


def test_shortcuts_endpoint_lists_real_handler_actions(client):
    r = client.get("/api/plugins/android-emulator/shortcuts")
    actions = {s["action"] for s in r.json()["shortcuts"]}
    assert {"screenshot", "record", "home", "back", "logcat", "gallery"} <= actions
    js = open("dashboard/plugin.js", encoding="utf-8").read()
    for needle in ("saveScreenshot()", "toggleRecording()", "sendKey('HOME')",
                   "sendKey('BACK')", "fetchLogcat()", "fetchGallery()"):
        assert needle in js, f"shortcut action not implemented in UI: {needle}"


def test_replay_endpoints_lifecycle(client, api_env, api_mod, api_calls, monkeypatch):
    # no capture in progress -> structured errors
    r = client.post("/api/plugins/android-emulator/replay/record/stop")
    assert r.json().get("ok") is False

    monkeypatch.setattr(api_mod, "_device_online", lambda: True)

    def fake_find():
        return "/dev/input/event2", {"x": [0, 1023], "y": [0, 2047]}

    monkeypatch.setattr(api_mod, "_find_touch_device", fake_find)

    class FakeProc:
        def __init__(self, argv, **kw):
            self.argv = argv
            kw.get("stdout").write(b"[   1.0] 0003 0035 00000200\n")

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(api_mod.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(api_mod, "_screen_size", lambda: (1080, 2400))

    r = client.post("/api/plugins/android-emulator/replay/record/start")
    body = r.json()
    assert body.get("ok") is True, body

    r = client.post("/api/plugins/android-emulator/replay/record/stop")
    body = r.json()
    assert body.get("ok") is True, body
    assert "gestures" in body
    assert body["path"].endswith(".json")


def test_gps_clear_and_battery_unplug_routes_reachable(client, api_calls):
    # previously shadowed by /gps/{lat}/{lng} and /battery/{level} -> 422
    r1 = client.post("/api/plugins/android-emulator/gps/clear")
    r2 = client.post("/api/plugins/android-emulator/battery/unplug")
    assert r1.status_code == 200 and r1.json().get("ok") is True, r1.text
    assert r2.status_code == 200 and r2.json().get("ok") is True, r2.text
