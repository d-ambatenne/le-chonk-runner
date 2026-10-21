"""A stand-in for `harbor run` and `harbor job resume` that writes made-up job folders.

It uses Harbor's own result models, so the folders look like real ones, and the skill arm scores better.
`le-chonk ab --smoke` runs the whole pipeline with it: no Docker, no model calls, no cost. For a task made
from an issue (with environment/repo/), the made-up project is the repo plus a file for the issue, and a
trial whose APK "builds" leaves one in verifier/, as the real verifier does with --keep-apk.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from harbor.models.agent.context import AgentContext
from harbor.models.job.result import JobResult, JobStats
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig
from harbor.models.trial.result import AgentInfo, ModelInfo, TimingInfo, TrialResult
from harbor.models.verifier.result import VerifierResult

from ..utils.results import ASSERTIONS
from .harbor_jobs import AGENT

SKILL_PASS = 0.92
BASE_PASS = 0.35


def _pass_rate(skill: bool) -> float:
    """How often an assertion passes; LECHONK_FAKE_PASS fixes it for both arms (tests)."""
    if os.environ.get("LECHONK_FAKE_PASS"):
        return float(os.environ["LECHONK_FAKE_PASS"])
    return SKILL_PASS if skill else BASE_PASS


def _trajectory(skill: bool) -> dict:
    commands = (["kotlin init compose-multiplatform", "./kotlin build -m androidApp", "./kotlin test -m shared"] if skill
                else ["ls", "gradle init", "./gradlew assembleDebug", "kotlin build"])
    calls = [{"tool_call_id": f"c{i}", "function_name": "run_command", "arguments": {"command": c}} for i, c in enumerate(commands)]
    if skill:
        calls.insert(0, {"tool_call_id": "s0", "function_name": "read_file", "arguments": {"path": "/harbor/skills/kotlin-tooling-kotlin-toolchain/SKILL.md"}})
    steps = [{"step_id": 1, "source": "user", "message": "Build Le Chonk Runner"}]
    for call in calls:
        steps.append({"step_id": len(steps) + 1, "source": "agent", "message": "", "tool_calls": [call]})
    steps.append({"step_id": len(steps) + 1, "source": "agent", "message": "Done."})
    return {"schema_version": "ATIF-v1.8", "agent": {"name": "mistral-studio", "version": "fake"}, "steps": steps}


def _write_trial(job_dir: Path, job_name: str, task: str, model: str, skills: list[str], index: int) -> None:
    skill = bool(skills)
    rng = random.Random(f"{job_name}-{index}")
    trial_name = f"le-chonk-runner__{index:02d}{uuid4().hex[:5]}"
    trial_dir = job_dir / trial_name
    for sub in ("agent", "verifier", "artifacts/project/shared/src"):
        (trial_dir / sub).mkdir(parents=True, exist_ok=True)
    passed = {key: rng.random() < _pass_rate(skill) for key in ASSERTIONS}
    rewards = {"reward": round(sum(passed.values()) / len(passed), 4), **{k: int(v) for k, v in passed.items()}}
    repo = Path(task) / "environment" / "repo"
    apk = None
    if repo.is_dir():  # a task made from an issue
        project = trial_dir / "artifacts" / "project"
        shutil.rmtree(project)
        shutil.copytree(repo, project, ignore=shutil.ignore_patterns(".git"))
        (project / "shared" / "src").mkdir(parents=True, exist_ok=True)
        (project / "shared" / "src" / f"{job_name}.kt").write_text(f"// Made up by the fake harbor for {job_name}\nfun feature() = {index}\n")
        if passed["android_apk"]:
            apk = "app-debug.apk"
            (trial_dir / "verifier" / apk).write_bytes(b"PK fake apk")
    else:
        (trial_dir / "artifacts" / "project" / "shared" / "src" / "Game.kt").write_text("@Composable\nfun Game() {}\n")
    (trial_dir / "verifier" / "reward.json").write_text(json.dumps(rewards))
    commands, log = {}, ""  # as the real verifier writes them: each build in details.json, its output in build.log
    for check, name, command, error in (("android_apk", "android", "./kotlin build -m androidApp", "e: Game.kt:12:5 Unresolved reference: Hearts"),
                                        ("shared_tests_pass", "tests", "./kotlin test -m shared", "GameTest > hitCostsAHeart FAILED")):
        code = 0 if passed[check] else 1
        commands[name] = {"command": command, "exit_code": code, "seconds": 42.0, "timed_out": False}
        log += f"\n$ {command}\n[exit {code}, 42.0s, timed out: False]\n" + ("BUILD SUCCESSFUL\n" if code == 0 else f"> Task :compile\n{error}\nBUILD FAILED\n")
    (trial_dir / "verifier" / "build.log").write_text(log)
    (trial_dir / "verifier" / "details.json").write_text(json.dumps({
        "reward": rewards["reward"], "assertions": passed, "commands": commands, "environment_errors": [], "apk": apk,
        "environment": {"kotlin_toolchain": "fake 0.0", "warmup": "ok (fake)"},
    }))
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(_trajectory(skill)))

    config = TrialConfig(
        task=TaskConfig(path=Path(task)),
        trial_name=trial_name,
        trials_dir=job_dir,
        agent=AgentConfig(import_path=AGENT, model_name=model, skills=skills),
    )
    (trial_dir / "config.json").write_text(config.model_dump_json(indent=2))
    now = datetime.now(timezone.utc)
    minutes = rng.uniform(6, 12) if skill else rng.uniform(10, 25)
    tokens_in = int(rng.uniform(300_000, 900_000) if skill else rng.uniform(800_000, 2_500_000))
    tokens_out = int(tokens_in * 0.02)
    result = TrialResult(
        task_name="dambatenne/le-chonk-runner",
        trial_name=trial_name,
        trial_uri=trial_dir.resolve().as_uri(),
        task_id=LocalTaskId(path=Path(task)),
        task_checksum="fake",
        config=config,
        agent_info=AgentInfo(name="mistral-studio", version="fake", model_info=ModelInfo(name=model.split("/")[-1], provider="mistral")),
        agent_result=AgentContext(n_input_tokens=tokens_in, n_output_tokens=tokens_out, cost_usd=round(tokens_in * 0.68e-6 + tokens_out * 2.09e-6, 4)),
        verifier_result=VerifierResult(rewards=rewards),
        agent_execution=TimingInfo(started_at=now - timedelta(minutes=minutes), finished_at=now),
        started_at=now - timedelta(minutes=minutes + 2),
        finished_at=now,
    )
    (trial_dir / "result.json").write_text(result.model_dump_json(indent=2))


def _run_job(job_dir: Path, config: dict) -> None:
    agent = config["agents"][0]
    done = [p for p in job_dir.iterdir() if p.is_dir() and (p / "result.json").is_file()]
    started = datetime.now(timezone.utc)
    for index in range(len(done), int(config["n_attempts"])):
        time.sleep(0.2)
        _write_trial(job_dir, config["job_name"], config["tasks"][0]["path"], agent["model_name"], agent["skills"], index)
    job = JobResult(id=uuid4(), started_at=started, finished_at=datetime.now(timezone.utc), n_total_trials=int(config["n_attempts"]), stats=JobStats())
    (job_dir / "result.json").write_text(job.model_dump_json(indent=2))


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ["job", "resume"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("-p", "--job-path", required=True)
        args, _ = parser.parse_known_args(argv[2:])
        job_dir = Path(args.job_path)
        _run_job(job_dir, json.loads((job_dir / "config.json").read_text()))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("-p", "--path", required=True)
    parser.add_argument("-a", "--agent", default=AGENT)
    parser.add_argument("-m", "--model", required=True)
    parser.add_argument("-k", "--n-attempts", type=int, default=1)
    parser.add_argument("-n", "--n-concurrent", type=int, default=1)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("-o", "--jobs-dir", default="jobs")
    parser.add_argument("--skill", action="append", default=[])
    parser.add_argument("--ak", action="append", default=[])
    args, _ = parser.parse_known_args(argv)
    job_dir = Path(args.jobs_dir) / args.job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "job_name": args.job_name,
        "n_attempts": args.n_attempts,
        "n_concurrent_trials": args.n_concurrent,
        "agents": [{"import_path": args.agent, "model_name": args.model, "skills": args.skill, "kwargs": dict(a.split("=", 1) for a in args.ak)}],
        "tasks": [{"path": args.path}],
    }
    (job_dir / "config.json").write_text(json.dumps(config, indent=2))
    _run_job(job_dir, config)
    print(f"fake harbor: wrote {args.n_attempts} trials to {job_dir}")


if __name__ == "__main__":
    main()
