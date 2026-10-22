"""QA's device layer: reading a uiautomator dump, and the adb commands Device sends, against a fake adb."""

from __future__ import annotations

import pytest

from fake_device import SHOT, FakeAdb, failing, node, window
from le_chonk_eval.qa.device import AdbError, Device, parse_tree, tree_text

GAME_OVER = window(
    node(text="Score: 130", bounds="[800,60][1060,120]")
    + node(description="0 of 5 hearts", bounds="[20,60][200,100]")
    + node(bounds="[0,200][1080,2000]")  # the game's Canvas: nothing to read
    + node(text="GAME OVER", bounds="[300,1000][780,1100]")
    + node(text="RESTART", clickable=True, kind="android.widget.Button", bounds="[400,1300][680,1400]")
) + "UI hierchary dumped to: /dev/tty"


def test_a_dump_becomes_the_readable_elements_in_screen_order() -> None:
    elements = parse_tree(GAME_OVER)
    assert [e.key for e in elements] == ["e1", "e2", "e3", "e4"]  # the Canvas has no text, description or tap
    score, hearts, over, restart = elements
    assert score.text == "Score: 130" and hearts.description == "0 of 5 hearts" and over.text == "GAME OVER"
    assert restart.clickable and restart.kind == "Button" and restart.center == (540, 1350)
    text = tree_text(elements)
    assert 'e2 description="0 of 5 hearts" View at 20,60-200,100' in text and 'e4 "RESTART" Button, clickable at 400,1300-680,1400' in text
    assert tree_text([]).startswith("(nothing readable")
    with pytest.raises(AdbError, match="not a UI dump"):
        parse_tree("ERROR: could not get idle state.")


async def test_device_commands() -> None:
    adb = FakeAdb([("cat /sdcard/lechonk-window.xml", GAME_OVER.encode()), ("screencap", SHOT), ("wm size", b"Physical size: 1080x2400\nOverride size: 720x1600\n"),
                   ("pm list packages", b"package:dev.dambatenne.lechonk\npackage:com.example.other\n"),
                   ("resolve-activity", b"priority=0 preferredOrder=0\ndev.dambatenne.lechonk/.MainActivity\n")])
    device = Device("emulator-5580", run=adb)
    assert [e.text for e in await device.ui_tree()][0] == "Score: 130"
    assert adb.commands[0] == ["adb", "-s", "emulator-5580", "shell", "uiautomator dump /sdcard/lechonk-window.xml"]

    assert await device.tap_then_capture(540, 1200, 300) == SHOT
    assert adb.commands[-1] == ["adb", "-s", "emulator-5580", "exec-out", "input tap 540 1200; sleep 0.300; screencap -p"]  # one call
    await device.tap(10, 20)
    assert adb.commands[-1][-1] == "input tap 10 20"
    assert await device.screen_size() == (720, 1600)  # the override, when there is one
    assert await device.packages() == {"dev.dambatenne.lechonk", "com.example.other"}
    assert await device.launch_activity("dev.dambatenne.lechonk") == "dev.dambatenne.lechonk/.MainActivity"
    await device.install("/tmp/app.apk")
    assert adb.commands[-1] == ["adb", "-s", "emulator-5580", "install", "-r", "-t", "-g", "/tmp/app.apk"]
    await device.restart_app("dev.dambatenne.lechonk", "dev.dambatenne.lechonk/.MainActivity")
    assert [c[-1] for c in adb.commands[-2:]] == ["am force-stop dev.dambatenne.lechonk", "am start -W -n dev.dambatenne.lechonk/.MainActivity"]


async def test_logs_are_the_apps_when_it_runs_and_all_otherwise() -> None:
    adb = FakeAdb([("pidof", b"4242\n"), ("-b crash", b"FATAL EXCEPTION: main\n"), ("logcat -d", b"I LeChonk: hit\n")])
    log = await Device("emulator-5580", run=adb).logcat("dev.dambatenne.lechonk", lines=50)
    assert "logcat -d -t 50 --pid=4242" in [c[-1] for c in adb.commands] and "FATAL EXCEPTION" in log
    adb = FakeAdb([("pidof", failing()), ("-b crash", b""), ("logcat -d", b"I system\n")])
    await Device("emulator-5580", run=adb).logcat("dev.dambatenne.lechonk")
    assert "logcat -d -t 100" in [c[-1] for c in adb.commands]


async def test_a_screenshot_that_isnt_a_png_is_an_error() -> None:
    device = Device("emulator-5580", run=FakeAdb([("screencap", b"error: device offline")]))
    with pytest.raises(AdbError, match="isn't a PNG"):
        await device.screenshot()
