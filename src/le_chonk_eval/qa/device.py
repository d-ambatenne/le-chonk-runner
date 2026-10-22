"""An Android emulator or phone over adb: what the QA agent's tools do on it.

Every command goes through a runner, so the tests use a fake one: no adb, no emulator.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

PNG = b"\x89PNG\r\n\x1a\n"
WINDOW_DUMP = "/sdcard/lechonk-window.xml"
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_SIZE = re.compile(r"(\d+)x(\d+)")

Runner = Callable[[list[str], float], Awaitable[bytes]]  # a command and its timeout in seconds -> its output


class AdbError(RuntimeError):
    pass


async def run_command(args: list[str], timeout: float = 60) -> bytes:
    """Run a command and return what it printed. A failure or a timeout raises AdbError, with the end of its output."""
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise AdbError(f"{shlex.join(args)} timed out after {timeout:g} s") from None
    if process.returncode != 0:
        said = (err or out).decode(errors="replace").strip()[-500:]
        raise AdbError(f"{shlex.join(args)} exited with {process.returncode}: {said}")
    return out


@dataclass
class Element:
    """One readable thing on screen, from the UI tree: a text, a button, a description."""

    key: str  # e1, e2, ...: how the agent names it in a tap
    text: str
    description: str  # content-desc: what a Compose semantics description says
    resource_id: str
    kind: str  # the class's short name, such as View or Button
    bounds: tuple[int, int, int, int]
    clickable: bool

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    def line(self) -> str:
        parts = [self.key]
        if self.text:
            parts.append(f'"{self.text}"')
        if self.description:
            parts.append(f'description="{self.description}"')
        if self.resource_id:
            parts.append(f"id={self.resource_id.split('/')[-1]}")
        parts.append(self.kind + (", clickable" if self.clickable else ""))
        left, top, right, bottom = self.bounds
        parts.append(f"at {left},{top}-{right},{bottom}")
        return " ".join(parts)


def parse_tree(xml: str) -> list[Element]:
    """The readable elements of a uiautomator dump, in screen order: those with a text, a description, an id, or a tap."""
    start = xml.find("<hierarchy")
    if start < 0:
        raise AdbError(f"not a UI dump: {xml[:200]!r}")
    end = xml.rfind("</hierarchy>")
    root = ET.fromstring(xml[start:end + len("</hierarchy>")] if end > start else xml[start:])
    found: list[Element] = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        description = (node.get("content-desc") or "").strip()
        resource_id = (node.get("resource-id") or "").strip()
        clickable = node.get("clickable") == "true"
        if not (text or description or resource_id or clickable):
            continue
        match = _BOUNDS.fullmatch((node.get("bounds") or "").strip())
        bounds = tuple(int(n) for n in match.groups()) if match else (0, 0, 0, 0)
        kind = (node.get("class") or "").rsplit(".", 1)[-1] or "View"
        found.append(Element(f"e{len(found) + 1}", text, description, resource_id, kind, bounds, clickable))  # type: ignore[arg-type]
    return found


def tree_text(elements: list[Element]) -> str:
    return "\n".join(element.line() for element in elements) or "(nothing readable on screen: no text, description or button)"


def is_png(data: bytes) -> bool:
    return data.startswith(PNG)


class Device:
    """An emulator or phone, by its adb serial, such as emulator-5580."""

    def __init__(self, serial: str, adb: str = "adb", run: Runner = run_command) -> None:
        self.serial, self.adb, self.run = serial, adb, run

    def command(self, *args: str) -> list[str]:
        return [self.adb, "-s", self.serial, *args]

    async def shell(self, command: str, timeout: float = 60) -> str:
        return (await self.run(self.command("shell", command), timeout)).decode(errors="replace")

    async def ui_tree(self) -> list[Element]:
        await self.run(self.command("shell", f"uiautomator dump {WINDOW_DUMP}"), 30)
        return parse_tree((await self.run(self.command("exec-out", f"cat {WINDOW_DUMP}"), 30)).decode(errors="replace"))

    async def screenshot(self) -> bytes:
        return self._png(await self.run(self.command("exec-out", "screencap -p"), 30))

    async def tap(self, x: int, y: int) -> None:
        await self.shell(f"input tap {int(x)} {int(y)}")

    async def tap_then_capture(self, x: int, y: int, ms: int) -> bytes:
        """Tap, wait, take a screenshot: one shell call, because a jump is over before a model's next turn."""
        command = f"input tap {int(x)} {int(y)}; sleep {max(ms, 0) / 1000:.3f}; screencap -p"
        return self._png(await self.run(self.command("exec-out", command), 30))

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def screen_size(self) -> tuple[int, int]:
        sizes = _SIZE.findall(await self.shell("wm size"))  # Physical size, then Override size if there is one
        if not sizes:
            return 1080, 2400
        width, height = sizes[-1]
        return int(width), int(height)

    async def logcat(self, package: str, lines: int = 100) -> str:
        """The app's latest log lines, and the crash buffer's."""
        try:
            pid = (await self.shell(f"pidof {package}", 10)).strip().split()[0]
        except (AdbError, IndexError):
            pid = ""
        log = await self.shell(f"logcat -d -t {int(lines)}" + (f" --pid={pid}" if pid else ""), 30)
        crashes = await self.crashes()
        return log + (f"\n--- crashes ---\n{crashes}" if crashes else "")

    async def crashes(self) -> str:
        return (await self.shell("logcat -d -b crash -t 50", 30)).strip()

    async def clear_logs(self) -> None:
        await self.shell("logcat -c -b main -b crash", 30)

    async def packages(self) -> set[str]:
        """The apps people installed: what an installed APK adds to."""
        listed = await self.shell("pm list packages -3", 30)
        return {line.split(":", 1)[1].strip() for line in listed.splitlines() if line.startswith("package:")}

    async def install(self, apk: str) -> None:
        await self.run(self.command("install", "-r", "-t", "-g", apk), 240)

    async def launch_activity(self, package: str) -> str | None:
        """The app's launcher activity, such as dev.example/.MainActivity."""
        found = await self.shell(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {package}", 30)
        lines = [line.strip() for line in found.splitlines() if "/" in line]
        return lines[-1] if lines else None

    async def restart_app(self, package: str, activity: str | None) -> None:
        await self.shell(f"am force-stop {package}")
        if activity:
            await self.shell(f"am start -W -n {activity}", 60)
        else:
            await self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1", 60)

    @staticmethod
    def _png(data: bytes) -> bytes:
        if not is_png(data):
            raise AdbError(f"the screenshot isn't a PNG: {data[:80]!r}")
        return data


def describe(element: Element | None, x: int, y: int) -> str:
    return f"{element.key} ({element.text or element.description or element.kind}) at {x},{y}" if element else f"{x},{y}"


def find(elements: list[Element], key: Any) -> Element | None:
    key = str(key or "").strip()
    return next((element for element in elements if element.key == key), None)
