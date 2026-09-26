"""Run the plugin.js runtime load/render check under Node (F-16)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_js_runtime_load_check():
    if not shutil.which("node"):
        pytest.skip("node not available")
    r = subprocess.run(
        ["node", str(ROOT / "tests" / "js_stubs" / "run_check.mjs")],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0 and "PLUGIN_JS_LOAD_CHECK_OK" in r.stdout, \
        f"plugin.js runtime check failed: {r.stdout}\n{r.stderr}"
