"""lechonk-qa: the emulator QA runs on. Headless, on its own port so it never meets the one you use by hand, and
booted from a clean snapshot, so every run starts from the same phone.

The first boot is a cold one; when it's done, the snapshot qa-clean is saved. Later boots load it and never save
over it. Create the emulator once (docs/next.md, QA):

    sdkmanager "system-images;android-35;google_apis;arm64-v8a"
    avdmanager create avd -n lechonk-qa -k "system-images;android-35;google_apis;arm64-v8a" -d pixel_7
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from .device import AdbError, Runner, run_command

AVD = "lechonk-qa"
PORT = 5580
SNAPSHOT = "qa-clean"
SYSTEM_IMAGE = "system-images;android-35;google_apis;arm64-v8a"  # Apple Silicon; x86_64 on an Intel machine
BOOT_TIMEOUT = 300

Spawn = Callable[[list[str], Path], Awaitable[Any]]  # starts a long-running command, logging to a file


class EmulatorError(RuntimeError):
    pass


def sdk_dir() -> Path | None:
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(name):
            return Path(os.environ[name])
    mac = Path.home() / "Library" / "Android" / "sdk"
    return mac if mac.is_dir() else None


def tool(name: str) -> str:
    """adb, emulator or avdmanager: from the Android SDK, else from the PATH."""
    sdk = sdk_dir()
    places = {"adb": "platform-tools/adb", "emulator": "emulator/emulator", "avdmanager": "cmdline-tools/latest/bin/avdmanager",
              "sdkmanager": "cmdline-tools/latest/bin/sdkmanager"}
    if sdk and (sdk / places[name]).exists():
        return str(sdk / places[name])
    return shutil.which(name) or name


def aapt2() -> str | None:
    """The newest build tools' aapt2, which reads an APK's package name."""
    sdk = sdk_dir()
    found = sorted((sdk / "build-tools").glob("*/aapt2"), key=lambda p: [int(n) if n.isdigit() else 0 for n in p.parent.name.split(".")]) if sdk else []
    return str(found[-1]) if found else shutil.which("aapt2")


async def apk_package(apk: Path, run: Runner = run_command) -> str | None:
    """The APK's package name, or None when aapt2 isn't there or can't read it."""
    binary = aapt2()
    if not binary:
        return None
    try:
        name = (await run([binary, "dump", "packagename", str(apk)], 60)).decode(errors="replace").strip()
    except (AdbError, OSError):
        return None
    return name if name and "." in name and " " not in name else None


def avd_home() -> Path:
    return Path(os.environ.get("ANDROID_AVD_HOME") or Path.home() / ".android" / "avd")


def create_commands(image: str = SYSTEM_IMAGE) -> list[list[str]]:
    """What makes lechonk-qa, for people to run once."""
    return [[tool("sdkmanager"), image], [tool("avdmanager"), "create", "avd", "-n", AVD, "-k", image, "-d", "pixel_7"]]


async def spawn_logged(command: list[str], log: Path) -> Any:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as handle:
        return await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL, stdout=handle, stderr=handle,
                                                    start_new_session=True)


class Emulator:
    def __init__(self, avd: str = AVD, port: int = PORT, *, run: Runner = run_command, spawn: Spawn = spawn_logged,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, boot_timeout: float = BOOT_TIMEOUT, log: Path | None = None) -> None:
        self.avd, self.port, self.run, self.spawn, self.sleep = avd, port, run, spawn, sleep
        self.boot_timeout = boot_timeout
        self.log = log or Path("emulator.log")
        self.process: Any = None

    @property
    def serial(self) -> str:
        return f"emulator-{self.port}"

    def adb(self, *args: str) -> list[str]:
        return [tool("adb"), "-s", self.serial, *args]

    def has_snapshot(self) -> bool:
        return (avd_home() / f"{self.avd}.avd" / "snapshots" / SNAPSHOT).is_dir()

    def boot_command(self, from_snapshot: bool) -> list[str]:
        command = [tool("emulator"), "-avd", self.avd, "-port", str(self.port), "-no-window", "-no-audio", "-no-boot-anim",
                   "-no-snapshot-save"]
        command += ["-snapshot", SNAPSHOT] if from_snapshot else ["-no-snapshot-load"]
        return command + shlex.split(os.environ.get("LECHONK_QA_EMULATOR_ARGS", ""))

    async def exists(self) -> bool:
        listed = (await self.run([tool("emulator"), "-list-avds"], 60)).decode(errors="replace")
        return self.avd in {line.strip() for line in listed.splitlines()}

    async def start(self) -> str:
        if not await self.exists():
            steps = "\n".join("    " + shlex.join(command) for command in create_commands())
            raise EmulatorError(f"There's no emulator named {self.avd}. Create it once:\n{steps}")
        from_snapshot = self.has_snapshot()
        self.process = await self.spawn(self.boot_command(from_snapshot), self.log)
        await self.wait_for_boot()
        if not from_snapshot:  # the clean phone later runs start from
            await self.run(self.adb("emu", "avd", "snapshot", "save", SNAPSHOT), 120)
        return self.serial

    async def wait_for_boot(self) -> None:
        waited = 0.0
        while waited < self.boot_timeout:
            if getattr(self.process, "returncode", None) not in (None, 0):
                raise EmulatorError(f"The emulator stopped while booting (exit {self.process.returncode}): see {self.log}")
            with suppress(AdbError):
                if (await self.run(self.adb("shell", "getprop sys.boot_completed"), 10)).decode().strip() == "1":
                    return
            await self.sleep(2)
            waited += 2
        raise EmulatorError(f"{self.avd} didn't finish booting in {self.boot_timeout:g} s: see {self.log}")

    async def stop(self) -> None:
        with suppress(AdbError):
            await self.run(self.adb("emu", "kill"), 30)
        if self.process is not None and getattr(self.process, "returncode", 0) is None:
            with suppress(Exception):
                await asyncio.wait_for(self.process.wait(), 30)
            if self.process.returncode is None:
                self.process.kill()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[str]:
        serial = await self.start()
        try:
            yield serial
        finally:
            await self.stop()
