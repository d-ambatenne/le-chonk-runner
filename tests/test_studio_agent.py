"""The Studio agent adapter: a fake Mistral client, and a fake environment that runs commands with local bash."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mistralai.client.models as models
import pytest
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import Trajectory

from conftest import FIXTURE_SKILL, write_tree
from fakes import FakeClient, LocalEnvironment, call
from le_chonk_eval.agents.studio_agent import StudioAgent, build_instructions


SCRIPT = [
    [
        models.MessageOutputEntry(content="Setting up the project."),
        call("t1", "write_file", path="shared/module.yaml", content="product: kmp/lib\n"),
        call("t2", "run_command", command="ls shared && echo built"),
    ],
    [
        call("t3", "read_file", path="shared/module.yaml"),
        call("t4", "list_files", max_depth=2),
        call("t5", "browse_web", url="https://kotlinlang.org"),
        call("t6", "run_command", command="exit 3"),
    ],
    [models.MessageOutputEntry(content="Done. Build with ./kotlin build.")],
]


def make_agent(tmp_path: Path, skills_dir: str | None = None, **kwargs: Any) -> StudioAgent:
    return StudioAgent(logs_dir=tmp_path / "logs", model_name="mistral/mistral-large-4", skills_dir=skills_dir, **kwargs)


async def test_a_run_executes_tools_and_records_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    app.mkdir()
    client = FakeClient(SCRIPT)
    agent = make_agent(tmp_path)
    monkeypatch.setattr(agent, "_client", lambda: client)
    context = AgentContext()

    await agent.run("Build Le Chonk Runner", LocalEnvironment(app), context)

    assert (app / "shared" / "module.yaml").read_text() == "product: kmp/lib\n"
    results = {entry.tool_call_id: entry.result for entry in client.inputs[1]["inputs"]}
    assert results["t1"].startswith("Created shared/module.yaml")
    assert "built" in results["t2"] and results["t2"].startswith("[exit code 0")
    second = {entry.tool_call_id: entry.result for entry in client.inputs[2]["inputs"]}
    assert "1  product: kmp/lib" in second["t3"]
    assert "shared/module.yaml" in second["t4"]
    assert second["t5"].startswith("Error: unknown tool 'browse_web'")
    assert second["t6"].startswith("[exit code 3")

    assert client.created[0]["name"] == "le-chonk-builder-no-skill"
    assert client.created[0]["model"] == "mistral-large-4"
    assert {t["function"]["name"] for t in client.created[0]["tools"]} == {"run_command", "read_file", "write_file", "list_files"}
    assert context.n_input_tokens == 3000 and context.n_output_tokens == 300
    assert context.cost_usd == pytest.approx(3000 * 0.68e-6 + 300 * 2.09e-6)
    assert context.metadata["stop_reason"] == "done" and context.metadata["conversation_id"] == "conv_1"

    trajectory = Trajectory.model_validate_json((tmp_path / "logs" / "trajectory.json").read_text())
    assert [s.source for s in trajectory.steps] == ["system", "user", "agent", "agent", "agent"]
    assert trajectory.steps[2].tool_calls[1].arguments["command"].startswith("ls shared")
    assert trajectory.final_metrics.total_prompt_tokens == 3000
    trace = [json.loads(line) for line in (tmp_path / "logs" / "trace.jsonl").read_text().splitlines()]
    assert [t["kind"] for t in trace][0] == "start" and trace[-1]["kind"] == "end"


async def test_the_studio_agent_is_created_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    app.mkdir()
    for attempt in range(2):
        client = FakeClient([[models.MessageOutputEntry(content="Nothing to do.")]])
        agent = make_agent(tmp_path / f"trial{attempt}")
        monkeypatch.setattr(agent, "_client", lambda client=client: client)
        await agent.run("Build", LocalEnvironment(app), AgentContext())
        assert len(client.created) == (1 if attempt == 0 else 0) and not client.updated
    state = json.loads((tmp_path / "state" / "studio-agents.json").read_text())
    assert state["le-chonk-builder-no-skill"]["id"] == "ag_1"

    client = FakeClient([[models.MessageOutputEntry(content="Nothing to do.")]])
    agent = make_agent(tmp_path / "trial-temperature", temperature=0.3)  # a new configuration: a new agent version
    monkeypatch.setattr(agent, "_client", lambda: client)
    await agent.run("Build", LocalEnvironment(app), AgentContext())
    assert client.updated and client.updated[0]["agent_id"] == "ag_1" and client.updated[0]["completion_args"] == {"temperature": 0.3}


async def test_skills_are_listed_and_change_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    app.mkdir()
    skills = tmp_path / "harbor-skills"
    write_tree(skills / "kotlin-tooling-kotlin-toolchain", {"SKILL.md": FIXTURE_SKILL})
    client = FakeClient([[models.MessageOutputEntry(content="Done.")]])
    agent = make_agent(tmp_path, skills_dir=str(skills))
    monkeypatch.setattr(agent, "_client", lambda: client)
    context = AgentContext()
    await agent.run("Build", LocalEnvironment(app), context)
    created = client.created[0]
    assert created["name"] == "le-chonk-builder-skill"
    assert "<available_skills>" in created["instructions"]
    assert f"{skills}/kotlin-tooling-kotlin-toolchain/SKILL.md" in created["instructions"]
    assert context.metadata["skills"] == ["kotlin-tooling-kotlin-toolchain"]


def test_both_arms_share_the_base_instructions() -> None:
    from le_chonk_eval.agents.studio_agent import Skill

    plain = build_instructions("/app", [])
    with_skill = build_instructions("/app", [Skill("s", "d", "/harbor/skills/s/SKILL.md")])
    assert with_skill.startswith(plain) and "## Skills" in with_skill and "## Skills" not in plain


async def test_an_api_failure_is_recorded_and_the_trial_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    app.mkdir()
    client = FakeClient(SCRIPT, fail_after=1)
    agent = make_agent(tmp_path)
    monkeypatch.setattr(agent, "_client", lambda: client)
    context = AgentContext()
    await agent.run("Build", LocalEnvironment(app), context)  # doesn't raise: the verifier still runs
    assert context.metadata["stop_reason"] == "error" and "API unavailable" in context.metadata["error"]
    assert (tmp_path / "logs" / "trajectory.json").is_file()


async def test_max_turns_stops_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    app.mkdir()
    looping = [[call(f"t{i}", "run_command", command="true")] for i in range(5)]
    client = FakeClient(looping)
    agent = make_agent(tmp_path, max_turns=3)
    monkeypatch.setattr(agent, "_client", lambda: client)
    context = AgentContext()
    await agent.run("Build", LocalEnvironment(app), context)
    assert context.metadata["stop_reason"] == "max_turns" and context.metadata["model_calls"] == 4


async def test_the_cost_cap_stops_the_trial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    app.mkdir()
    looping = [[call(f"t{i}", "run_command", command="true")] for i in range(10)]
    client = FakeClient(looping)
    # Each response costs 1000 * 0.68e-6 + 100 * 2.09e-6 = $0.000889; the cap falls during the third.
    agent = make_agent(tmp_path, max_cost_usd=0.0025)
    monkeypatch.setattr(agent, "_client", lambda: client)
    context = AgentContext()
    await agent.run("Build", LocalEnvironment(app), context)
    assert context.metadata["stop_reason"] == "max_cost" and context.metadata["model_calls"] == 3
    assert context.cost_usd >= 0.0025 and len(client.inputs) == 3


async def test_command_timeouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    app.mkdir()
    agent = make_agent(tmp_path, command_timeout_sec=10)
    text, record = await agent.run_tool(LocalEnvironment(app), str(app), "run_command", {"command": "sleep 30", "timeout_seconds": 1})
    assert text.startswith("[timed out after 10s") and record["timed_out"]


def test_preflight_needs_a_key() -> None:
    with pytest.raises(ValueError, match="MISTRAL_API_KEY"):
        StudioAgent.preflight({}, {})
    StudioAgent.preflight({}, {"MISTRAL_API_KEY": "x"})
    with pytest.raises(ValueError, match="Unknown option"):
        StudioAgent.preflight({"max_turn": 3}, {"MISTRAL_API_KEY": "x"})
