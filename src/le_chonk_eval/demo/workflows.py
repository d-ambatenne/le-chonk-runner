"""Mistral Workflows for the demo: watch a repo's issues, and let Vibe implement or A/B them, one at a time.

le-chonk-issues (the repo is its parameter; runs for weeks, with continue-as-new)
    find_issue_work          open issues labelled vibe:ready or ab:requested, oldest first
    le-chonk-issue-run       for vibe:ready: one Vibe run, judged, then merged and released if it builds
        prepare_issue          clone, base commit, the Harbor task made from the issue, labels, Slack
        run_harbor_job         Harbor's Vibe agent in the task's container, with the issue's skills
        judge_issue_jobs       the judge's verdict on the change
        publish_issue_run      PR, merge, release with the APK, comments, labels, Slack
    le-chonk-issue-ab        for ab:requested: k runs without the issue's skills and k with them
        prepare_issue, run_harbor_job twice in parallel, judge_issue_jobs, publish_issue_ab (report, APKs)

Issues are handled one at a time, so each run starts from the main branch the last one merged into.
The activities run on the worker, this Mac: Docker, git and the GitHub token are here.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

import mistralai.workflows as workflows
from pydantic import BaseModel, Field

from ..utils.paths import jobs_dir, runs_dir
from ..workflow.harbor_jobs import VIBE
from ..workflow.pipeline import HarborJobInput, _now, run_harbor_job

DEFAULT_MODEL = "mistral/mistral-large-4"
REWARD_NOTE = ("Reward = the mean of three checks: the Android APK builds, the shared tests pass, and the judge's share of the "
               "issue's acceptance criteria met. The skill's eight toolchain assertions are shown too.")


def _in_workflow() -> bool:
    import temporalio.workflow

    return temporalio.workflow.in_workflow()


async def _sleep(seconds: float) -> None:
    if _in_workflow():
        await workflows.workflow.sleep(timedelta(seconds=seconds))
    else:
        await asyncio.sleep(seconds)


def make_github() -> Any:
    from .github import GitHub

    return GitHub()


def make_judge_client() -> Any:
    from mistralai.client import Mistral
    from mistralai.client.utils import BackoffStrategy, RetryConfig

    retry = RetryConfig("backoff", BackoffStrategy(2_000, 60_000, 1.8, 600_000), True, ["429", "5XX"])
    return Mistral(api_key=os.environ.get("MISTRAL_API_KEY"), retry_config=retry)


class SmokeJudge:
    """The judge of a smoke run: no model call. A change meets every criterion."""

    def __init__(self) -> None:
        self.chat = self

    async def complete_async(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        content = '{"criteria": [{"met": true, "evidence": "smoke run"}, {"met": true}, {"met": true}, {"met": true}, {"met": true}], ' \
                  '"verdict": "done", "summary": "Smoke run: the fake judge accepts every change."}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=None)


# --- Inputs ---------------------------------------------------------------------------------------------------

class WatchInput(BaseModel):
    repo: str
    poll_seconds: int = 60
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 3.0
    ab_attempts: int = 3
    smoke: bool = False
    polls: int = 0
    max_polls: int | None = None
    handled: int = 0


class IssueInput(BaseModel):
    repo: str
    number: int
    kind: str = "run"
    run_id: str
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 3.0
    ab_attempts: int = 3
    smoke: bool = False


class JudgeInput(BaseModel):
    repo: str
    number: int
    run_id: str
    jobs: list[str]
    smoke: bool = False


class PublishInput(BaseModel):
    repo: str
    number: int
    run_id: str
    base: str = ""
    jobs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class FailInput(BaseModel):
    repo: str
    number: int
    kind: str
    run_id: str
    error: str


class FindInput(BaseModel):
    repo: str


def watcher_id(repo: str) -> str:
    return "le-chonk-issues-" + repo.replace("/", "-").lower()


# --- Activities -----------------------------------------------------------------------------------------------

async def _task(github: Any, repo: str, number: int) -> Any:
    from .issues import Task

    issue = await github.issue(repo, number)
    return issue, Task.from_issue(issue["title"], issue.get("body"))


@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=3)
async def find_issue_work(params: FindInput) -> list[dict[str, Any]]:
    """Open issues waiting for Vibe, oldest first."""
    from .issues import work_kind

    github = make_github()
    try:
        issues = await github.issues(params.repo, state="open")
    finally:
        await github.close()
    work = [{"number": i["number"], "kind": kind, "title": i["title"]} for i in issues if (kind := work_kind(i))]
    return sorted(work, key=lambda w: w["number"])


@workflows.activity(start_to_close_timeout=timedelta(minutes=30), retry_policy_max_attempts=3)
async def prepare_issue(params: IssueInput) -> dict[str, Any]:
    """Mark the issue as taken, clone the repo at its base commit, and make the Harbor task."""
    from . import github as gh
    from .issues import AB_REQUESTED, AB_RUNNING, READY, RUNNING
    from .notify import make_notifier
    from .task_builder import build_task

    github = make_github()
    try:
        issue, task = await _task(github, params.repo, params.number)
        info = await github.repo(params.repo)
        if params.kind == "ab":
            await github.relabel(params.repo, params.number, add=[AB_RUNNING], remove=[AB_REQUESTED])
        else:
            await github.relabel(params.repo, params.number, add=[RUNNING], remove=[READY])
        branch = info.get("default_branch") or "main"
        clone = runs_dir() / params.run_id / "repo"
        base = await asyncio.to_thread(gh.clone, params.repo, clone, github.token, branch)
        if base is None:
            base = await asyncio.to_thread(gh.initial_commit, clone, github.token, branch, info.get("name") or params.repo)
        empty = await asyncio.to_thread(gh.is_fresh, clone)  # also on a retry, after the first commit was pushed
        task_dir = await asyncio.to_thread(build_task, params.repo, params.number, task, clone, runs_dir() / params.run_id / "task", empty)
        model = task.model or params.model
        max_cost, agent_env, cost_note = _cost_limit(model, params.max_cost_usd)
        what = (f"the skill A/B, {task.ab_attempts or params.ab_attempts} runs per arm" if params.kind == "ab"
                else "the implementation" + (f" with {len(task.skills)} skill(s)" if task.skills else ""))
        await github.comment(params.repo, params.number, f"Started {what}: run `{params.run_id}`, Vibe on {model}, from {base[:8]}.{cost_note}")
        await make_notifier().post(task.slack_channel, task.slack_thread, f"Started {what} of #{params.number} {task.title} (Vibe on {model}).")
    finally:
        await github.close()
    return {"task_dir": str(task_dir), "clone_dir": str(clone), "base": base, "branch": branch, "empty": empty,
            "skills": task.skills, "model": model, "ab_attempts": task.ab_attempts or params.ab_attempts, "title": task.title,
            "max_cost_usd": max_cost, "agent_env": agent_env}


def _cost_limit(model: str, max_cost: float | None) -> tuple[float | None, list[str], str]:
    """Vibe enforces --max-price from the model's prices: give it ours, or run without a limit if none is known."""
    from ..agents.studio_agent import _price_per_token

    if max_cost is None or (os.environ.get("VIBE_INPUT_PRICE") and os.environ.get("VIBE_OUTPUT_PRICE")):
        return max_cost, [], ""
    price_in, price_out = _price_per_token(model.split("/", 1)[-1])
    if not price_in or not price_out:
        return None, [], f" No price is known for {model}, so the run has no cost limit (set LECHONK_PRICE_INPUT and LECHONK_PRICE_OUTPUT)."
    return max_cost, [f"VIBE_INPUT_PRICE={price_in * 1e6:g}", f"VIBE_OUTPUT_PRICE={price_out * 1e6:g}"], ""


