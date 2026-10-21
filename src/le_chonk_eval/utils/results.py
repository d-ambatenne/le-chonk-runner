"""What a Harbor job left behind: the trials of one arm, read back from its job folder."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ARMS = ("no-skill", "skill")
ASSERTIONS = [
    "no_gradle_or_maven",
    "toolchain_config",
    "three_modules",
    "ios_targets",
    "android_apk",
    "shared_tests_pass",
    "shared_composable_in_both_apps",
    "dev_entry_point",
]
TASK_DONE = "task_done"  # the demo's judge: the issue's acceptance criteria are met
LABELS = {
    TASK_DONE: "Judge: task done",
    "no_gradle_or_maven": "No Gradle or Maven files",
    "toolchain_config": "module.yaml or project.yaml",
    "three_modules": "Shared + android/app + ios/app",
    "ios_targets": "iOS targets declared",
    "android_apk": "Android APK builds",
    "shared_tests_pass": "Shared tests pass",
    "shared_composable_in_both_apps": "Shared @Composable in both apps",
    "dev_entry_point": "@DevelopmentEntryPoint present",
}

# What the agent ran, as the skill's own eval reports it: gradle or kotlin, and whether it scaffolded with the toolchain.
GRADLE = re.compile(r"(?<![\w./~-])(?:\./)?gradlew?(?![\w.-])")
KOTLIN = re.compile(r"(?<![\w./~-])(?:\./)?kotlin(?=\s|$)")
SCAFFOLD = re.compile(r"(?<![\w./~-])(?:\./)?kotlin\s+(?:init|new)\b")


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def trial_dirs(job_dir: Path) -> list[Path]:
    return sorted(p for p in job_dir.iterdir() if p.is_dir() and (p / "config.json").is_file()) if job_dir.is_dir() else []


def job_status(job_dir: Path) -> dict[str, Any]:
    config = _load(job_dir / "config.json") or {}
    result = _load(job_dir / "result.json") or {}
    done = sum(1 for trial in trial_dirs(job_dir) if (trial / "result.json").is_file())
    return {
        "job": job_dir.name,
        "trials_done": done,
        "trials_total": config.get("n_attempts") or result.get("n_total_trials"),
        "finished": bool(result.get("finished_at")),
    }


def _seconds(timing: dict[str, Any] | None) -> float | None:
    if not timing or not timing.get("started_at") or not timing.get("finished_at"):
        return None
    try:
        start = datetime.fromisoformat(str(timing["started_at"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(timing["finished_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((end - start).total_seconds(), 1)


def process_metrics(trajectory: dict[str, Any] | None) -> dict[str, Any]:
    """How the agent worked, from the trial's ATIF trajectory (agent/trajectory.json)."""
    if not trajectory:
        return {}
    steps = trajectory.get("steps") or []
    calls = [call for step in steps for call in (step.get("tool_calls") or [])]
    # The Studio agent's run_command, Vibe's bash: any tool called with a shell command.
    commands = [str(args["command"]) for call in calls if isinstance(args := call.get("arguments") or {}, dict) and isinstance(args.get("command"), str)]
    return {
        "model_calls": sum(1 for step in steps if step.get("source") == "agent"),
        "tool_calls": len(calls),
        "shell_commands": len(commands),
        "gradle_calls": sum(1 for command in commands if GRADLE.search(command)),
        "kotlin_calls": sum(1 for command in commands if KOTLIN.search(command)),
        "kotlin_scaffold_used": any(SCAFFOLD.search(command) for command in commands),
        "skill_md_read": any("SKILL.md" in json.dumps(call.get("arguments") or {}) for call in calls),
        "tools": dict(Counter(call.get("function_name") or "?" for call in calls)),
    }


def read_trial(trial_dir: Path, job_name: str) -> dict[str, Any] | None:
    result = _load(trial_dir / "result.json")
    if not isinstance(result, dict):
        return None  # still running
    config = (result.get("config") or _load(trial_dir / "config.json") or {}).get("agent") or {}
    agent_result = result.get("agent_result") or {}
    metadata = agent_result.get("metadata") or {}
    rewards = (result.get("verifier_result") or {}).get("rewards") or _load(trial_dir / "verifier" / "reward.json") or {}
    details = _load(trial_dir / "verifier" / "details.json") or {}
    exception = result.get("exception_info") or {}

    assertions = details.get("assertions") or {k: (bool(rewards[k]) if k in rewards else None) for k in ASSERTIONS}
    assertions = {k: assertions.get(k) for k in ASSERTIONS}
    reward = rewards.get("reward")
    judge = _load(trial_dir / "judge.json")
    if isinstance(judge, dict) and reward is not None:
        # The demo's reward: the APK builds, the tests pass, and the judge's share of criteria met.
        checks = [1.0 if assertions.get("android_apk") else 0.0, 1.0 if assertions.get("shared_tests_pass") else 0.0, float(judge.get("score") or 0)]
        reward, assertions[TASK_DONE] = round(sum(checks) / 3, 4), judge.get("verdict") == "done"
    project = trial_dir / "artifacts" / "project"
    error = None
    if exception:
        error = f"{exception.get('exception_type', 'Error')}: {exception.get('exception_message', '')}".strip()[:1000]
    elif metadata.get("error"):
        error = str(metadata["error"])[:1000]
    return {
        "trial": trial_dir.name,
        "job": job_name,
        "trial_dir": str(trial_dir),
        "arm": "skill" if config.get("skills") else "no-skill",
        "model": config.get("model_name") or "?",
        "reward": reward,
        "toolchain_reward": rewards.get("reward"),
        "assertions": assertions,
        "judge": judge if isinstance(judge, dict) else None,
        "apk": details.get("apk"),
        "input_tokens": agent_result.get("n_input_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "cost_usd": agent_result.get("cost_usd"),
        "agent_seconds": _seconds(result.get("agent_execution")),
        "process": process_metrics(_load(trial_dir / "agent" / "trajectory.json")),
        "stop_reason": metadata.get("stop_reason"),
        "error": error,
        "environment_errors": details.get("environment_errors") or [],
        "environment": details.get("environment") or {},
        "project_dir": str(project) if project.is_dir() else None,
        "studio": {k: metadata.get(k) for k in ("studio_agent", "studio_agent_id", "conversation_id", "skills")},
    }


def read_job(job_dir: Path) -> list[dict[str, Any]]:
    runs = [read_trial(trial, job_dir.name) for trial in trial_dirs(job_dir)]
    return [run for run in runs if run is not None]
