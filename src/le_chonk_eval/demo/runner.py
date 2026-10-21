"""The demo's commands: `le-chonk demo` runs the worker and the Slack app; `le-chonk watch` starts watching a repo.

Cloud mode (the default) runs the workflows on Mistral Workflows: this Mac is their worker, and the Slack
app starts a le-chonk-issues execution for each repo it's given. Local mode (--local) runs the same
workflows in this process instead, to try the demo without Workflows.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .workflows import WatchInput, watcher_id

log = logging.getLogger(__name__)
_local_watchers: dict[str, asyncio.Task] = {}


def _mistral() -> Any:
    from mistralai.client import Mistral
    from mistralai.client.utils import BackoffStrategy, RetryConfig

    if not os.environ.get("MISTRAL_API_KEY"):
        raise SystemExit("MISTRAL_API_KEY is not set. Put it in .env (see .env.example).")
    retry = RetryConfig("backoff", BackoffStrategy(1_000, 30_000, 1.8, 300_000), True, ["429", "5XX"])
    return Mistral(api_key=os.environ["MISTRAL_API_KEY"], retry_config=retry)


async def start_watcher_cloud(params: WatchInput, client: Any | None = None) -> str:
    """Start le-chonk-issues for the repo on Mistral Workflows, unless it's running already."""
    from ..workflow.pipeline import DEMO_DEPLOYMENT

    client = client or _mistral()
    execution = watcher_id(params.repo)
    deployment = os.environ.get("DEPLOYMENT_NAME") or DEMO_DEPLOYMENT
    try:
        current = await client.workflows.executions.get_workflow_execution_async(execution_id=execution)
        if "RUNNING" in str(getattr(current, "status", "")).upper():
            return f"already running (Workflows execution {execution})"
    except Exception:
        pass  # never started, or not visible: start it
    try:
        await client.workflows.execute_workflow_async(workflow_identifier="le-chonk-issues", execution_id=execution, deployment_name=deployment,
                                                      input=params.model_dump())
    except Exception as exc:
        if "already" in str(exc).lower():
            return f"already running (Workflows execution {execution})"
        return f"couldn't start the watcher: {str(exc)[:300]}"
    return f"started (Workflows execution {execution})"


async def start_watcher_local(params: WatchInput) -> str:
    """Run le-chonk-issues for the repo in this process."""
    import mistralai.workflows as workflows

    from .workflows import IssueWatcher

    running = _local_watchers.get(params.repo)
    if running and not running.done():
        return "already running in this process"
    _local_watchers[params.repo] = asyncio.create_task(workflows.execute_workflow(IssueWatcher, params))
    return "started in this process (local mode)"


async def run_demo(*, slack: bool = True, local: bool = False, options: dict[str, Any] | None = None) -> None:
    options = options or {}

    async def start_watcher(repo: str) -> str:
        params = WatchInput(repo=repo, **options)
        return await (start_watcher_local(params) if local else start_watcher_cloud(params, client))

    client = _mistral()
    tasks = []
    if not local:
        import mistralai.workflows as workflows

        from ..workflow.pipeline import HarborAb, use_deployment
        from .workflows import WORKFLOWS

        use_deployment()
        tasks.append(workflows.run_worker([HarborAb, *WORKFLOWS]))
    if slack:
        from .github import GitHub
        from .slack_bot import run_slack
        from .triage import Triage

        if not os.environ.get("GITHUB_TOKEN"):
            raise SystemExit("GITHUB_TOKEN is not set: see docs/demo.md for the token.")
        tasks.append(run_slack(Triage(client, GitHub(), start_watcher)))
    if not tasks:
        raise SystemExit("Nothing to run: local mode needs the Slack app.")
    await asyncio.gather(*tasks)


async def watch(repo: str, *, local: bool = False, options: dict[str, Any] | None = None) -> Any:
    from .github import parse_repo

    params = WatchInput(repo=parse_repo(repo), **(options or {}))
    if local:
        import mistralai.workflows as workflows

        from .workflows import IssueWatcher

        return await workflows.execute_workflow(IssueWatcher, params)
    return await start_watcher_cloud(params)
