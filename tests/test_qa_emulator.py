"""lechonk-qa: booting from the clean snapshot, saving it on the first boot, and stopping, against a fake adb."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_device import FakeAdb, failing
from le_chonk_eval.qa.emulator import Emulator, EmulatorError


class Spawned:
    returncode = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def emulator(adb: FakeAdb, spawned: list[list[str]], tmp_path: Path, **options: Any) -> Emulator:
    async def spawn(command: list[str], log: Path) -> Spawned:
        spawned.append(command)
        return Spawned()

    async def no_sleep(seconds: float) -> None:
        return None

    return Emulator(run=adb, spawn=spawn, sleep=no_sleep, log=tmp_path / "emulator.log", **options)


@pytest.fixture(autouse=True)
def sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "sdk"))  # no tools there: their names come from the PATH
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path / "avd"))
    monkeypatch.delenv("LECHONK_QA_EMULATOR_ARGS", raising=False)
    return tmp_path / "avd"


async def test_the_first_boot_is_cold_and_saves_the_clean_snapshot(tmp_path: Path) -> None:
    adb = FakeAdb([("-list-avds", b"Pixel_7\nlechonk-qa\n"), ("getprop", b"1\n")])
    spawned: list[list[str]] = []
    async with emulator(adb, spawned, tmp_path).running() as serial:
        assert serial == "emulator-5580"
    command = spawned[0]
    assert command[1:4] == ["-avd", "lechonk-qa", "-port"] and "5580" in command
    assert {"-no-window", "-no-audio", "-no-snapshot-save", "-no-snapshot-load"} <= set(command)
    sent = [" ".join(c[1:]) for c in adb.commands]
    assert "-s emulator-5580 emu avd snapshot save qa-clean" in sent and sent[-1] == "-s emulator-5580 emu kill"


async def test_later_boots_load_the_snapshot_and_never_save_it(tmp_path: Path, sdk: Path) -> None:
    (sdk / "lechonk-qa.avd" / "snapshots" / "qa-clean").mkdir(parents=True)
    adb = FakeAdb([("-list-avds", b"lechonk-qa\n"), ("getprop", b"1\n")])
    spawned: list[list[str]] = []
    await emulator(adb, spawned, tmp_path).start()
    assert spawned[0][-3:] == ["-no-snapshot-save", "-snapshot", "qa-clean"]
    assert not any("snapshot save" in " ".join(c) for c in adb.commands)


async def test_it_waits_for_the_boot_and_gives_up_in_time(tmp_path: Path) -> None:
    answers = iter([failing("device offline"), b"0\n", b"1\n"])

    class Booting(FakeAdb):
        async def __call__(self, args: list[str], timeout: float = 60) -> bytes:
            if "getprop" in args[-1]:
                self.commands.append(args)
                answer = next(answers)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return await super().__call__(args, timeout)

    adb = Booting([("-list-avds", b"lechonk-qa\n")])
    await emulator(adb, [], tmp_path).start()
    assert sum("getprop" in c[-1] for c in adb.commands) == 3

    never = FakeAdb([("-list-avds", b"lechonk-qa\n"), ("getprop", b"0\n")])
    with pytest.raises(EmulatorError, match="didn't finish booting in 10 s"):
        await emulator(never, [], tmp_path, boot_timeout=10).start()


async def test_a_missing_emulator_says_how_to_make_it(tmp_path: Path) -> None:
    with pytest.raises(EmulatorError, match="avdmanager create avd -n lechonk-qa -k 'system-images;android-35;google_apis;arm64-v8a'"):
        await emulator(FakeAdb([("-list-avds", b"Pixel_7\n")]), [], tmp_path).start()


async def test_the_apks_package_comes_from_the_newest_aapt2(tmp_path: Path) -> None:
    from le_chonk_eval.qa.emulator import apk_package

    for version in ("34.0.0", "35.0.1", "9.0.0"):
        tool_path = tmp_path / "sdk" / "build-tools" / version / "aapt2"
        tool_path.parent.mkdir(parents=True)
        tool_path.write_text("")
    adb = FakeAdb([("dump packagename", b"dev.dambatenne.lechonk\n")])
    assert await apk_package(tmp_path / "app.apk", run=adb) == "dev.dambatenne.lechonk"
    assert adb.commands[0][0].endswith("35.0.1/aapt2")
    assert await apk_package(tmp_path / "app.apk", run=FakeAdb([("dump packagename", failing("not an APK"))])) is None
