"""The Harbor commands for one arm: start its job, or resume it.

One arm is one Harbor job: the same task, agent and model, with or without --skill. The A/B uses the
Studio agent on tasks/le-chonk-runner; the demo uses Harbor's Vibe agent, with the two model settings it leaves out
(agents/vibe_agent.py), on a task made from an issue.
"""

from __future__ import annotations

from pathlib import Path

from ..utils.paths import env_file, harbor_bin, jobs_dir, skill_dir, task_dir
from ..utils.results import ARMS

AGENT = "le_chonk_eval.agents.studio_agent:StudioAgent"
VIBE = "le_chonk_eval.agents.vibe_agent:LeChonkVibe"
# The two agents name their cost limit differently.
COST_LIMIT_OPTION = {AGENT: "max_cost_usd", VIBE: "max_price"}


def run_command(
    job_name: str,
    arm: str,
    model: str = "mistral/mistral-large-4",
    attempts: int = 8,
    concurrency: int = 1,
    max_cost_usd: float | None = None,
    agent_options: list[str] | None = None,
    harbor: list[str] | None = None,
    *,
    task: Path | str | None = None,
    agent: str = AGENT,
    skills: list[str] | None = None,
    agent_env: list[str] | None = None,
) -> list[str]:
    """`harbor run` for one arm. The skill arm differs only by --skill: the A/B's skill folder unless `skills`
    names others (local folders or GitHub URLs, which Harbor downloads)."""
    if arm not in ARMS:
        raise ValueError(f"Unknown arm {arm!r}: use one of {', '.join(ARMS)}")
    command = [
        *(harbor or harbor_bin()), "run",
        "-p", str(task or task_dir()),
        "-a", agent,
        "-m", model,
        "-k", str(attempts),
        "-n", str(concurrency),
        "--job-name", job_name,
        "-o", str(jobs_dir()),
        "--yes",
        "--quiet",
    ]
    if env_file().is_file():
        command += ["--env-file", str(env_file())]
    if arm == "skill":
        for skill in skills if skills is not None else [str(skill_dir())]:
            command += ["--skill", skill]
    if max_cost_usd is not None:
        command += ["--ak", f"{COST_LIMIT_OPTION.get(agent, 'max_cost_usd')}={max_cost_usd:g}"]
    for option in agent_options or []:
        command += ["--ak", option]
    for variable in agent_env or []:
        command += ["--ae", variable]
    return command


def resume_command(job_dir: Path, harbor: list[str] | None = None) -> list[str]:
    return [*(harbor or harbor_bin()), "job", "resume", "-p", str(job_dir)]
