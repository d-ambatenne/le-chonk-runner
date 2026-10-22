"""The QA agent's loop: its tools on a fake phone, screenshots sent as images beside the results, and its verdicts."""

from __future__ import annotations

import base64
from pathlib import Path

import mistralai.client.models as models
import pytest

from fake_device import PACKAGE, SHOT, FakeDevice
from fakes import FakeClient, call
from le_chonk_eval.qa.agent import AGENT_NAME, QaTools, ToolResult, check_criterion, ensure_qa_agent, tool_inputs

CRITERION = "Le Chonk runs by himself without player input; a tap makes him jump"


def say(content: str) -> models.MessageOutputEntry:
    return models.MessageOutputEntry(content=content)


def tools(tmp_path: Path, device: FakeDevice | None = None) -> QaTools:
    return QaTools(device or FakeDevice(), PACKAGE, f"{PACKAGE}/.MainActivity", tmp_path / "screenshots")


async def test_a_screenshot_goes_in_as_an_image_beside_the_results_in_one_append(tmp_path: Path) -> None:
    client = FakeClient([
        [call("c1", "ui_tree")],
        [say("Let me jump."), call("c2", "tap_then_capture", ms=300)],
        [call("c3", "report_verdict", verdict="pass", check="screenshot",
              evidence="Score rose from 0 to 10 with no input. Screenshot c1-1.png shows him above the ground after a tap.")],
    ])
    device = FakeDevice()
    found = await check_criterion(client, "ag_1", tools(tmp_path, device), 1, CRITERION)

    assert found.verdict == "pass" and found.check == "screenshot" and not found.blocking and found.rounds == 3
    assert found.screenshots == ["c1-1.png"] and (tmp_path / "screenshots" / "c1-1.png").read_bytes() == SHOT
    assert ("tap_then_capture", 540, 1200, 300) in device.calls  # no element: the screen's center
    start, after_tree, after_shot = client.inputs
    assert CRITERION in start["inputs"] and PACKAGE in start["inputs"] and start["agent_id"] == "ag_1"
    assert "github.com" not in start["inputs"] and ".kt" not in start["inputs"]  # the criterion and the app, never the code
    assert [type(e).__name__ for e in after_tree["inputs"]] == ["FunctionResultEntry"]
    result, image = after_shot["inputs"]
    assert isinstance(result, models.FunctionResultEntry) and "c1-1.png" in result.result
    assert isinstance(image, models.MessageInputEntry) and image.role == "user"
    picture = [chunk for chunk in image.content if isinstance(chunk, models.ImageURLChunk)][0]
    assert picture.image_url == "data:image/png;base64," + base64.b64encode(SHOT).decode()


async def test_a_tree_verdict_blocks_and_keeps_its_flow_and_evidence(tmp_path: Path) -> None:
    flow = 'appId: x\n---\n- launchApp\n- assertVisible: "(?i)score: \\\\d+"\n'
    client = FakeClient([
        [call("c1", "wait", seconds=12), call("c2", "ui_tree")],
        [call("c3", "tap", element="e4"), call("c4", "ui_tree")],
        [call("c5", "report_verdict", verdict="fail", check="tree", evidence="RESTART left GAME OVER on screen.", flow=flow)],
    ])
    device = FakeDevice()
    device.crash_log = "FATAL EXCEPTION: main"
    found = await check_criterion(client, "ag_1", tools(tmp_path, device), 2, "The game-over screen has a restart option")
    assert found.verdict == "fail" and found.blocking and found.flow == flow.strip()
    assert ("tap", 540, 1350) in device.calls  # e4 is RESTART on the game-over screen
    assert 'description="5 of 5 hearts"' in found.tree and found.log == "FATAL EXCEPTION: main"  # a new run after the tap


async def test_tool_mistakes_are_answered_and_a_missing_verdict_is_cant_check(tmp_path: Path) -> None:
    client = FakeClient([
        [call("c1", "tap", element="e9"), call("c2", "report_verdict", verdict="maybe", check="tree", evidence="?"), call("c3", "rm_rf")],
        [say("I think it works.")],
        [say("It works.")],
    ])
    found = await check_criterion(client, "ag_1", tools(tmp_path), 1, CRITERION)
    answers = [e.result for e in client.inputs[1]["inputs"]]
    assert "no element e9" in answers[0] and "verdict must be one of" in answers[1] and "no tool named rm_rf" in answers[2]
    assert client.inputs[2]["inputs"] == "Call report_verdict now."
    assert found.verdict == "cant_check" and "No verdict after" in found.evidence and "It works." in found.evidence


