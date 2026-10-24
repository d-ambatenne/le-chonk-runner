"""A whole QA run against fakes: GitHub's issue and release, a phone, the model and Maestro. Also the CLI and le-chonk-qa."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mistralai.workflows as workflows
import pytest

from fake_device import PACKAGE, FakeDevice
from fake_github import FakeGitHub
from fakes import FakeClient, call
from le_chonk_eval.demo.github import GitHub
from le_chonk_eval.qa import run as qa_run
from le_chonk_eval.qa.flows import FlowResult, criterion_line, flow_path
from le_chonk_eval.qa.inputs import QaInput

REPO, TAG = "denis/le-chonk-game", "issue-1-20261021-202046"
BODY = """Build Le Chonk Runner.

### Acceptance criteria
- At zero hearts the game ends with a game-over screen showing the final score
- The game-over screen has a restart option that starts a new run
- He has 5 hearts; each hit removes one heart
"""
RESTART_FLOW = 'appId: x\n---\n- launchApp\n- tapOn: "(?i)restart"\n'
CRITERIA = [line[2:] for line in BODY.splitlines() if line.startswith("- ")]


def kept(k: int, text: str, words: str | None = None) -> Path:
    """A flow an earlier run kept for criterion k, written for the criterion's words or for other ones."""
    path = flow_path(REPO, 1, k)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(criterion_line(CRITERIA[k - 1] if words is None else words) + "\n" + text)
    return path


