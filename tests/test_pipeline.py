"""Reading Harbor jobs, comparing them, and the Workflows pipeline, with the fake harbor."""

from __future__ import annotations

import json
from pathlib import Path

import mistralai.workflows as workflows
import pytest
import temporalio.workflow
from mistralai.workflows.core.sandbox import get_sandbox_restrictions
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from le_chonk_eval.utils.compare import compare
from le_chonk_eval.utils.results import ASSERTIONS, job_status, process_metrics, read_job
from le_chonk_eval.utils.stats import permutation_p_value
from le_chonk_eval.workflow import fake_harbor
from le_chonk_eval.workflow.pipeline import HarborAb, HarborAbInput, HarborJobInput, plan_jobs, run_harbor_job


def make_job(jobs: Path, name: str, skill: bool, attempts: int = 4, model: str = "mistral/mistral-large-4") -> Path:
    args = ["run", "-p", "tasks/le-chonk-runner", "-m", model, "-k", str(attempts), "--job-name", name, "-o", str(jobs)]
    fake_harbor.main(args + (["--skill", "skills/kotlin-tooling-kotlin-toolchain"] if skill else []))
    return jobs / name


def test_runs_are_read_from_harbor_job_folders(tmp_path: Path) -> None:
    job = make_job(tmp_path / "jobs", "ab-skill", skill=True, attempts=3)
    runs = read_job(job)
    assert len(runs) == 3 and job_status(job) == {"job": "ab-skill", "trials_done": 3, "trials_total": 3, "finished": True}
    run = runs[0]
    assert run["arm"] == "skill" and run["model"] == "mistral/mistral-large-4"
    assert set(run["assertions"]) == set(ASSERTIONS)
    assert run["agent_seconds"] > 0 and run["input_tokens"] > 0 and run["cost_usd"] > 0
    assert run["process"]["kotlin_scaffold_used"] and run["process"]["skill_md_read"] and run["process"]["gradle_calls"] == 0


def test_process_metrics_count_the_agents_commands() -> None:
    def call(name: str, **arguments: str) -> dict:
        return {"function_name": name, "arguments": arguments}

    steps = [{"source": "agent", "tool_calls": [
        call("run_command", command="./gradlew assembleDebug"),
        call("run_command", command="cat build.gradle.kts && ls ~/.gradle"),
        call("run_command", command="kotlin init compose-multiplatform && ./kotlin build -m androidApp"),
        call("read_file", path="/harbor/skills/kotlin-tooling-kotlin-toolchain/SKILL.md"),
    ]}, {"source": "agent", "message": "Done."}]
    metrics = process_metrics({"steps": steps})
    assert metrics["model_calls"] == 2 and metrics["tool_calls"] == 4 and metrics["shell_commands"] == 3
    assert metrics["gradle_calls"] == 1 and metrics["kotlin_calls"] == 1
    assert metrics["kotlin_scaffold_used"] and metrics["skill_md_read"]


def test_compare_writes_summary_and_dashboard(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    a, b = make_job(jobs, "ab1-no-skill", skill=False, attempts=6), make_job(jobs, "ab1-skill", skill=True, attempts=6)
    summary = compare([a, b])
    assert summary["ab_id"] == "ab1" and summary["model"] == "mistral/mistral-large-4"
    assert list(summary["arms"]) == ["no-skill", "skill"]
    assert summary["comparison"]["reward_delta"] > 0 and 0 <= summary["comparison"]["reward_p_value"] <= 1
    assert summary["arms"]["no-skill"]["gradle_calls_mean"] > 0
    out = tmp_path / "results" / "ab1"
    html = (out / "dashboard.html").read_text()
    assert "Kotlin Toolchain skill A/B" in html and "with the skill" in html
    assert len((out / "runs.jsonl").read_text().splitlines()) == 12


def test_machine_problems_and_missing_verifier_are_set_aside(tmp_path: Path) -> None:
    job = make_job(tmp_path / "jobs", "ab2-skill", skill=True, attempts=3)
    trials = sorted(p for p in job.iterdir() if p.is_dir())
    details = json.loads((trials[0] / "verifier" / "details.json").read_text())
    details["environment_errors"] = ["android_sdk_licences"]
    (trials[0] / "verifier" / "details.json").write_text(json.dumps(details))
    result = json.loads((trials[1] / "result.json").read_text())
    result["verifier_result"] = None
    result["exception_info"] = {"exception_type": "EnvironmentStartTimeoutError", "exception_message": "no docker"}
    (trials[1] / "result.json").write_text(json.dumps(result))
    (trials[1] / "verifier" / "reward.json").unlink()
    (trials[1] / "verifier" / "details.json").unlink()

    arm = compare([job], ab_id="ab2")["arms"]["skill"]
    assert arm["trials"] == 3 and arm["n"] == 1 and arm["set_aside"] == 2


def test_jobs_of_different_models_are_not_mixed(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    a = make_job(jobs, "m-no-skill", skill=False, attempts=2)
    b = make_job(jobs, "m-skill", skill=True, attempts=2, model="mistral/mistral-medium-latest")
    with pytest.raises(ValueError, match="different models"):
        compare([a, b])


def test_permutation_test() -> None:
    assert permutation_p_value([0.0] * 6, [1.0] * 6) == pytest.approx(2 / 924, abs=1e-4)
    assert permutation_p_value([0.5, 0.5], [0.5, 0.5]) == 1.0


def test_plan_has_one_job_per_arm() -> None:
    jobs = plan_jobs(HarborAbInput(attempts=3, max_cost_usd=2), "ab-x")
    assert [j.job_name for j in jobs] == ["ab-x-no-skill", "ab-x-skill"]
    assert all(j.attempts == 3 and j.max_cost_usd == 2 for j in jobs)


async def test_the_workflow_runs_in_process_with_the_fake_harbor() -> None:
    summary = await workflows.execute_workflow(HarborAb, HarborAbInput(ab_id="smoke-1", attempts=3, smoke=True))
    assert [job["finished"] for job in summary["jobs"]] == [True, True]
    assert summary["comparison"]["reward_delta"] is not None
    assert Path(summary["dashboard"]).is_file()


async def test_a_retried_activity_resumes_the_job(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    job = make_job(jobs, "ab3-skill", skill=True, attempts=4)
    trials = sorted(p for p in job.iterdir() if p.is_dir())
    for trial in trials[2:]:  # as if the worker died after two trials
        for path in sorted(trial.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        trial.rmdir()
    (job / "result.json").unlink()
    outcome = await run_harbor_job(HarborJobInput(ab_id="ab3", arm="skill", attempts=4, smoke=True))
    assert outcome["finished"] and outcome["trials_done"] == 4
    assert "job resume" in (tmp_path / "results" / "ab3" / "ab3-skill.log").read_text()


async def test_workflows_import_inside_the_sandbox() -> None:
    runner = SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions())
    runner.prepare_workflow(temporalio.workflow._Definition.must_from_class(HarborAb))
