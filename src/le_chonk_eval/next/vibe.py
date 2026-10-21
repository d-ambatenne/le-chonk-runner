"""The next phase on your Mac: the workflows that need Docker, git and the GitHub token.

le-chonk-vibe-run   one Vibe run in the task's container. If the APK builds and the shared tests pass, the
                    branch vibe/<run id> is pushed and a release gets the APK. The cloud makes the PR, judges
                    the change and merges it.
le-chonk-vibe-ab    the skill A/B: k runs without the issue's skills and k with them, judged here, where the
                    changes are; a pre-release gets the report and one APK per arm.

They run on the le-chonk-vibe deployment: `uv run le-chonk next worker` makes this Mac its worker, and
le-chonk-tasks starts them through the Workflows API. Labels, comments and Slack are the cloud's: these
workflows only return what happened.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import mistralai.workflows as workflows
from pydantic import BaseModel, Field

from ..demo.workflows import DEFAULT_MODEL, REWARD_NOTE, JudgeInput, _cost_limit, _error, _task, judge_issue_jobs, make_github
from ..utils.paths import jobs_dir, runs_dir
from ..workflow.harbor_jobs import VIBE
from ..workflow.pipeline import HarborJobInput, run_harbor_job


class VibeInput(BaseModel):
    repo: str
    number: int
    run_id: str
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 3.0
    ab_attempts: int = 3
    smoke: bool = False


class ReleaseInput(BaseModel):
    repo: str
    number: int
    run_id: str
    base: str
    branch: str
    jobs: list[str]
    errors: list[str] = Field(default_factory=list)


@workflows.activity(start_to_close_timeout=timedelta(minutes=30), retry_policy_max_attempts=3)
async def prepare_vibe(params: VibeInput) -> dict[str, Any]:
    """Clone the repo at its default branch and make the Harbor task from the issue."""
    from ..demo import github as gh
    from ..demo.task_builder import build_task

    github = make_github()
    try:
        _, task = await _task(github, params.repo, params.number)
        info = await github.repo(params.repo)
        branch = info.get("default_branch") or "main"
        clone = runs_dir() / params.run_id / "repo"
        base = await asyncio.to_thread(gh.clone, params.repo, clone, github.token, branch)
        if base is None:
            base = await asyncio.to_thread(gh.initial_commit, clone, github.token, branch, info.get("name") or params.repo)
        empty = await asyncio.to_thread(gh.is_fresh, clone)  # also on a retry, after the first commit was pushed
        task_dir = await asyncio.to_thread(build_task, params.repo, params.number, task, clone, runs_dir() / params.run_id / "task", empty)
    finally:
        await github.close()
    model = task.model or params.model
    max_cost, agent_env, cost_note = _cost_limit(model, params.max_cost_usd)
    return {"task_dir": str(task_dir), "base": base, "branch": branch, "skills": task.skills, "model": model,
            "ab_attempts": task.ab_attempts or params.ab_attempts, "max_cost_usd": max_cost, "agent_env": agent_env, "note": cost_note.strip()}


@workflows.activity(start_to_close_timeout=timedelta(minutes=30), retry_policy_max_attempts=2)
async def release_vibe_run(params: ReleaseInput) -> dict[str, Any]:
    """Push Vibe's change and release its APK when it builds and passes the tests; say why not otherwise.

    Safe to retry: the clone goes back to the base commit first, the branch is force-pushed, the release is reused."""
    from ..demo.github import commit_all, head, push, reset_to, sync_tree
    from ..demo.publish import APK_TYPE, _apk, _facts, _why_not_merged
    from ..utils.results import read_job

    runs = read_job(jobs_dir() / params.jobs[0])
    if not runs:
        raise RuntimeError(f"No finished trial in {params.jobs[0]}" + (f": {params.errors[0]}" if params.errors else ""))
    run = runs[0]
    checks = run.get("assertions") or {}
    project = Path(run["project_dir"]) if run.get("project_dir") else None
    result: dict[str, Any] = {"ok": True, "run_id": params.run_id, "base": params.base, "base_branch": params.branch, "facts": _facts(run),
                              "assertions": checks, "reward": run.get("reward"), "cost_usd": run.get("cost_usd")}
    if not (checks.get("android_apk") and checks.get("shared_tests_pass") and project and project.is_dir()):
        return {**result, "passed": False, "reason": _why_not_merged(run)}

    clone = runs_dir() / params.run_id / "repo"
    branch = f"vibe/{params.run_id}"
    github = make_github()
    try:
        _, task = await _task(github, params.repo, params.number)
        await asyncio.to_thread(reset_to, clone, params.base)
        await asyncio.to_thread(sync_tree, project, clone)
        commit = await asyncio.to_thread(commit_all, clone, f"#{params.number}: {task.title}\n\nImplemented by Vibe, run {params.run_id}.")
        if commit is None:
            return {**result, "passed": True, "changed": False}
        await asyncio.to_thread(push, clone, branch, github.token)
        sha = await asyncio.to_thread(head, clone)
        release = await github.ensure_release(params.repo, params.run_id, sha, f"#{params.number}: {task.title}",
                                              f"Built by Vibe for #{params.number}, run `{params.run_id}`, from branch `{branch}`. "
                                              f"The APK builds and the shared tests pass. {result['facts']}.")
        apk_url = None
        apk = _apk(run)
        if apk:
            asset = await github.upload_asset(params.repo, release, apk, f"le-chonk-issue-{params.number}.apk", APK_TYPE)
            apk_url = asset.get("browser_download_url")
    finally:
        await github.close()
    return {**result, "passed": True, "changed": True, "branch": branch, "commit": sha, "release": release.get("html_url"), "apk": apk_url}


@workflows.activity(start_to_close_timeout=timedelta(minutes=30), retry_policy_max_attempts=2)
async def release_vibe_ab(params: ReleaseInput) -> dict[str, Any]:
    """The A/B's comparison and its pre-release with the report and one APK per arm."""
    from ..demo.publish import release_ab
    from ..utils.compare import compact, compare
    from ..utils.results import read_job

    job_dirs = [jobs_dir() / job for job in params.jobs]
    runs = [run for job_dir in job_dirs for run in read_job(job_dir)]
    if not runs:
        raise RuntimeError("no trial of the A/B finished" + (f": {'; '.join(params.errors)}" if params.errors else ""))
    github = make_github()
    try:
        _, task = await _task(github, params.repo, params.number)
        summary = await asyncio.to_thread(compare, job_dirs, params.run_id, title=f"#{params.number}: {task.title}, skill A/B",
                                          agent_label="Vibe", reward_note=REWARD_NOTE)
        released = await release_ab(github, params.repo, params.number, task, params.run_id, summary, runs, params.base, params.errors)
    finally:
        await github.close()
    return {"ok": True, **compact(summary), **{k: v for k, v in released.items() if k != "report"}, "errors": params.errors}