@workflows.activity(start_to_close_timeout=timedelta(minutes=60), retry_policy_max_attempts=2)
async def judge_issue_jobs(params: JudgeInput) -> list[dict[str, Any]]:
    """The judge's verdict for every finished trial of the jobs, in judge.json next to each trial's result."""
    from ..utils.results import trial_dirs
    from .judge import judge_trial

    github = make_github()
    try:
        _, task = await _task(github, params.repo, params.number)
    finally:
        await github.close()
    client = SmokeJudge() if params.smoke else make_judge_client()
    model = os.environ.get("LECHONK_JUDGE_MODEL") or DEFAULT_MODEL
    clone = runs_dir() / params.run_id / "repo"
    verdicts = []
    for job in params.jobs:
        for trial in trial_dirs(jobs_dir() / job):
            result = await judge_trial(client, model, task, trial, clone)
            if result:
                verdicts.append({"trial": trial.name, "verdict": result["verdict"], "score": result["score"]})
    return verdicts


@workflows.activity(start_to_close_timeout=timedelta(minutes=30), retry_policy_max_attempts=2)
async def publish_issue_run(params: PublishInput) -> dict[str, Any]:
    from ..utils.results import read_job
    from .notify import make_notifier
    from .publish import publish_run

    runs = read_job(jobs_dir() / params.jobs[0])
    if not runs:
        raise RuntimeError(f"No finished trial in {params.jobs[0]}" + (f": {params.errors[0]}" if params.errors else ""))
    github = make_github()
    try:
        _, task = await _task(github, params.repo, params.number)
        return await publish_run(github, make_notifier(), params.repo, params.number, task, params.run_id, runs[0],
                                 runs_dir() / params.run_id / "repo", f"vibe/{params.run_id}", github.token, params.base)
    finally:
        await github.close()


