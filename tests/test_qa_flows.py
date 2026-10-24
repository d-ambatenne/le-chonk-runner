"""The Maestro flows the QA agent writes: checked, given the app's package, replayed, and read from JUnit reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from le_chonk_eval.qa.flows import Maestro, criterion_line, flow_path, keep_flow, kept_flow, normalize, parse_junit

FLOW = """appId: com.wrong.guess
---
- launchApp:
    clearState: true
- extendedWaitUntil:
    visible: "(?i).*game over.*"
    timeout: 30000
- tapOn: "(?i)restart"
- assertNotVisible: "(?i).*game over.*"
"""
PASSED = '<testsuites><testsuite name="Test Suite" tests="1" failures="0"><testcase id="criterion-2" name="criterion-2"/></testsuite></testsuites>'
FAILED = ('<testsuites><testsuite tests="1" failures="1"><testcase name="criterion-2"><failure message="Assertion is false: '
          '&quot;(?i).*game over.*&quot; is visible">trace</failure></testcase></testsuite></testsuites>')


def test_a_flow_gets_the_apps_package_and_only_known_steps() -> None:
    text, problems = normalize(FLOW, "dev.dambatenne.lechonk")
    assert not problems and text.startswith("appId: dev.dambatenne.lechonk\n---\n") and "tapOn: (?i)restart" in text
    assert normalize("appId: x\n---\n- runScript: steal.js\n", "p")[1] == [
        "step {'runScript': 'steal.js'} isn't one of assertNotVisible, assertVisible, back, extendedWaitUntil, launchApp, stopApp, "
        "takeScreenshot, tapOn, waitForAnimationToEnd"]
    assert normalize("- tapOn: x\n", "p")[1] == ["a flow is a header with appId, then ---, then a list of steps"]
    assert normalize("appId: [unclosed\n---\n", "p")[1][0].startswith("not YAML")


def test_junit_reports() -> None:
    assert parse_junit(PASSED) == (True, "passed")
    passed, message = parse_junit(FAILED)
    assert not passed and '"(?i).*game over.*" is visible' in message
    assert parse_junit("<testsuites/>") == (False, "the report has no test") and not parse_junit("not xml")[0]


async def test_a_replay_runs_maestro_on_the_emulator_and_reads_its_report(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    async def run(args: list[str], timeout: float) -> tuple[int, str]:
        seen.append(args)
        Path(args[args.index("--output") + 1]).write_text(PASSED)
        return 0, "Flow passed"

    flow = tmp_path / "criterion-2.yaml"
    flow.write_text(FLOW)
    result = await Maestro("emulator-5580", binary="maestro", run=run).replay(flow, tmp_path / "out")
    assert result.passed and result.report == tmp_path / "out" / "criterion-2.xml"
    assert seen[0] == ["maestro", "--device", "emulator-5580", "test", "--format", "JUNIT", "--output", str(result.report),
                       "--test-output-dir", str(tmp_path / "out" / "maestro"), str(flow)]

    async def broken(args: list[str], timeout: float) -> tuple[int, Any]:
        return 1, "Not enough devices connected"

    result = await Maestro("emulator-5580", binary="maestro", run=broken).replay(flow, tmp_path / "out")
    assert not result.passed and "wrote no report: Not enough devices connected" in result.message


def test_flows_are_kept_per_issue_and_criterion() -> None:
    assert flow_path("denis/le-chonk-game", 1, 3).parts[-4:] == ("flows", "denis-le-chonk-game", "issue-1", "criterion-3.yaml")


def test_a_flow_is_kept_with_its_criterion_and_replayed_only_for_the_same_words(tmp_path: Path) -> None:
    assert kept_flow("denis/le-chonk-game", 1, 2, "He jumps") == (None, "")
    path = keep_flow("denis/le-chonk-game", 1, 2, "Tapping  anywhere\nmakes him jump", "appId: x\n---\n- launchApp\n")
    assert path.read_text() == "# criterion: Tapping anywhere makes him jump\nappId: x\n---\n- launchApp\n"
    assert criterion_line(" Tapping anywhere makes him jump ") == "# criterion: Tapping anywhere makes him jump"
    assert kept_flow("denis/le-chonk-game", 1, 2, "Tapping anywhere makes him jump") == (path, "")
    missing, why = kept_flow("denis/le-chonk-game", 1, 2, "A double tap makes him jump twice")
    assert missing is None and why == "The kept flow criterion-2.yaml was written for other words in this criterion, so it wasn't replayed."
