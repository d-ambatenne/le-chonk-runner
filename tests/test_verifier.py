"""tasks/le-chonk-runner/tests/score.py on fixture projects, with a fake ./kotlin wrapper."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FAKE_KOTLIN, GRADLE_PROJECT, SCORE, TOOLCHAIN_PROJECT, make_executable, python, write_tree


def _score(project: Path, out: Path, *extra: str, env: dict[str, str] | None = None) -> tuple[dict, dict]:
    import os

    subprocess.run([python(), str(SCORE), "--project", str(project), "--out", str(out), *extra], check=True,
                   env={**os.environ, **(env or {})}, capture_output=True, text=True)
    return json.loads((out / "reward.json").read_text()), json.loads((out / "details.json").read_text())


@pytest.fixture
def toolchain_project(tmp_path: Path) -> Path:
    project = tmp_path / "app"
    write_tree(project, TOOLCHAIN_PROJECT)
    make_executable(project / "kotlin", FAKE_KOTLIN)
    return project


def test_a_correct_project_passes_everything(toolchain_project: Path, tmp_path: Path) -> None:
    rewards, details = _score(toolchain_project, tmp_path / "out")
    assert rewards["reward"] == 1.0
    assert all(rewards[key] == 1 for key in details["assertions"])
    assert details["complete"] and not details["environment_errors"]
    assert details["commands"]["android"]["command"] == "./kotlin build -m android-app"
    assert details["commands"]["tests"]["command"] == "./kotlin test -m shared"
    assert "build.log" in {p.name for p in (tmp_path / "out").iterdir()}


def test_the_apk_is_kept_only_when_asked(toolchain_project: Path, tmp_path: Path) -> None:
    _, details = _score(toolchain_project, tmp_path / "plain")
    assert details["apk"] is None and not list((tmp_path / "plain").glob("*.apk"))
    _, details = _score(toolchain_project, tmp_path / "kept", "--keep-apk")
    assert details["apk"] == "app-debug.apk" and (tmp_path / "kept" / "app-debug.apk").is_file()


def test_generated_gradle_files_under_build_dont_count(toolchain_project: Path, tmp_path: Path) -> None:
    _score(toolchain_project, tmp_path / "first")  # the fake build writes build/gradle-project/build.gradle.kts
    assert (toolchain_project / "build" / "gradle-project" / "build.gradle.kts").is_file()
    rewards, _ = _score(toolchain_project, tmp_path / "second")
    assert rewards["no_gradle_or_maven"] == 1


def test_a_gradle_project_fails_the_toolchain_assertions(tmp_path: Path) -> None:
    project = tmp_path / "app"
    write_tree(project, GRADLE_PROJECT)
    rewards, details = _score(project, tmp_path / "out")
    assert rewards["no_gradle_or_maven"] == 0 and rewards["toolchain_config"] == 0 and rewards["android_apk"] == 0
    assert "build.gradle.kts" in details["facts"]["gradle_or_maven_files"]
    assert rewards["reward"] == 0.0


def test_a_licence_failure_is_reported_as_a_machine_problem(toolchain_project: Path, tmp_path: Path) -> None:
    rewards, details = _score(toolchain_project, tmp_path / "out", env={"FAKE_LICENCE_MARKER": str(tmp_path / "never")})
    assert rewards["android_apk"] == 0
    assert "android_sdk_licences" in details["environment_errors"]


def test_failing_tests_fail_one_assertion(toolchain_project: Path, tmp_path: Path) -> None:
    rewards, _ = _score(toolchain_project, tmp_path / "out", env={"FAKE_TEST_EXIT": "1"})
    assert rewards["shared_tests_pass"] == 0 and rewards["reward"] == 0.875


def test_static_only_and_missing_project(tmp_path: Path) -> None:
    rewards, details = _score(tmp_path / "nothing-here", tmp_path / "out", "--no-build")
    assert rewards["reward"] == 0.0 and details["complete"] is False


def test_composable_must_reach_both_apps(toolchain_project: Path, tmp_path: Path) -> None:
    (toolchain_project / "ios-app" / "src" / "ViewController.kt").write_text("fun MainViewController() = ComposeUIViewController { }\n")
    rewards, _ = _score(toolchain_project, tmp_path / "out", "--no-build")
    assert rewards["shared_composable_in_both_apps"] == 0


def test_product_parsing_without_pyyaml(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("score_module", SCORE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "score_module", module)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml now raises ImportError
    assert module._parse_product("product: android/app\n") == ("android/app", [])
    assert module._parse_product("product:\n  type: kmp/lib\n  platforms: [android, iosArm64]\n") == ("kmp/lib", ["android", "iosArm64"])
