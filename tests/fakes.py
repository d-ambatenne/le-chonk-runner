"""Fakes shared by the tests: an environment that runs commands with local bash, and a scripted Mistral client."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mistralai.client.models as models
from harbor.environments.base import ExecResult


class LocalEnvironment:
    """Just the parts of Harbor's BaseEnvironment the agent uses, on the local machine."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[str] = []

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: Any = None) -> ExecResult:
        self.commands.append(command)
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=cwd or self.root, env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds")
        except asyncio.CancelledError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
            raise
        return ExecResult(stdout=out.decode() or None, stderr=err.decode() or None, return_code=process.returncode)

    async def is_dir(self, path: str, user: Any = None) -> bool:
        return Path(path).is_dir()

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        shutil.copy(source_path, target_path)


def call(tool_id: str, name: str, **arguments: Any) -> models.FunctionCallEntry:
    return models.FunctionCallEntry(tool_call_id=tool_id, name=name, arguments=json.dumps(arguments))


class FakeClient:
    def __init__(self, script: list[list[Any]], fail_after: int | None = None) -> None:
        self.script = list(script)
        self.inputs: list[Any] = []
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.fail_after = fail_after
        self.beta = SimpleNamespace(
            agents=SimpleNamespace(create_async=self._create, update_async=self._update),
            conversations=SimpleNamespace(start_async=self._start, append_async=self._append),
        )

    async def _create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return SimpleNamespace(id=f"ag_{len(self.created)}", version=0)

    async def _update(self, **kwargs: Any) -> Any:
        self.updated.append(kwargs)
        return SimpleNamespace(id=kwargs["agent_id"], version=len(self.updated))

    async def _start(self, **kwargs: Any) -> models.ConversationResponse:
        self.inputs.append(kwargs)
        return self._next()

    async def _append(self, **kwargs: Any) -> models.ConversationResponse:
        self.inputs.append(kwargs)
        if self.fail_after is not None and len(self.inputs) > self.fail_after:
            raise RuntimeError("API unavailable")
        return self._next()

    def _next(self) -> models.ConversationResponse:
        outputs = self.script.pop(0)
        return models.ConversationResponse(
            conversation_id="conv_1",
            outputs=outputs,
            usage=models.ConversationUsageInfo(prompt_tokens=1000, completion_tokens=100, total_tokens=1100),
        )