def test_results_without_images_are_only_results() -> None:
    entries = tool_inputs([("a", ToolResult("Tapped.")), ("b", ToolResult("Waited 2 s."))])
    assert [e.tool_call_id for e in entries] == ["a", "b"]


async def test_the_studio_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient([])
    await ensure_qa_agent(client)
    created = client.created[0]
    assert created["name"] == AGENT_NAME == "le-chonk-qa" and created["model"] == "mistral-large-4"
    assert {t["function"]["name"] for t in created["tools"]} >= {"ui_tree", "tap_then_capture", "report_verdict"}
    assert "never its code" in created["instructions"]
    monkeypatch.setenv("LECHONK_QA_MODEL", "mistral/mistral-medium-3")
    await ensure_qa_agent(client)
    assert client.updated[0]["model"] == "mistral-medium-3"


async def test_a_flow_that_fails_its_replay_goes_back_to_the_agent_to_fix(tmp_path: Path) -> None:
    hint = 'appId: x\n---\n- tapOn: "(?i)restart"\n- assertVisible: "(?i).*tap to jump.*"\n'
    lasting = 'appId: x\n---\n- tapOn: "(?i)restart"\n- assertNotVisible: "(?i).*game over.*"\n'
    client = FakeClient([
        [call("c1", "report_verdict", verdict="pass", check="tree", evidence="RESTART started a new run.", flow=hint)],
        [call("c2", "report_verdict", verdict="pass", check="tree", evidence="RESTART started a new run.", flow=lasting)],
    ])
    replayed: list[str] = []

    async def check_flow(flow: str) -> str | None:
        replayed.append(flow)
        return 'it failed on replay: Assertion is false: "(?i).*tap to jump.*" is visible' if "tap to jump" in flow else None

    found = await check_criterion(client, "ag_1", tools(tmp_path), 6, "Restart starts a new run", check_flow=check_flow)
    assert found.verdict == "pass" and found.flow == lasting.strip() and replayed == [hint.strip(), lasting.strip()]
    answer = client.inputs[1]["inputs"][0]
    assert isinstance(answer, models.FunctionResultEntry) and answer.tool_call_id == "c1" and "tap to jump" in answer.result
    assert "Fix the flow" in answer.result and found.notes == [
        'Its flow didn\'t work, so the agent was asked to fix it: it failed on replay: Assertion is false: "(?i).*tap to jump.*" is visible']


async def test_a_flow_that_never_works_leaves_the_verdict_and_a_note(tmp_path: Path) -> None:
    bad = 'appId: x\n---\n- wait: 5\n'
    client = FakeClient([[call(f"c{n}", "report_verdict", verdict="pass", check="tree", evidence="The score grew.", flow=bad)] for n in (1, 2, 3)])

    async def check_flow(flow: str) -> str | None:
        return "step {'wait': 5} isn't one of the allowed steps"

    found = await check_criterion(client, "ag_1", tools(tmp_path), 5, "The score grows", check_flow=check_flow, flow_fixes=2)
    assert found.verdict == "pass" and found.check == "tree" and len(client.inputs) == 3  # the start, then two fixes asked for
    assert found.notes[-1] == "Its flow wasn't kept: step {'wait': 5} isn't one of the allowed steps" and len(found.notes) == 3


async def test_a_screenshot_verdict_or_a_failure_has_no_flow_to_replay(tmp_path: Path) -> None:
    client = FakeClient([[call("c1", "report_verdict", verdict="fail", check="tree", evidence="No GAME OVER.", flow="appId: x\n---\n- launchApp\n")]])

    async def check_flow(flow: str) -> str | None:
        raise AssertionError("a failing check's flow isn't replayed")

    found = await check_criterion(client, "ag_1", tools(tmp_path), 4, "Game over shows the score", check_flow=check_flow)
    assert found.verdict == "fail" and found.notes == []


async def test_a_tool_that_keeps_failing_stops_the_check_instead_of_a_screenshot_verdict(tmp_path: Path) -> None:
    device = FakeDevice()
    device.tree_works_for = 0  # the emulator is sick: Android kills uiautomator
    client = FakeClient([
        [call("c1", "ui_tree")], [call("c2", "ui_tree")], [call("c3", "ui_tree")],
        [call("c4", "report_verdict", verdict="pass", check="screenshot", evidence="Judged from screenshots instead.")],
    ])
    found = await check_criterion(client, "ag_1", tools(tmp_path, device), 4, "Game over shows the score")
    assert found.verdict == "cant_check" and found.environment.startswith("ui_tree failed 3 times") and "137" in found.environment
    assert len(client.inputs) == 3  # it stopped: no fourth round, and no screenshot verdict
