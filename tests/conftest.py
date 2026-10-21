from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks" / "le-chonk-runner"
SCORE = TASK / "tests" / "score.py"

FIXTURE_SKILL = """---
name: kotlin-tooling-kotlin-toolchain
description: >
  Load when building a Kotlin project with the Kotlin Toolchain.
---

# Kotlin Toolchain (test fixture)
"""

# A stand-in for the Kotlin Toolchain CLI. `init|new compose-multiplatform` writes a small template project
# (FAKE_TEMPLATE_VERB picks which verb works: init, new or none); `build` makes an APK and a generated
# Gradle file under build/; `test` passes. FAKE_LICENCE_MARKER makes `build` fail on licences until that file exists.
FAKE_KOTLIN = r"""#!/usr/bin/env bash
verb="${FAKE_TEMPLATE_VERB:-init}"
case "$1" in
  --version) echo "Kotlin Toolchain 0.0-fake"; exit 0 ;;
  init|new)
    if [ "$1" != "$verb" ] || [ "$2" != "compose-multiplatform" ]; then echo "no such template"; exit 2; fi
    mkdir -p shared/src shared/src@ios androidApp/src iosApp
    printf 'modules:\n  - ./shared\n  - ./androidApp\n  - ./iosApp\n' > project.yaml
    printf 'product:\n  type: kmp/lib\n  platforms: [android, iosArm64, iosSimulatorArm64]\n' > shared/module.yaml
    printf 'import androidx.compose.runtime.Composable\n\n@DevelopmentEntryPoint\n@Composable\nfun App() {}\n' > shared/src/App.kt
    printf 'fun MainViewController() = ComposeUIViewController { App() }\n' > shared/src@ios/MainViewController.kt
    printf 'product: android/app\n' > androidApp/module.yaml
    printf 'class MainActivity { fun onCreate() { setContent { App() } } }\n' > androidApp/src/MainActivity.kt
    printf 'product: ios/app\n' > iosApp/module.yaml
    cp "$0" ./kotlin && chmod +x ./kotlin
    exit 0 ;;
  build)
    if [ -n "${FAKE_LICENCE_MARKER:-}" ] && [ ! -f "$FAKE_LICENCE_MARKER" ]; then
      echo "License for package Android SDK Platform 35 not accepted. Run sdkmanager --licenses"; exit 1
    fi
    mkdir -p build/tasks/_app_buildAndroidDebug build/gradle-project
    touch build/tasks/_app_buildAndroidDebug/app-debug.apk
    echo 'plugins {}' > build/gradle-project/build.gradle.kts
    echo "BUILD SUCCESSFUL"; exit "${FAKE_BUILD_EXIT:-0}" ;;
  test) echo "All tests passed"; exit "${FAKE_TEST_EXIT:-0}" ;;
esac
echo "unknown command $1"; exit 2
"""


def write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def make_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


TOOLCHAIN_PROJECT = {
    "project.yaml": "modules:\n  - shared\n  - android-app\n  - ios-app\n",
    "shared/module.yaml": "product:\n  type: kmp/lib\n  platforms: [ android, iosArm64, iosSimulatorArm64 ]\nsettings:\n  compose: enabled\n",
    "shared/src/App.kt": "import androidx.compose.runtime.Composable\nimport org.jetbrains.compose.reload.DevelopmentEntryPoint\n\n@Composable\n@DevelopmentEntryPoint\nfun App() {\n}\n",
    "shared/src/Game.kt": "class Game { fun tick() {} }\n",
    "shared/test/GameTest.kt": "import kotlin.test.Test\nclass GameTest {\n  @Test fun jumpLands() {}\n}\n",
    "android-app/module.yaml": "product: android/app\ndependencies:\n  - ../shared\n",
    "android-app/src/MainActivity.kt": "class MainActivity { fun onCreate() { setContent { App() } } }\n",
    "ios-app/module.yaml": "product: ios/app\ndependencies:\n  - ../shared\n",
    "ios-app/src/ViewController.kt": "fun MainViewController() = ComposeUIViewController { App() }\n",
    "ios-app/src/EntryPoint.swift": "@main struct iOSApp: App {}\n",
}

GRADLE_PROJECT = {
    "settings.gradle.kts": 'include(":composeApp")\n',
    "build.gradle.kts": "plugins {}\n",
    "composeApp/build.gradle.kts": "kotlin { androidTarget(); iosArm64() }\n",
    "composeApp/src/commonMain/kotlin/App.kt": "@Composable\nfun App() {}\n",
}


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every folder the code writes to points at a temp dir; a small fixture skill stands in for the real one."""
    skill = tmp_path / "skills" / "kotlin-tooling-kotlin-toolchain"
    write_tree(skill, {"SKILL.md": FIXTURE_SKILL, "references/examples.md": "# Examples\n"})
    for name, sub in [("LECHONK_JOBS_DIR", "jobs"), ("LECHONK_RESULTS_DIR", "results"), ("LECHONK_STATE_DIR", "state"),
                      ("LECHONK_CHAT_DIR", "chat"), ("LECHONK_RUNS_DIR", "runs")]:
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("LECHONK_SKILL_DIR", str(skill))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("LECHONK_HARBOR_BIN", raising=False)
    return tmp_path


@pytest.fixture
def fake_kotlin_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bin folder with the fake `kotlin` first on PATH."""
    bin_dir = tmp_path / "bin"
    make_executable(bin_dir / "kotlin", FAKE_KOTLIN)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def python() -> str:
    return sys.executable
