"""Mistral Workflows: the A/B as two Harbor jobs on this Mac, then the comparison.

le-chonk-harbor-ab
    run_harbor_job(no-skill) ┐ in parallel: one Harbor job per arm, each starts its own containers
    run_harbor_job(skill)    ┘
    compare_jobs               statistics and the dashboard

Worker mode: `uv run le-chonk-worker` makes this Mac a worker; start a run with `uv run le-chonk ab --cloud`
or from Studio > Workflows. Local mode: `uv run le-chonk ab` runs the same code in this process.

Harbor needs Docker, so the activities run where Docker is: on your Mac. A retried activity resumes its
Harbor job, so trials that already finished are kept.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from datetime import datetime, timedelta, timezone
from typing import Any

import mistralai.workflows as workflows
from pydantic import BaseModel, Field

from ..utils.paths import fake_harbor, jobs_dir, project_root, results_dir
from ..utils.results import ARMS, job_status
from .harbor_jobs import AGENT, resume_command, run_command


DEMO_DEPLOYMENT = "le-chonk"


def use_deployment(name: str | None = None) -> str:
    """The deployment this Mac's worker registers under, and that executions are started on: `name`, else
    DEPLOYMENT_NAME, else le-chonk. Workflows refuses to start a worker without one."""
    from mistralai.workflows.core.config.config import config

    deployment = name or os.environ.get("DEPLOYMENT_NAME") or DEMO_DEPLOYMENT
    os.environ["DEPLOYMENT_NAME"] = deployment
    config.worker.deployment_name = deployment  # the configuration was read when mistralai.workflows was imported
    return deployment


def _now() -> datetime:
    import temporalio.workflow

    if temporalio.workflow.in_workflow():
        return workflows.workflow.now()
    return datetime.now(timezone.utc)


def _heartbeat(details: Any) -> None:
    try:
        import temporalio.activity

        if temporalio.activity.in_activity():
            temporalio.activity.heartbeat(details)
    except Exception:
        pass


class HarborJobInput(BaseModel):
    ab_id: str
    arm: str
    model: str = "mistral/mistral-large-4"
    attempts: int = 8
    concurrency: int = 1
    max_cost_usd: float | None = 4.0
    agent_options: list[str] = Field(default_factory=list)
    smoke: bool = False
    # For the demo's jobs: a task made from an issue, Harbor's Vibe agent and the issue's skills.
    task: str | None = None
    agent: str = AGENT
    skills: list[str] | None = None
    agent_env: list[str] = Field(default_factory=list)
    job: str | None = None

    @property
    def job_name(self) -> str:
        return self.job or f"{self.ab_id}-{self.arm}"


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Interrupt harbor like Ctrl-C, so it stops its containers, then kill it if it hangs."""
    for sig, wait in ((signal.SIGINT, 60), (signal.SIGKILL, 10)):
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, sig)
            await asyncio.wait_for(process.wait(), timeout=wait)
        except (ProcessLookupError, asyncio.TimeoutError):
            continue


@workflows.activity(start_to_close_timeout=timedelta(hours=16), heartbeat_timeout=timedelta(minutes=10), retry_policy_max_attempts=3)
async def run_harbor_job(params: HarborJobInput) -> dict[str, Any]:
    """Run one arm as a Harbor job on this machine, or resume it if it was started before."""
    job_dir = jobs_dir() / params.job_name
    harbor = fake_harbor() if params.smoke else None
    if (job_dir / "config.json").is_file():
        command = resume_command(job_dir, harbor=harbor)
    else:
        command = run_command(
            params.job_name, params.arm, model=params.model, attempts=params.attempts, concurrency=params.concurrency,
            max_cost_usd=params.max_cost_usd, agent_options=params.agent_options, harbor=harbor,
            task=params.task, agent=params.agent, skills=params.skills, agent_env=params.agent_env,
        )
    log_path = results_dir() / params.ab_id / f"{params.job_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        log.write(f"\n$ {shlex.join(command)}\n".encode())
        log.flush()
        process = await asyncio.create_subprocess_exec(
            *command, cwd=project_root(), stdout=log, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"}, start_new_session=True,
        )
        try:
            while True:
                try:
                    await asyncio.wait_for(process.wait(), timeout=30)
                    break
                except asyncio.TimeoutError:
                    _heartbeat(job_status(job_dir))
        except asyncio.CancelledError:
            await _stop(process)
            raise
    status = job_status(job_dir)
    if not status["finished"]:
        raise RuntimeError(f"harbor exited with code {process.returncode} before {params.job_name} finished; see {log_path}")
    return {**status, "arm": params.arm, "exit_code": process.returncode, "log": str(log_path)}


class CompareInput(BaseModel):
    ab_id: str
    jobs: list[str]


@workflows.activity(start_to_close_timeout=timedelta(minutes=30), retry_policy_max_attempts=2)
async def compare_jobs(params: CompareInput) -> dict[str, Any]:
    """Read every trial of the jobs and write results/<ab id>/."""
    from ..utils.compare import compact, compare

    summary = await asyncio.to_thread(compare, [jobs_dir() / job for job in params.jobs], params.ab_id)
    return compact(summary)


class HarborAbInput(BaseModel):
    ab_id: str = ""
    model: str = "mistral/mistral-large-4"
    attempts: int = 8
    concurrency: int = 1
    max_cost_usd: float | None = 4.0
    agent_options: list[str] = Field(default_factory=list)
    smoke: bool = False


def plan_jobs(params: HarborAbInput, ab_id: str) -> list[HarborJobInput]:
    return [
        HarborJobInput(
            ab_id=ab_id, arm=arm, model=params.model, attempts=params.attempts, concurrency=params.concurrency,
            max_cost_usd=params.max_cost_usd, agent_options=params.agent_options, smoke=params.smoke,
        )
        for arm in ARMS
    ]


@workflows.workflow.define(
    name="le-chonk-harbor-ab",
    workflow_display_name="Le Chonk: Harbor skill A/B",
    workflow_description="Runs the Le Chonk Runner Harbor task with and without the kotlin-tooling-kotlin-toolchain skill, then compares the two jobs.",
    execution_timeout=timedelta(days=2),
    search_keys=["ab_id"],
)
class HarborAb:
    @workflows.workflow.entrypoint
    async def run(self, params: HarborAbInput) -> dict[str, Any]:
        ab_id = params.ab_id or "ab-" + _now().strftime("%Y%m%d-%H%M")
        jobs = plan_jobs(params, ab_id)
        outcomes = await asyncio.gather(*[run_harbor_job(job) for job in jobs], return_exceptions=True)
        summary = await compare_jobs(CompareInput(ab_id=ab_id, jobs=[job.job_name for job in jobs]))
        summary["jobs"] = [
            {"job": job.job_name, "error": f"{type(outcome).__name__}: {outcome}"[:1000]} if isinstance(outcome, BaseException) else outcome
            for job, outcome in zip(jobs, outcomes)
        ]
        return summary


def main() -> None:
    """`le-chonk-worker`: this Mac pulls work from Mistral Workflows over an outbound connection and runs it."""
    from dotenv import load_dotenv

    load_dotenv()
    from ..demo.workflows import WORKFLOWS  # the demo's workflows: le-chonk-issues and its two children

    use_deployment()
    asyncio.run(workflows.run_worker([HarborAb, *WORKFLOWS]))
