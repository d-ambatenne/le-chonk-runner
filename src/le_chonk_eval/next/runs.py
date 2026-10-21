"""Starting and following the next phase's executions through the Workflows API: from your Mac with your key,
and from the cloud workflows on behalf of the person who started them.

A watcher has two execution ids, <base>-a and <base>-b. When its history grows long, it starts the other one
with its state and stops: continue-as-new would drop the on-behalf-of identity, a new execution started through
the API gets it again (FRICTIONS.md, row 27).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .names import CONTROL

__all__ = ["CONTROL", "FINISHED", "execution_status", "generation_ids", "slack_watch_id", "start_execution", "start_watcher",
           "status_name", "tasks_watch_id", "vibe_run_id"]
FINISHED = {"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}
GENERATIONS = ("a", "b")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_id(value: str) -> str:
    """Execution ids take letters, digits, - and _ only. A changed name gets a short hash, so two can't meet."""
    cleaned = _UNSAFE.sub("-", value).strip("-").lower()
    if cleaned != value.lower():
        cleaned += "-" + hashlib.sha256(value.encode()).hexdigest()[:6]
    return cleaned


def slack_watch_id(channel: str) -> str:
    return f"le-chonk-slack-{safe_id(channel)}"


def tasks_watch_id(repo: str) -> str:
    return f"le-chonk-tasks-{safe_id(repo.replace('/', '-'))}"


def vibe_run_id(repo: str, run_id: str) -> str:
    """The execution id of a run on your Mac: the run id is per repo, the execution id is per account."""
    return f"le-chonk-vibe-{safe_id(repo.replace('/', '-'))}-{run_id}"


def generation_ids(base: str) -> list[str]:
    return [f"{base}-{generation}" for generation in GENERATIONS]


def other_generation(generation: str) -> str:
    return GENERATIONS[1 - GENERATIONS.index(generation)] if generation in GENERATIONS else GENERATIONS[0]


def status_name(value: Any) -> str:
    """RUNNING, COMPLETED and so on, from the API's enum or string."""
    return str(getattr(value, "value", value) or "").split(".")[-1].upper()


async def start_execution(client: Any, workflow: str, execution_id: str, deployment: str, params: dict[str, Any]) -> str:
    """Start the workflow on that deployment, unless this execution is running already."""
    try:
        current = await client.workflows.executions.get_workflow_execution_async(execution_id=execution_id)
        if status_name(current.status) == "RUNNING":
            return f"already running ({execution_id})"
    except Exception:
        pass  # never started, or not visible: start it
    try:
        await client.workflows.execute_workflow_async(workflow_identifier=workflow, execution_id=execution_id, deployment_name=deployment,
                                                      input=params)
    except Exception as exc:
        if "already" in str(exc).lower():
            return f"already running ({execution_id})"
        raise
    return f"started ({execution_id})"


async def start_watcher(client: Any, workflow: str, base: str, deployment: str, params: dict[str, Any]) -> str:
    """Start a watcher's first generation, unless one of its generations is running."""
    for execution in generation_ids(base):
        try:
            current = await client.workflows.executions.get_workflow_execution_async(execution_id=execution)
        except Exception:
            continue
        if status_name(current.status) == "RUNNING":
            return f"already running ({execution})"
    first = generation_ids(base)[0]
    return await start_execution(client, workflow, first, deployment, {**params, "generation": GENERATIONS[0], "fresh": True})


def _result(value: Any) -> Any:
    """A workflow's result as the API returns it: maybe JSON text, maybe in the {"result": ...} the SDK stores."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, dict) and set(value) == {"result"}:
        return value["result"]
    return value


async def execution_status(client: Any, execution_id: str) -> dict[str, Any]:
    execution = await client.workflows.executions.get_workflow_execution_async(execution_id=execution_id)
    status = status_name(execution.status)
    return {"status": status, "result": _result(execution.result) if status == "COMPLETED" else None}
