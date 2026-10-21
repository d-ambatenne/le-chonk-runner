"""`le-chonk next`: your Mac's worker, and the cloud watchers started and stopped from here.

    le-chonk next worker                          this Mac becomes the worker of le-chonk-vibe
    le-chonk next start --channel le-chonk --repo https://github.com/<you>/le-chonk-game
    le-chonk next stop --channel le-chonk --repo https://github.com/<you>/le-chonk-game

`start` runs with your API key, so the watchers run on your behalf: with your GitHub and Slack connectors.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .names import VIBE
from .runs import CONTROL, generation_ids, slack_watch_id, start_watcher, tasks_watch_id

CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{6,}$")


def run_worker(deployment: str | None = None) -> None:
    """This Mac as the worker of le-chonk-vibe: Docker, git and the GitHub token are here."""
    import asyncio

    if not os.environ.get("MISTRAL_API_KEY"):
        raise SystemExit("MISTRAL_API_KEY is not set. Put it in .env (see .env.example).")
    if not os.environ.get("GITHUB_TOKEN"):
        raise SystemExit("GITHUB_TOKEN is not set: the worker clones, pushes and releases with it (see docs/next.md).")
    from ..workflow.pipeline import use_deployment

    use_deployment(deployment or VIBE)  # a worker registers under a deployment name, or it doesn't start
    import mistralai.workflows as workflows

    from .vibe import WORKFLOWS

    asyncio.run(workflows.run_worker(WORKFLOWS))


def _mistral() -> Any:
    from ..demo.runner import _mistral

    return _mistral()


async def find_channel(client: Any, channel: str) -> str:
    """A channel's ID from its exact name, with the Slack connector; an ID is kept as it is."""
    from .connectors import SLACK, content_text, parse_channels

    name = channel.strip().lstrip("#")
    if CHANNEL_ID.match(name):
        return name
    result = await client.beta.connectors.call_tool_async(connector_id_or_name=SLACK, tool_name="slack_search_channels",
                                                          arguments={"keywords": [name], "limit": 20})
    found = parse_channels(content_text(result))
    exact = sorted({cid for label, cid in found if label.lower() == name.lower()})
    if len(exact) == 1:
        return exact[0]
    if not exact and len(found) == 1 and not found[0][0]:  # one channel, and no name to compare
        return found[0][1]
    # Every new message in the channel is a request for the agent: a near match could be the wrong channel
    others = ", ".join(f"#{label}" if label else cid for label, cid in found[:10])
    raise SystemExit(f"No channel is named #{name}" + (f". Found: {others}" if found else "") +
                     ". Give its exact name, or its ID (in Slack, the channel's details, at the bottom).")


async def start(channel: str, repo: str | None, options: dict[str, Any], control: str = CONTROL) -> dict[str, Any]:
    from ..demo.github import GitHub, parse_repo
    from .control import SlackInput, TasksInput

    client = _mistral()
    channel_id = await find_channel(client, channel)
    started: dict[str, Any] = {"channel": channel_id}
    if repo:
        repo = parse_repo(repo)
        if os.environ.get("GITHUB_TOKEN"):  # the labels' colors and descriptions: the connector has no tool for them
            async with GitHub() as github:
                await github.ensure_labels(repo)
        started["tasks"] = await start_watcher(client, "le-chonk-tasks", tasks_watch_id(repo), control,
                                               TasksInput(repo=repo, **options).model_dump())
    started["slack"] = await start_watcher(client, "le-chonk-slack", slack_watch_id(channel_id), control,
                                           SlackInput(channel=channel_id, repo=repo, **options).model_dump())
    return started


async def stop(channel: str | None, repo: str | None) -> dict[str, str]:
    from ..demo.github import parse_repo

    from .runs import status_name

    client = _mistral()
    bases = []
    if channel:
        bases.append(slack_watch_id(await find_channel(client, channel)))
    if repo:
        bases.append(tasks_watch_id(parse_repo(repo)))
    stopped = {}
    for base in bases:  # a watcher runs as <base>-a or <base>-b
        for execution in generation_ids(base):
            try:
                current = await client.workflows.executions.get_workflow_execution_async(execution_id=execution)
            except Exception:
                continue
            if status_name(current.status) != "RUNNING":
                continue
            try:
                await client.workflows.executions.cancel_workflow_execution_async(execution_id=execution)
                stopped[execution] = "stopped"
            except Exception as exc:
                stopped[execution] = f"not stopped: {str(exc)[:200]}"
        if not any(key.startswith(base) for key in stopped):
            stopped[base] = "not running"
    return stopped
