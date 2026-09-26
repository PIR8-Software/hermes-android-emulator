"""Shared fixtures: load the plugin modules standalone and capture subprocess argv.

No test may ever talk to a device: every subprocess is mocked at `_run` /
`subprocess.Popen` level and the argv is what gets asserted.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def agent_mod():
    return load_module("ae_agent_init", ROOT / "__init__.py")


@pytest.fixture(scope="session")
def api_mod():
    return load_module("ae_plugin_api", ROOT / "dashboard" / "plugin_api.py")


class FakeCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset=None, schema=None, handler=None, **kw):
        self.tools[name] = handler


@pytest.fixture
def agent_tools(agent_mod, monkeypatch):
    """Registered agent-tool handlers with `_run` argv-captured."""
    calls = []

    def fake_run(cmd, timeout=30):
        calls.append({"argv": list(cmd), "timeout": timeout})
        return "ok", "", 0

    monkeypatch.setattr(agent_mod, "_run", fake_run)
    ctx = FakeCtx()
    agent_mod.register(ctx)
    return ctx.tools, calls


@pytest.fixture
def api_calls(api_mod, monkeypatch):
    """Plugin API `_run` argv-captured (bytes return like the real one)."""
    calls = []

    def fake_run(cmd, timeout=10):
        calls.append({"argv": list(cmd), "timeout": timeout})
        return b"ok", 0

    monkeypatch.setattr(api_mod, "_run", fake_run)
    return calls


@pytest.fixture
def api_env(api_mod, tmp_path, monkeypatch):
    """Sandboxed paths + no real adb/devices for the dashboard API module."""
    avd_root = tmp_path / "avd"
    avd_root.mkdir()
    shots = tmp_path / "shots"
    shots.mkdir()
    replay = tmp_path / "replay"
    replay.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(api_mod, "AVD_ROOT", avd_root)
    monkeypatch.setattr(api_mod, "SCREENSHOT_DIR", str(shots))
    monkeypatch.setattr(api_mod, "REPLAY_DIR", str(replay))
    monkeypatch.setattr(api_mod, "RECORD_LOG_DIR", str(logs))
    monkeypatch.setattr(api_mod, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(api_mod, "EMULATOR_BIN", str(tmp_path / "emulator"))
    Path(api_mod.EMULATOR_BIN).write_text("#!/bin/sh\n")
    monkeypatch.setattr(api_mod, "_device_online", lambda: False)
    api_mod._cache.update({"ts": 0.0, "data": None, "online_ts": 0.0, "online": False})
    api_mod._state.update({"emulator_pid": None, "emulator_avd": None,
                           "record_proc": None, "record_remote": None,
                           "replay_proc": None, "replay_file": None, "replay_node": None})
    return {"avd_root": avd_root, "shots": shots, "replay": replay}


@pytest.fixture
def client(api_mod, api_env, api_calls):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_mod.router, prefix="/api/plugins/android-emulator")
    return TestClient(app)


def make_avd(env, name):
    """Create a minimal AVD inventory entry under the sandboxed AVD root."""
    d = env["avd_root"] / f"{name}.avd"
    d.mkdir()
    (d / "config.ini").write_text("avd.ini.encoding=UTF-8\n")
    return d