@workflows.activity(start_to_close_timeout=timedelta(minutes=30), retry_policy_max_attempts=2)
async def publish_issue_ab(params: PublishInput) -> dict[str, Any]:
    from ..utils.compare import compact, compare
    from ..utils.results import read_job
    from .github import head
    from .notify import make_notifier
    from .publish import publish_ab

    job_dirs = [jobs_dir() / job for job in params.jobs]
    runs = [run for job_dir in job_dirs for run in read_job(job_dir)]
    if not runs:
        raise RuntimeError("no trial of the A/B finished" + (f": {'; '.join(params.errors)}" if params.errors else ""))
    github = make_github()
    try:
        _, task = await _task(github, params.repo, params.number)
        summary = await asyncio.to_thread(compare, job_dirs, params.run_id, title=f"#{params.number}: {task.title}, skill A/B",
                                          agent_label="Vibe", reward_note=REWARD_NOTE)
        target = params.base or head(runs_dir() / params.run_id / "repo")
        published = await publish_ab(github, make_notifier(), params.repo, params.number, task, params.run_id, summary, runs, target,
                                     params.errors)
    finally:
        await github.close()
    return {**compact(summary), **published, "errors": params.errors}


@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=3)
async def fail_issue(params: FailInput) -> None:
    from .notify import make_notifier
    from .publish import report_failure

    github = make_github()
    try:
        try:
            _, task = await _task(github, params.repo, params.number)
        except Exception:
            task = None
        await report_failure(github, make_notifier(), params.repo, params.number, task, params.kind, params.run_id, params.error)
    finally:
        await github.close()


# --- Workflows ------------------------------------------------------------------------------------------------

def _error(exc: BaseException) -> str:
    cause = exc
    while getattr(cause, "__cause__", None) is not None:  # an activity's error is the cause of ActivityError
        cause = cause.__cause__
    return f"{type(cause).__name__}: {cause}"[:2000]


@workflows.workflow.define(
    name="le-chonk-issue-run",
    workflow_display_name="Le Chonk: Vibe implements an issue",
    workflow_description="Runs Vibe on one issue in the task's container, judges the change, and merges and releases it if it builds.",
    # Longer than the Harbor activity with its retries, so a failed run always reaches its cleanup.
    execution_timeout=timedelta(days=3),
    search_keys=["repo", "number"],
)
class IssueRun:
    @workflows.workflow.entrypoint
    async def run(self, params: IssueInput) -> dict[str, Any]:
        try:
            prepared = await prepare_issue(params)
            job = HarborJobInput(
                ab_id=params.run_id, arm="skill" if prepared["skills"] else "no-skill", model=prepared["model"], attempts=1,
                max_cost_usd=prepared["max_cost_usd"], agent_env=prepared["agent_env"], smoke=params.smoke, task=prepared["task_dir"],
                agent=VIBE, skills=prepared["skills"], job=params.run_id, fake_pass=1.0,  # a smoke run goes all the way
            )
            await run_harbor_job(job)
            await judge_issue_jobs(JudgeInput(repo=params.repo, number=params.number, run_id=params.run_id, jobs=[job.job_name], smoke=params.smoke))
            return await publish_issue_run(PublishInput(repo=params.repo, number=params.number, run_id=params.run_id, base=prepared["base"],
                                                        jobs=[job.job_name]))
        except Exception as exc:
            error = _error(exc)
            await fail_issue(FailInput(repo=params.repo, number=params.number, kind="run", run_id=params.run_id, error=error))
            return {"ok": False, "error": error}


