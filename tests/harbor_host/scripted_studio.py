"""Test only: the Studio agent with a scripted Mistral client, so a real Harbor trial runs without the API."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mistralai.client.models as models

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import FAKE_KOTLIN, TOOLCHAIN_PROJECT  # noqa: E402

from le_chonk_eval.agents.studio_agent import StudioAgent  # noqa: E402


def _call(i: int, name: str, **arguments: Any) -> models.FunctionCallEntry:
    return models.FunctionCallEntry(tool_call_id=f"t{i}", name=name, arguments=json.dumps(arguments))


def _script(skills_seen: bool) -> list[list[Any]]:
    writes = [_call(i, "write_file", path=path, content=content) for i, (path, content) in enumerate(TOOLCHAIN_PROJECT.items())]
    first = [_call(100, "read_file", path="/harbor/skills/kotlin-tooling-kotlin-toolchain/SKILL.md")] if skills_seen else []
    return [
        first + [_call(101, "list_files")],
        writes + [_call(102, "write_file", path="kotlin", content=FAKE_KOTLIN)],
        [_call(103, "run_command", command="chmod +x kotlin && ./kotlin build -m android-app && ./kotlin test -m shared")],
        [models.MessageOutputEntry(content="Done: ./kotlin build -m android-app, ./kotlin test -m shared")],
    ]


class ScriptedStudioAgent(StudioAgent):
    @staticmethod
    def name() -> str:
        return "mistral-studio-scripted"

    def _client(self) -> Any:
        script = _script(bool(self.skills_dir))

        async def respond(**kwargs: Any) -> models.ConversationResponse:
            return models.ConversationResponse(
                conversation_id="conv_scripted", outputs=script.pop(0),
                usage=models.ConversationUsageInfo(prompt_tokens=2000, completion_tokens=200, total_tokens=2200),
            )

        async def agent(**kwargs: Any) -> Any:
            return SimpleNamespace(id="ag_scripted", version=0)

        return SimpleNamespace(beta=SimpleNamespace(
            agents=SimpleNamespace(create_async=agent, update_async=agent),
            conversations=SimpleNamespace(start_async=respond, append_async=respond),
        ))
