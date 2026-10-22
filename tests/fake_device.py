"""A phone in memory for the QA tests: Le Chonk Runner's screens over a fake clock, and a fake adb for Device's commands.

FakeDevice has Device's methods. The game runs while time passes: left alone, Le Chonk loses a heart every 2 s, and
at zero the game-over screen shows the score and RESTART. A tap on RESTART starts a new run.
"""

from __future__ import annotations

from typing import Any

from le_chonk_eval.qa.device import PNG, AdbError, Element, parse_tree

SHOT = PNG + b"fake-image-data"
PACKAGE = "dev.dambatenne.lechonk"


def window(nodes: str) -> str:
    """A uiautomator dump, as `uiautomator dump` writes it."""
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">"
            "<node index=\"0\" text=\"\" resource-id=\"\" class=\"android.widget.FrameLayout\" package=\"dev.dambatenne.lechonk\" "
            "content-desc=\"\" clickable=\"false\" bounds=\"[0,0][1080,2400]\">"
            f"{nodes}</node></hierarchy>")


def node(text: str = "", description: str = "", clickable: bool = False, bounds: str = "[0,0][10,10]", kind: str = "android.view.View",
         resource_id: str = "") -> str:
    return (f'<node index="0" text="{text}" resource-id="{resource_id}" class="{kind}" package="{PACKAGE}" content-desc="{description}" '
            f'clickable="{str(clickable).lower()}" bounds="{bounds}" />')


class FakeDevice:
    def __init__(self, hearts_described: bool = True, serial: str = "emulator-5580") -> None:
        self.serial = serial
        self.hearts_described = hearts_described  # the app's test hook: a description on the drawn hearts
        self.clock = 0.0
        self.started_at = 0.0
        self.installed: set[str] = set()
        self.calls: list[tuple[Any, ...]] = []
        self.restarts = 0
        self.crash_log = ""
        self.trees = 0
        self.tree_works_for: int | None = None  # the UI tree answers this many times, then the device breaks

    # --- The game ----------------------------------------------------------------------------------------

    @property
    def hearts(self) -> int:
        return max(0, 5 - int((self.clock - self.started_at) // 2))

    @property
    def score(self) -> int:
        return int((self.clock - self.started_at) * 10)

    def screen(self) -> str:
        nodes = node(text=f"Score: {self.score}", bounds="[800,60][1060,120]")
        if self.hearts_described:
            nodes += node(description=f"{self.hearts} of 5 hearts", bounds="[20,60][200,100]")
        if self.hearts == 0:
            nodes += node(text="GAME OVER", bounds="[300,1000][780,1100]")
            nodes += node(text="RESTART", clickable=True, kind="android.widget.Button", bounds="[400,1300][680,1400]")
        return window(nodes)

    # --- Device's methods --------------------------------------------------------------------------------

    async def ui_tree(self) -> list[Element]:
        self.calls.append(("ui_tree",))
        self.trees += 1
        if self.tree_works_for is not None and self.trees > self.tree_works_for:  # a sick emulator: Android kills uiautomator
            raise AdbError("adb shell uiautomator dump /sdcard/window_dump.xml exited 137: Killed")
        return parse_tree(self.screen())

    async def screenshot(self) -> bytes:
        self.calls.append(("screenshot",))
        return SHOT

    async def tap(self, x: int, y: int) -> None:
        self.calls.append(("tap", x, y))
        if self.hearts == 0 and 400 <= x <= 680 and 1300 <= y <= 1400:
            self.started_at = self.clock

    async def tap_then_capture(self, x: int, y: int, ms: int) -> bytes:
        self.calls.append(("tap_then_capture", x, y, ms))
        self.clock += ms / 1000
        return SHOT

    async def wait(self, seconds: float) -> None:
        self.calls.append(("wait", seconds))
        self.clock += seconds

    async def screen_size(self) -> tuple[int, int]:
        return 1080, 2400

    async def logcat(self, package: str, lines: int = 100) -> str:
        return "10-22 12:00:00.000 I LeChonk: run started"

    async def crashes(self) -> str:
        return self.crash_log

    async def clear_logs(self) -> None:
        self.calls.append(("clear_logs",))

    async def packages(self) -> set[str]:
        return set(self.installed)

    async def install(self, apk: str) -> None:
        self.calls.append(("install", apk))
        self.installed.add(PACKAGE)

    async def launch_activity(self, package: str) -> str | None:
        return f"{package}/.MainActivity"

    async def restart_app(self, package: str, activity: str | None) -> None:
        self.calls.append(("restart_app", package, activity))
        self.restarts += 1
        self.started_at = self.clock


class FakeAdb:
    """Device's commands, recorded; the answers come from (part of the command, output) pairs, first match wins."""

    def __init__(self, answers: list[tuple[str, bytes | Exception]] | None = None) -> None:
        self.answers = answers or []
        self.commands: list[list[str]] = []

    async def __call__(self, args: list[str], timeout: float = 60) -> bytes:
        self.commands.append(args)
        joined = " ".join(args)
        for part, answer in self.answers:
            if part in joined:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return b""


def failing(message: str = "exit 1") -> AdbError:
    return AdbError(message)