@workflows.workflow.define(
    name="le-chonk-issue-ab",
    workflow_display_name="Le Chonk: skill A/B on an issue",
    workflow_description="Runs Vibe on one issue k times without its skills and k times with them, judges every run, and reports.",
    execution_timeout=timedelta(days=3),
    search_keys=["repo", "number"],
)
class IssueAb:
    @workflows.workflow.entrypoint
    async def run(self, params: IssueInput) -> dict[str, Any]:
        try:
            prepared = await prepare_issue(params)
            if not prepared["skills"]:
                raise ValueError("the issue has no skill: an A/B compares Vibe without and with one")
            jobs = [
                HarborJobInput(
                    ab_id=params.run_id, arm=arm, model=prepared["model"], attempts=prepared["ab_attempts"],
                    max_cost_usd=prepared["max_cost_usd"], agent_env=prepared["agent_env"], smoke=params.smoke,
                    task=prepared["task_dir"], agent=VIBE, skills=prepared["skills"] if arm == "skill" else [], job=f"{params.run_id}-{arm}",
                )
                for arm in ("no-skill", "skill")
            ]
            outcomes = await asyncio.gather(*[run_harbor_job(job) for job in jobs], return_exceptions=True)
            errors = [f"{job.arm}: {_error(o)}" for job, o in zip(jobs, outcomes) if isinstance(o, BaseException)]
            names = [job.job_name for job in jobs]
            await judge_issue_jobs(JudgeInput(repo=params.repo, number=params.number, run_id=params.run_id, jobs=names, smoke=params.smoke))
            return await publish_issue_ab(PublishInput(repo=params.repo, number=params.number, run_id=params.run_id, base=prepared["base"],
                                                       jobs=names, errors=errors))
        except Exception as exc:
            error = _error(exc)
            await fail_issue(FailInput(repo=params.repo, number=params.number, kind="ab", run_id=params.run_id, error=error))
            return {"ok": False, "error": error}


@workflows.workflow.define(
    name="le-chonk-issues",
    workflow_display_name="Le Chonk: watch a repo's issues",
    workflow_description="Polls a GitHub repo for issues labelled vibe:ready or ab:requested, and runs le-chonk-issue-run or le-chonk-issue-ab for each, one at a time.",
    execution_timeout=timedelta(days=365),
    search_keys=["repo"],
)
class IssueWatcher:
    @workflows.workflow.entrypoint
    async def run(self, params: WatchInput) -> dict[str, Any]:
        handled: list[dict[str, Any]] = []
        while True:
            try:
                work = await find_issue_work(FindInput(repo=params.repo))
            except Exception as exc:  # GitHub down, a token expired: try again later
                handled.append({"error": _error(exc)})
                work = []
            if work:
                item = work[0]
                run_id = f"issue-{item['number']}-{'ab-' if item['kind'] == 'ab' else ''}{_now().strftime('%Y%m%d-%H%M%S')}"
                child = IssueAb if item["kind"] == "ab" else IssueRun
                issue = IssueInput(repo=params.repo, number=item["number"], kind=item["kind"], run_id=run_id, model=params.model,
                                   max_cost_usd=params.max_cost_usd, ab_attempts=params.ab_attempts, smoke=params.smoke)
                execution = f"{watcher_id(params.repo)}-{run_id}"
                try:
                    # ABANDON: if the watcher stops, the run still finishes, labels its issue and reports.
                    result = await workflows.workflow.execute_workflow(child, issue, execution_id=execution,
                                                                       parent_close_policy=workflows.ParentClosePolicy.ABANDON)
                except Exception as exc:
                    result = {"ok": False, "error": _error(exc)}
                handled.append({"number": item["number"], "kind": item["kind"], "run_id": run_id, "result": result})
                params.handled += 1
                if isinstance(result, dict) and result.get("ok") is False:
                    await _sleep(params.poll_seconds)  # so an issue that keeps failing can't spin the loop
                continue
            params.polls += 1
            if params.max_polls is not None and params.polls >= params.max_polls:
                return {"repo": params.repo, "polls": params.polls, "handled": handled}
            if _in_workflow() and workflows.workflow.should_continue_as_new():
                workflows.workflow.continue_as_new(params)
            await _sleep(params.poll_seconds)


WORKFLOWS = [IssueWatcher, IssueRun, IssueAb]
