"""Touch capture parsing (F-08 record/replay) — fixtures are synthetic getevent text."""

from conftest import load_module, ROOT
import pytest


@pytest.fixture(scope="session")
def parser_mod():
    return load_module("ae_plugin_api_parser", ROOT / "dashboard" / "plugin_api.py")


TAP_CAPTURE = """\
[   100.000001] 0003 0039 0000000a
[   100.000002] 0003 0035 00000200
[   100.000003] 0003 0036 00000400
[   100.000004] 0001 014a 00000001
[   100.000005] 0000 0000 00000000
[   100.050000] 0001 014a 00000000
[   100.050001] 0003 0039 ffffffff
[   100.050002] 0000 0000 00000000
"""

SWIPE_CAPTURE = """\
[   200.000001] 0003 0039 0000000b
[   200.000002] 0003 0035 00000064
[   200.000003] 0003 0036 00000064
[   200.000004] 0001 014a 00000001
[   200.000005] 0000 0000 00000000
[   200.200000] 0003 0035 0000012c
[   200.200001] 0003 0036 00000258
[   200.200002] 0000 0000 00000000
[   200.400000] 0001 014a 00000000
[   200.400001] 0003 0039 ffffffff
"""

RANGES = {"x": [0, 1023], "y": [0, 2047]}
DISPLAY = (1080, 2400)


def test_parse_tap(parser_mod):
    gestures = parser_mod.parse_getevent(TAP_CAPTURE, RANGES, DISPLAY)
    assert len(gestures) == 1
    g = gestures[0]
    assert g["type"] == "tap"
    # raw (512,1024) of (0..1023, 0..2047) scales to ~(540, 1200) on 1080x2400
    assert abs(g["x"] - 540) <= 2
    assert abs(g["y"] - 1200) <= 2


def test_parse_swipe(parser_mod):
    gestures = parser_mod.parse_getevent(SWIPE_CAPTURE, RANGES, DISPLAY)
    assert len(gestures) == 1
    g = gestures[0]
    assert g["type"] == "swipe"
    assert abs(g["x1"] - 105) <= 3 and abs(g["y1"] - 117) <= 3
    assert abs(g["x2"] - 316) <= 3 and abs(g["y2"] - 703) <= 3
    assert 300 <= g["duration_ms"] <= 500


def test_parse_mixed_sequence(parser_mod):
    text = TAP_CAPTURE + SWIPE_CAPTURE
    gestures = parser_mod.parse_getevent(text, RANGES, DISPLAY)
    assert [g["type"] for g in gestures] == ["tap", "swipe"]


def test_parse_empty_and_noise(parser_mod):
    assert parser_mod.parse_getevent("", RANGES, DISPLAY) == []
    assert parser_mod.parse_getevent("garbage\nnot a line\n", RANGES, DISPLAY) == []


def test_parse_unterminated_stream_flushes(parser_mod):
    text = TAP_CAPTURE.split("[   100.050000]")[0]  # capture cut before BTN_TOUCH up
    gestures = parser_mod.parse_getevent(text, RANGES, DISPLAY)
    assert len(gestures) == 1
    assert gestures[0]["type"] in ("tap", "swipe")