class FakeMaestro:
    """Replays pass, except the flows named in `failing`."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.replayed: list[str] = []

    def __call__(self, serial: str) -> FakeMaestro:
        return self

    async def replay(self, flow: Path, out: Path) -> FlowResult:
        self.replayed.append(flow.name)
        return FlowResult(flow.name not in self.failing, "passed" if flow.name not in self.failing else "Element not found: (?i)restart")


@pytest.fixture
def github(tmp_path: Path) -> FakeGitHub:
    fake = FakeGitHub(tmp_path / "remotes")
    fake.add_repo(REPO)
    fake.issues[REPO].append({"number": 1, "title": "Build Le Chonk Runner", "body": BODY, "state": "open", "labels": [],
                              "html_url": f"https://github.com/{REPO}/issues/1"})
    fake.releases[REPO].append({"id": 1, "tag_name": TAG, "assets": [], "html_url": f"https://github.com/{REPO}/releases/tag/{TAG}"})
    fake.assets[1] = b"PK-the-apk"
    fake.releases[REPO][0]["assets"].append({"id": 1, "name": "le-chonk-issue-1.apk"})
    return fake


async def test_a_run_replays_kept_flows_and_explores_the_rest(github: FakeGitHub, tmp_path: Path) -> None:
    kept(1, "appId: dev.dambatenne.lechonk\n---\n- launchApp\n")  # criterion 1 was checked by an earlier run
    client = FakeClient([
        [call("a1", "wait", seconds=12), call("a2", "ui_tree")],
        [call("a3", "tap", element="e4"), call("a4", "ui_tree")],
        [call("a5", "report_verdict", verdict="pass", check="tree", evidence="RESTART started a new run.", flow=RESTART_FLOW)],
        [call("b1", "tap_then_capture", ms=0)],
        [call("b2", "report_verdict", verdict="fail", check="tree", evidence="The tree shows no hearts.")],
    ])
    device, maestro = FakeDevice(), FakeMaestro()
    result = await qa_run.run_qa(QaInput(repo=f"https://github.com/{REPO}", number=1, release=TAG, serial="emulator-5554"),
                                 github=GitHub("t0ken", transport=github.transport()), client=client,
                                 make_device=lambda serial: device, make_maestro=maestro)

    out = tmp_path / "runs" / "qa" / f"1-{TAG}"
    assert (out / "apk" / "le-chonk-issue-1.apk").read_bytes() == b"PK-the-apk"
    assert ("install", str(out / "apk" / "le-chonk-issue-1.apk")) in device.calls and result["package"] == PACKAGE
    first, second, third = result["verdicts"]
    assert first["source"] == "flow" and first["verdict"] == "pass" and first["blocking"]  # no conversation for it
    assert second["verdict"] == "pass" and second["flow_file"] == str(flow_path(REPO, 1, 2))
    assert flow_path(REPO, 1, 2).read_text().startswith(f"{criterion_line(CRITERIA[1])}\nappId: {PACKAGE}\n")  # replayed, then kept
    assert third["verdict"] == "fail" and third["blocking"] and third["screenshots"] == ["c3-1.png"]
    assert maestro.replayed == ["criterion-1.yaml", "criterion-2.yaml"]
    assert result["blocking"] == [3] and (result["passed"], result["failed"], result["cant_check"]) == (2, 1, 0)
    assert len([i for i in client.inputs if "agent_id" in i]) == 2  # two conversations: criteria 2 and 3
    assert json.loads((out / "verdicts.json").read_text())["verdicts"][2]["evidence"] == "The tree shows no hearts."
    report = (out / "report.md").read_text()
    assert report.startswith("# QA of #1: Build Le Chonk Runner") and "| 3 | He has 5 hearts; each hit removes one heart | fail | tree |" in report
    table = qa_run.table(result)
    assert "2 passed, 1 failed, 0 not checked" in table and "FAIL  3. He has 5 hearts" in table and "blocks the merge" in table


async def test_a_failing_kept_flow_sends_the_agent_back(github: FakeGitHub) -> None:
    for k in (1, 2, 3):
        kept(k, RESTART_FLOW)
    client = FakeClient([[call("a1", "report_verdict", verdict="fail", check="tree", evidence="No GAME OVER after 30 s.")]])
    result = await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554"),
                                 github=GitHub("t0ken", transport=github.transport()), client=client,
                                 make_device=lambda serial: FakeDevice(), make_maestro=FakeMaestro({"criterion-1.yaml"}))
    first = result["verdicts"][0]
    assert first["source"] == "agent" and first["verdict"] == "fail" and "The kept flow criterion-1.yaml failed" in first["notes"][0]


async def test_a_flow_kept_for_other_words_is_not_replayed(github: FakeGitHub) -> None:
    kept(1, RESTART_FLOW, words="The game-over screen shows the best score")  # the criterion was edited since
    flow_path(REPO, 1, 2).write_text(RESTART_FLOW)  # kept before flows named their criterion
    kept(3, RESTART_FLOW)
    client = FakeClient([
        [call("a1", "report_verdict", verdict="pass", check="tree", evidence="GAME OVER with the score.", flow=RESTART_FLOW)],
        [call("b1", "report_verdict", verdict="pass", check="tree", evidence="RESTART started a new run.")],
    ])
    maestro = FakeMaestro()
    result = await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554", label=False),
                                 github=GitHub("t0ken", transport=github.transport()), client=client,
                                 make_device=lambda serial: FakeDevice(), make_maestro=maestro)
    first, second, third = result["verdicts"]
    assert first["source"] == "agent" and "was written for other words in this criterion" in first["notes"][0]
    assert second["source"] == "agent" and "doesn't name its criterion" in second["notes"][0]
    assert third["source"] == "flow"  # same words: replayed, no conversation
    assert flow_path(REPO, 1, 1).read_text().startswith(criterion_line(CRITERIA[0]) + "\n")  # kept again, for the new words
    assert maestro.replayed == ["criterion-1.yaml", "criterion-3.yaml"]  # the agent's new flow, then the kept one


async def test_a_release_without_an_apk_or_criteria_stops_the_run(github: FakeGitHub) -> None:
    github.releases[REPO][0]["assets"] = []
    with pytest.raises(ValueError, match="has no APK"):
        await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="x"), github=GitHub("t", transport=github.transport()), client=FakeClient([]))
    github.issues[REPO][0]["body"] = "Just a goal."
    with pytest.raises(ValueError, match="no acceptance criteria"):
        await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="x"), github=GitHub("t", transport=github.transport()), client=FakeClient([]))


async def test_le_chonk_qa_runs_the_same_code_and_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from le_chonk_eval.qa.workflow import QaRun

    async def fake_run(params: QaInput) -> dict[str, Any]:
        return {"ok": True, "number": params.number, "release": params.release}

    monkeypatch.setattr(qa_run, "run_qa", fake_run)
    assert await workflows.execute_workflow(QaRun, QaInput(repo=REPO, number=1, release=TAG)) == {"ok": True, "number": 1, "release": TAG}

    async def broken(params: QaInput) -> dict[str, Any]:
        raise RuntimeError("There's no emulator named lechonk-qa")

    monkeypatch.setattr(qa_run, "run_qa", broken)
    outcome = await workflows.execute_workflow(QaRun, QaInput(repo=REPO, number=1, release=TAG))
    assert outcome["ok"] is False and "no emulator named lechonk-qa" in outcome["error"]


def test_the_cli_prints_the_table(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from le_chonk_eval import cli

    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    with pytest.raises(SystemExit, match="MISTRAL_API_KEY is not set"):
        cli.main(["qa", "run", "--repo", REPO, "--issue", "1", "--release", TAG])
    seen: list[QaInput] = []

    async def fake_run(params: QaInput) -> dict[str, Any]:
        seen.append(params)
        return {"repo": REPO, "number": 1, "release": TAG, "passed": 1, "failed": 0, "cant_check": 0, "out": "runs/qa/x",
                "verdicts": [{"number": 1, "criterion": "Restart starts a new run", "verdict": "pass", "check": "tree", "source": "agent",
                              "blocking": True}]}

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr(qa_run, "run_qa", fake_run)
    cli.main(["qa", "run", "--repo", REPO, "--issue", "1", "--release", TAG, "--serial", "emulator-5554"])
    printed = capsys.readouterr().out
    assert "pass  1. Restart starts a new run  (tree)" in printed and "runs/qa/x" in printed
    assert seen[0].serial == "emulator-5554" and seen[0].model is None


def test_the_macs_worker_registers_le_chonk_qa(monkeypatch: pytest.MonkeyPatch) -> None:
    from le_chonk_eval.next import start

    registered: list[str] = []

    async def run_worker(found: list[Any]) -> None:
        registered.extend(getattr(w, "__name__", str(w)) for w in found)

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("DEPLOYMENT_NAME", "le-chonk-vibe")
    monkeypatch.setattr(workflows, "run_worker", run_worker)
    start.run_worker("le-chonk-vibe")
    assert registered == ["VibeRun", "VibeAb", "QaRun"]


async def test_with_aapt2_the_package_is_known_even_on_a_used_emulator(tmp_path: Path) -> None:
    device = FakeDevice()
    device.installed = {PACKAGE, "com.example.other"}  # your own emulator, with the app and others on it

    async def package_of(apk: Path) -> str:
        return PACKAGE

    assert await qa_run.install(device, tmp_path / "app.apk", package_of=package_of) == (PACKAGE, f"{PACKAGE}/.MainActivity")

    async def unknown(apk: Path) -> None:
        return None

    with pytest.raises(RuntimeError, match="Can't tell which app app.apk is"):
        await qa_run.install(device, tmp_path / "app.apk", package_of=unknown)


class TextMaestro(FakeMaestro):
    """Replays fail when the flow has the given words."""

    def __init__(self, words: str) -> None:
        super().__init__()
        self.words = words

    async def replay(self, flow: Path, out: Path) -> FlowResult:
        self.replayed.append(flow.name)
        bad = self.words in flow.read_text()
        return FlowResult(not bad, f'Assertion is false: "{self.words}" is visible' if bad else "passed")


async def test_the_agent_fixes_a_flow_that_failed_its_replay_and_the_fix_is_kept(github: FakeGitHub, tmp_path: Path) -> None:
    hint = 'appId: x\n---\n- tapOn: "(?i)restart"\n- assertVisible: "(?i).*tap to jump.*"\n'
    client = FakeClient([
        [call("a1", "report_verdict", verdict="pass", check="tree", evidence="GAME OVER with the score.", flow='appId: x\n---\n- launchApp\n')],
        [call("b1", "report_verdict", verdict="pass", check="tree", evidence="RESTART started a new run.", flow=hint)],
        [call("b2", "report_verdict", verdict="pass", check="tree", evidence="RESTART started a new run.", flow=RESTART_FLOW)],
        [call("c1", "report_verdict", verdict="pass", check="screenshot", evidence=f"Hearts in {', '.join(f'c3-{n}.png' for n in range(1, 11))}.")],
    ])
    maestro = TextMaestro("tap to jump")
    result = await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554"),
                                 github=GitHub("t0ken", transport=github.transport()), client=client,
                                 make_device=lambda serial: FakeDevice(), make_maestro=maestro)
    restart = result["verdicts"][1]
    assert restart["verdict"] == "pass" and restart["flow_file"] == str(flow_path(REPO, 1, 2)) and "fix it" in restart["notes"][0]
    assert "tap to jump" not in flow_path(REPO, 1, 2).read_text()  # the fixed flow is the one kept
    assert maestro.replayed == ["criterion-1.yaml", "criterion-2.yaml", "criterion-2.yaml"]


def test_a_long_hunt_lists_a_few_screenshots_and_the_count() -> None:
    shots = [f"c3-{n}.png" for n in range(1, 53)]
    result = {"number": 1, "title": "Runner", "release": TAG, "package": PACKAGE, "verdicts": [
        {"number": 3, "criterion": "He has 5 hearts", "verdict": "pass", "check": "screenshot", "evidence": "One heart per hit.", "screenshots": shots}]}
    row = qa_run.report_markdown(result).splitlines()[-1]
    assert "c3-1.png, c3-2.png, c3-3.png, …, c3-50.png, c3-51.png, c3-52.png, 52 in all." in row


async def test_a_device_that_breaks_stops_the_run_and_says_so(github: FakeGitHub) -> None:
    device = FakeDevice()
    device.tree_works_for = 2  # criterion 1's probe and the agent's read, then uiautomator is killed
    client = FakeClient([
        [call("a1", "ui_tree")],
        [call("a2", "report_verdict", verdict="pass", check="tree", evidence="GAME OVER and the score.")],
    ])
    result = await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554"),
                                 github=GitHub("t0ken", transport=github.transport()), client=client,
                                 make_device=lambda serial: device, make_maestro=FakeMaestro())
    first, second, third = result["verdicts"]
    assert first["verdict"] == "pass" and second["verdict"] == third["verdict"] == "cant_check"
    assert second["environment"].startswith("its UI tree didn't answer") and third["evidence"].startswith("Not checked: the device broke")
    assert len([i for i in client.inputs if "agent_id" in i]) == 1  # no conversation on the broken device
    assert result["environment_errors"] == [second["environment"]] and (result["passed"], result["cant_check"]) == (1, 2)
    assert "The device broke, so the run stopped" in qa_run.table(result) and "without --serial" in qa_run.table(result)


def labels(github: FakeGitHub, number: int = 1) -> set[str]:
    issue = next(i for i in github.issues[REPO] if i["number"] == number)
    return {label["name"] for label in issue["labels"]}


async def test_a_run_marks_its_issue_tested_or_with_bugs_and_comments(github: FakeGitHub) -> None:
    github.issues[REPO][0]["labels"] = [{"name": "qa:bugs"}, {"name": "vibe:failed"}]  # an earlier run found a bug
    passing = FakeClient([[call(f"a{k}", "report_verdict", verdict="pass", check="tree", evidence="Seen.")] for k in (1, 2, 3)])
    result = await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554"),
                                 github=GitHub("t0ken", transport=github.transport()), client=passing,
                                 make_device=lambda serial: FakeDevice(), make_maestro=FakeMaestro())
    assert labels(github) == {"qa:tested", "vibe:failed"}  # QA owns only the qa: labels
    assert result["issue"] == "Issue #1 marked qa:tested, with a comment." and result["issue"] in qa_run.table(result)
    comment = github.comments[(REPO, 1)][-1]
    assert comment.startswith(f"QA ran release `{TAG}` on an emulator: 3 of 3 criteria passed.") and "never the code" in comment
    assert "qa:tested" in {label["name"] for label in github.labels[REPO].values()}  # the label exists in the repo, with its color

    failing = FakeClient([[call("b1", "report_verdict", verdict="fail", check="tree", evidence="GAME OVER never showed.")]]
                         + [[call(f"b{k}", "report_verdict", verdict="pass", check="screenshot", evidence="Seen.")] for k in (2, 3)])
    await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554"),
                        github=GitHub("t0ken", transport=github.transport()), client=failing,
                        make_device=lambda serial: FakeDevice(), make_maestro=FakeMaestro())
    assert labels(github) == {"qa:bugs", "vibe:failed"}
    assert "1 failed" in github.comments[(REPO, 1)][-1] and "- **1.** GAME OVER never showed." in github.comments[(REPO, 1)][-1]


async def test_a_broken_device_or_no_label_leaves_the_issue_alone(github: FakeGitHub) -> None:
    device = FakeDevice()
    device.tree_works_for = 0
    result = await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554"),
                                 github=GitHub("t0ken", transport=github.transport()), client=FakeClient([]),
                                 make_device=lambda serial: device, make_maestro=FakeMaestro())
    assert result["issue"].startswith("Issue not marked: the device broke") and labels(github) == set() and not github.comments.get((REPO, 1))
    quiet = FakeClient([[call(f"a{k}", "report_verdict", verdict="pass", check="tree", evidence="Seen.")] for k in (1, 2, 3)])
    result = await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554", label=False),
                                 github=GitHub("t0ken", transport=github.transport()), client=quiet,
                                 make_device=lambda serial: FakeDevice(), make_maestro=FakeMaestro())
    assert "issue" not in result and labels(github) == set()


async def test_an_issue_can_be_marked_from_a_run_already_done(github: FakeGitHub) -> None:
    passing = FakeClient([[call(f"a{k}", "report_verdict", verdict="pass", check="tree", evidence="Seen.")] for k in (1, 2, 3)])
    await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554", label=False),
                        github=GitHub("t0ken", transport=github.transport()), client=passing,
                        make_device=lambda serial: FakeDevice(), make_maestro=FakeMaestro())
    said = await qa_run.mark_saved(f"https://github.com/{REPO}", 1, TAG, github=GitHub("t0ken", transport=github.transport()))
    assert said == "Issue #1 marked qa:tested, with a comment." and labels(github) == {"qa:tested"}
    with pytest.raises(ValueError, match="No QA run of #1 on issue-9"):
        await qa_run.mark_saved(REPO, 1, "issue-9", github=GitHub("t0ken", transport=github.transport()))


def test_the_cli_marks_from_a_saved_run_and_runs_can_skip_marking(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from le_chonk_eval import cli

    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    asked: list[tuple[str, int, str]] = []

    async def fake_mark(repo: str, number: int, release: str) -> str:
        asked.append((repo, number, release))
        return "Issue #3 marked qa:tested, with a comment."

    monkeypatch.setattr(qa_run, "mark_saved", fake_mark)
    cli.main(["qa", "mark", "--repo", REPO, "--issue", "3", "--release", TAG])  # no model calls, so no key needed
    assert asked == [(REPO, 3, TAG)] and "marked qa:tested" in capsys.readouterr().out
    seen: list[QaInput] = []

    async def fake_run(params: QaInput) -> dict[str, Any]:
        seen.append(params)
        return {"repo": REPO, "number": 1, "release": TAG, "passed": 0, "failed": 0, "cant_check": 0, "out": "runs/qa/x", "verdicts": []}

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr(qa_run, "run_qa", fake_run)
    cli.main(["qa", "run", "--repo", REPO, "--issue", "1", "--release", TAG, "--no-label"])
    assert seen[0].label is False


async def test_the_agent_reads_its_issue_and_others_but_never_a_pull_request(github: FakeGitHub) -> None:
    github.issues[REPO][0]["body"] = BODY + '\n<!-- le-chonk {"slack": {"channel": "C1", "thread": "1.2"}} -->\n'
    github.issues[REPO].append({"number": 2, "title": "Sounds", "body": "Part of #1.\n\n### Acceptance criteria\n- A hit makes a sound\n",
                                "state": "open", "labels": [], "html_url": f"https://github.com/{REPO}/issues/2"})
    github.issues[REPO].append({"number": 3, "title": "#2: Sounds", "body": "Vibe's change", "state": "open", "labels": [],
                                "pull_request": {"url": "x"}, "html_url": f"https://github.com/{REPO}/pull/3"})
    client = FakeClient([[call(f"a{k}", "report_verdict", verdict="pass", check="tree", evidence="Seen.")] for k in (1, 2, 3)])
    await qa_run.run_qa(QaInput(repo=REPO, number=1, release=TAG, serial="emulator-5554", label=False),
                        github=GitHub("t0ken", transport=github.transport()), client=client,
                        make_device=lambda serial: FakeDevice(), make_maestro=FakeMaestro())
    start = client.inputs[0]["inputs"]
    assert start.startswith("The issue, so you know what the app is about:\n\n#1 Build Le Chonk Runner") and "Build Le Chonk Runner." in start
    assert "le-chonk" not in start.split("---")[0].replace("Le Chonk", "") and "C1" not in start  # no hidden metadata
    read = qa_run.issue_reader(GitHub("t0ken", transport=github.transport()), REPO)
    assert (await read(2)).startswith("#2 Sounds\n\nPart of #1.") and "A hit makes a sound" in await read(2)
    assert await read(3) == "#3 is a pull request: QA reads only issues." and await read(9) == "There's no issue #9."
