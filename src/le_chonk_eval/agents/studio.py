"""Studio agents created once: their ids are kept per name, and a new version is made only when the
configuration changes. Used by the A/B's builder agents and by the demo's triage agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from ..utils.paths import state_dir

_locks: dict[int, asyncio.Lock] = {}


def state_path() -> Path:
    return state_dir() / "studio-agents.json"


def _lock() -> asyncio.Lock:
    loop = id(asyncio.get_running_loop())
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def kept_agent(name: str) -> dict[str, Any] | None:
    """What .state/studio-agents.json keeps for the agent `name`: its id and configuration, or None."""
    return _read(state_path()).get(name)


async def ensure_studio_agent(client: Any, name: str, *, model: str, instructions: str, tools: list[dict[str, Any]],
                              description: str, completion_args: dict[str, Any] | None = None,
                              logger: logging.Logger | None = None, store: dict[str, Any] | None = None) -> str:
    """The id of the Studio agent `name` with this configuration: created, updated or reused.

    The ids are kept in .state/studio-agents.json, or in `store` when given: a workflow keeps them in its state."""
    config = {"model": model, "instructions": instructions, "tools": tools, "completion_args": completion_args}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    async with _lock():
        path = state_path()
        known = (store if store is not None else _read(path)).get(name) or {}
        agent_id = known.get("id")
        if agent_id and known.get("config") == digest:
            return agent_id
        fields: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "tools": tools,
            "description": description,
            "metadata": {"le_chonk_config": digest},
        }
        if completion_args:
            fields["completion_args"] = completion_args
        agent = None
        if agent_id:
            try:
                agent = await client.beta.agents.update_async(agent_id=agent_id, name=name, **fields)
            except Exception as exc:  # deleted in Studio, for example: create a new one
                (logger or logging.getLogger(__name__)).warning(f"Updating Studio agent {agent_id} failed ({exc}); creating a new one")
        if agent is None:
            agent = await client.beta.agents.create_async(name=name, **fields)
        entry = {"id": agent.id, "config": digest, "model": model, "version": getattr(agent, "version", None)}
        if store is not None:
            store[name] = entry
            return agent.id
        # Read again before writing, so another process's agents in the same file are kept.
        state = _read(path)
        state[name] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(path)
        return agent.id