@workflows.workflow.define(
    name="le-chonk-vibe-run",
    workflow_display_name="Le Chonk: Vibe implements an issue on your Mac",
    workflow_description="Runs Vibe on one issue in the task's container; if the APK builds and the tests pass, pushes the branch and releases the APK.",
    execution_timeout=timedelta(days=2),
    search_keys=["repo", "number"],
)
class VibeRun:
    @workflows.workflow.entrypoint
    async def run(self, params: VibeInput) -> dict[str, Any]:
        try:
            prepared = await prepare_vibe(params)
            job = HarborJobInput(
                ab_id=params.run_id, arm="skill" if prepared["skills"] else "no-skill", model=prepared["model"], attempts=1,
                max_cost_usd=prepared["max_cost_usd"], agent_env=prepared["agent_env"], smoke=params.smoke, task=prepared["task_dir"],
                agent=VIBE, skills=prepared["skills"], job=params.run_id,
            )
            await run_harbor_job(job)
            result = await release_vibe_run(ReleaseInput(repo=params.repo, number=params.number, run_id=params.run_id,
                                                         base=prepared["base"], branch=prepared["branch"], jobs=[job.job_name]))
            return {**result, "model": prepared["model"], "note": prepared["note"]}
        except Exception as exc:
            return {"ok": False, "error": _error(exc)}


@workflows.workflow.define(
    name="le-chonk-vibe-ab",
    workflow_display_name="Le Chonk: skill A/B on your Mac",
    workflow_description="Runs Vibe on one issue k times without its skills and k times with them, judges every run, and releases the report and APKs.",
    execution_timeout=timedelta(days=3),
    search_keys=["repo", "number"],
)
class VibeAb:
    @workflows.workflow.entrypoint
    async def run(self, params: VibeInput) -> dict[str, Any]:
        try:
            prepared = await prepare_vibe(params)
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
            result = await release_vibe_ab(ReleaseInput(repo=params.repo, number=params.number, run_id=params.run_id, base=prepared["base"],
                                                        branch=prepared["branch"], jobs=names, errors=errors))
            return {**result, "model": prepared["model"], "note": prepared["note"]}
        except Exception as exc:
            return {"ok": False, "error": _error(exc)}


WORKFLOWS = [VibeRun, VibeAb]
