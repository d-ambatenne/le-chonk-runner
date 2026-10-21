"""Real Harbor trials of the task, without Docker: a host environment, the fake toolchain, and either Harbor's
oracle agent or the Studio agent with a scripted client. Then the comparison runs on Harbor's real output.

Skipped unless running as root on a throwaway machine (it writes /app, /logs, /tests, /solution, /harbor).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT, TASK
from le_chonk_eval.utils.compare import compare
from le_chonk_eval.utils.results import read_job

HERE = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(os.geteuid() != 0 or not os.environ.get("LECHONK_HOST_TRIALS"),
                                reason="set LECHONK_HOST_TRIALS=1 and run as root on a throwaway machine")


def harbor(tmp_path: Path, job: str, agent: str, *extra: str) -> Path:
    env = {**os.environ, "NO_COLOR": "1", "PYTHONPATH": f"{HERE / 'harbor_host'}{os.pathsep}{HERE}", "MISTRAL_API_KEY": "test"}
    command = [str(ROOT / ".venv" / "bin" / "harbor"), "run", "-p", str(TASK), "-a", agent, "-m", "mistral/mistral-large-4",
               "-e", "host_env:HostEnvironment", "-k", "1", "-n", "1", "--job-name", job, "-o", str(tmp_path / "jobs"),
               "--yes", "--quiet", "--cpus", "ignore", "--memory", "ignore", *extra]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return tmp_path / "jobs" / job


def test_oracle_trial(tmp_path: Path, fake_kotlin_bin: Path) -> None:
    job = harbor(tmp_path, "it-oracle", "oracle")
    run = read_job(job)[0]
    assert run["reward"] == 1.0, json.dumps(run, indent=2)
    assert run["environment"]["warmup"] is None or isinstance(run["environment"]["warmup"], str)
    assert run["project_dir"] and Path(run["project_dir"], "project.yaml").is_file()


def test_studio_agent_trials_with_and_without_the_skill(tmp_path: Path, fake_kotlin_bin: Path) -> None:
    skill = os.environ["LECHONK_SKILL_DIR"]
    no_skill = harbor(tmp_path, "it-no-skill", "scripted_studio:ScriptedStudioAgent")
    with_skill = harbor(tmp_path, "it-skill", "scripted_studio:ScriptedStudioAgent", "--skill", skill)

    runs = {run["arm"]: run for job in (no_skill, with_skill) for run in read_job(job)}
    assert set(runs) == {"no-skill", "skill"}
    for arm, run in runs.items():
        assert run["reward"] == 1.0, json.dumps(run, indent=2)
        assert run["input_tokens"] == 8000 and run["output_tokens"] == 800 and run["cost_usd"] > 0
        assert run["process"]["kotlin_calls"] == 1 and run["process"]["gradle_calls"] == 0
        assert run["studio"]["studio_agent"] == f"le-chonk-builder-{arm}" and run["stop_reason"] == "done"
    assert runs["skill"]["process"]["skill_md_read"] and not runs["no-skill"]["process"]["skill_md_read"]
    assert runs["skill"]["studio"]["skills"] == ["kotlin-tooling-kotlin-toolchain"]

    summary = compare([no_skill, with_skill], ab_id="it")
    assert summary["comparison"]["reward_delta"] == 0.0
    assert Path(summary["dashboard"]).is_file()
