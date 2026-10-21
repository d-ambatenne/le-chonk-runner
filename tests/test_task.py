"""The Harbor task: its config, its scripts, and what the oracle and the warm-up do with a fake toolchain."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from harbor.models.task.config import TaskConfig
from harbor.models.task.task import Task

from conftest import FAKE_KOTLIN, SCORE, TASK, make_executable, python
from le_chonk_eval.utils.results import ASSERTIONS
from le_chonk_eval.workflow.harbor_jobs import AGENT, run_command


def test_task_loads_with_harbor() -> None:
    config = TaskConfig.model_validate_toml((TASK / "task.toml").read_text())
    assert config.environment.workdir == "/app"
    assert config.environment.memory_mb and config.environment.cpus
    task = Task(TASK)
    assert "Kotlin Toolchain" in task.instruction and "/app" in task.instruction


def test_scripts_are_executable_and_parse() -> None:
    for script in ("tests/test.sh", "solution/solve.sh", "environment/warmup.sh"):
        path = TASK / script
        assert os.access(path, os.X_OK), script
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_rubric_matches_the_skill_eval() -> None:
    evals = json.loads((TASK / "tests" / "evals.json").read_text())
    assert len(evals["evals"][0]["assertions"]) == len(ASSERTIONS) == 8


def test_the_skill_folder_holds_no_rubric() -> None:
    skill = Path(__file__).resolve().parents[1] / "skills" / "kotlin-tooling-kotlin-toolchain"
    if skill.is_dir():  # the real skill, when present: Harbor copies the whole folder into the container
        assert not (skill / "evals").exists(), "move evals/ out of the skill: the agent could read the grader's rubric"


def _config(command: list[str]) -> dict:
    output = subprocess.run(command + ["--print-config"], check=True, capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1"}).stdout
    return json.loads(output[output.index("{"):])


def test_harbor_accepts_both_arms_with_the_studio_agent() -> None:
    configs = {arm: _config(run_command(f"demo-{arm}", arm, attempts=2, max_cost_usd=4)) for arm in ("no-skill", "skill")}
    no_skill, skill = configs["no-skill"]["agents"][0], configs["skill"]["agents"][0]
    for agent in (no_skill, skill):
        assert AGENT in (agent.get("import_path"), agent.get("name"))
        assert agent["model_name"] == "mistral/mistral-large-4"
        assert float(agent["kwargs"]["max_cost_usd"]) == 4
    assert not no_skill.get("skills") and skill["skills"]
    assert configs["skill"]["n_attempts"] == 2
    for key in ("tasks", "n_concurrent_trials"):
        assert configs["skill"][key] == configs["no-skill"][key]



def test_oracle_scaffolds_a_project_the_verifier_passes(tmp_path: Path, fake_kotlin_bin: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    subprocess.run(["bash", str(TASK / "solution" / "solve.sh")], check=True, env={**os.environ, "LECHONK_APP_DIR": str(app)})
    assert (app / "shared" / "test" / "SmokeTest.kt").is_file()
    out = tmp_path / "verifier"
    subprocess.run([python(), str(SCORE), "--project", str(app), "--out", str(out)], check=True)
    assert json.loads((out / "reward.json").read_text())["reward"] == 1.0


def test_oracle_falls_back_to_kotlin_new(tmp_path: Path, fake_kotlin_bin: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    env = {**os.environ, "LECHONK_APP_DIR": str(app), "FAKE_TEMPLATE_VERB": "new"}
    result = subprocess.run(["bash", str(TASK / "solution" / "solve.sh")], check=True, env=env, capture_output=True, text=True)
    assert "kotlin new compose-multiplatform" in result.stdout
    assert (app / "project.yaml").is_file()


def _warmup(tmp_path: Path, **env: str) -> str:
    status = tmp_path / "warmup-status"
    sdk_root = tmp_path / "sdk-search"
    full_env = {
        **os.environ,
        "LECHONK_WARMUP_LOG": str(tmp_path / "warmup.log"),
        "LECHONK_WARMUP_STATUS": str(status),
        "LECHONK_WARMUP_DIR": str(tmp_path / "warmup"),
        "LECHONK_SDK_SEARCH": str(sdk_root),
        **env,
    }
    subprocess.run(["bash", str(TASK / "environment" / "warmup.sh")], check=True, env=full_env, capture_output=True, text=True)
    assert not (tmp_path / "warmup").exists(), "the warm-up project must be deleted"
    return status.read_text()


def test_warmup_builds_the_template(tmp_path: Path, fake_kotlin_bin: Path) -> None:
    assert _warmup(tmp_path).startswith("ok (template: kotlin init compose-multiplatform")


def test_warmup_falls_back_to_a_minimal_android_app(tmp_path: Path, fake_kotlin_bin: Path) -> None:
    assert _warmup(tmp_path, FAKE_TEMPLATE_VERB="none").startswith("ok (fallback: minimal android/app")


def test_warmup_accepts_licences_of_a_provisioned_sdk_and_retries(tmp_path: Path, fake_kotlin_bin: Path) -> None:
    marker = tmp_path / "licences-accepted"
    make_executable(tmp_path / "sdk-search" / "android-sdk" / "cmdline-tools" / "latest" / "bin" / "sdkmanager",
                    f"#!/usr/bin/env bash\nread -r answer\ntouch {marker}\n")
    status = _warmup(tmp_path, FAKE_LICENCE_MARKER=str(marker))
    assert marker.is_file() and status.startswith("ok")


def test_warmup_reports_a_build_that_keeps_failing(tmp_path: Path, fake_kotlin_bin: Path) -> None:
    assert _warmup(tmp_path, FAKE_BUILD_EXIT="1").startswith("android build failed")


def test_fake_kotlin_matches_conftest() -> None:
    assert "compose-multiplatform" in FAKE_KOTLIN
