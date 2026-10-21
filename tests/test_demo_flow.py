"""The demo's workflows end to end, in this process: an in-memory GitHub with a real bare repo, a fake Slack,
the fake harbor and the smoke judge. From an issue in the repo to a merged change, a release with an APK and
Slack messages; or to an A/B report with one APK per arm."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mistralai.workflows as workflows
import pytest
import temporalio.workflow
from mistralai.workflows.core.sandbox import get_sandbox_restrictions
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from conftest import FIXTURE_SKILL
from fake_github import FakeGitHub
from le_chonk_eval.demo import notify, runner
from le_chonk_eval.demo import workflows as demo
from le_chonk_eval.demo.github import GitHub
from le_chonk_eval.demo.issues import AB_DONE, AB_REQUESTED, DONE, FAILED, READY, Task
from le_chonk_eval.demo.notify import Notifier
from le_chonk_eval.demo.workflows import IssueAb, IssueRun, IssueWatcher, WatchInput

REPO = "denis/le-chonk-game"
SKILL_URL = "https://github.com/Kotlin/kotlin-agent-skills/tree/main/skills/kotlin-tooling-kotlin-toolchain"


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802 (Slack's name)
        self.calls.append(("post", kwargs))
        return {"ok": True}

    async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("upload", kwargs))
        return {"ok": True}

    def texts(self) -> list[str]:
        return [kwargs.get("text") or kwargs.get("initial_comment") for _, kwargs in self.calls]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    remotes = tmp_path / "remotes"
    monkeypatch.setenv("LECHONK_GIT_BASE", remotes.as_uri())
    fake = FakeGitHub(remotes)
    fake.add_repo(REPO)
    fake.add_repo("Kotlin/kotlin-agent-skills")
    fake.add_file("Kotlin/kotlin-agent-skills", "skills/kotlin-tooling-kotlin-toolchain/SKILL.md", FIXTURE_SKILL)
    slack = FakeSlack()
    monkeypatch.setattr(demo, "make_github", lambda: GitHub("t0ken", transport=fake.transport()))
    monkeypatch.setattr(notify, "make_notifier", lambda: Notifier(client=slack))
    monkeypatch.setenv("LECHONK_FAKE_PASS", "1")
    return SimpleNamespace(github=fake, slack=slack, tmp=tmp_path)


def add_issue(fake: FakeGitHub, label: str, skills: list[str] | None = None, attempts: int | None = None) -> dict[str, Any]:
    task = Task(title="Add a double jump", goal="Le Chonk can jump once more in the air.", criteria=["A second tap jumps again"],
                skills=skills or [], ab_attempts=attempts, slack_channel="C1", slack_thread="1712.5")
    number = fake._number(REPO)
    issue = {"number": number, "title": task.title, "body": task.body(), "state": "open", "labels": [{"name": label}],
             "html_url": f"https://github.com/{REPO}/issues/{number}"}
    fake.issues[REPO].append(issue)
    return issue


async def watch_once() -> dict[str, Any]:
    return await workflows.execute_workflow(IssueWatcher, WatchInput(repo=REPO, smoke=True, poll_seconds=0, max_polls=1))


def labels(issue: dict[str, Any]) -> set[str]:
    return {label["name"] for label in issue["labels"]}


async def test_an_issue_is_implemented_merged_and_released(world: SimpleNamespace) -> None:
    issue = add_issue(world.github, READY)
    outcome = await watch_once()

    handled = outcome["handled"][0]
    assert handled["number"] == 1 and handled["kind"] == "run" and handled["result"]["merged"] and handled["result"]["done"]
    fake = world.github
    assert issue["state"] == "closed" and labels(issue) == {DONE}
    run_id = handled["run_id"]
    assert "Made up by the fake harbor" in fake.branch_file(REPO, "main", f"shared/src/{run_id}.kt")
    assert fake.branch_file(REPO, "main", "README.md").startswith("# le-chonk-game")  # the first commit of an empty repo
    pull = fake.pulls[REPO][0]
    assert pull["merged"] and pull["head"] == f"vibe/{run_id}" and "Judge: done" in pull["body"]
    release = fake.releases[REPO][0]
    assert release["tag_name"] == run_id and not release["prerelease"]
    assert [(a["name"], a["content_type"]) for a in release["assets"]] == [("le-chonk-issue-1.apk", "application/vnd.android.package-archive")]
    comments = fake.comments[(REPO, 1)]
    assert comments[0].startswith("Started the implementation: run") and comments[-1].startswith("Merged Vibe's change: PR #2")
    texts = world.slack.texts()
    assert texts[0].startswith("Started the implementation of #1") and "merged in <https://github.com/denis/le-chonk-game/pull/2|PR #2>" in texts[-1]
    assert all(kwargs["thread_ts"] == "1712.5" for _, kwargs in world.slack.calls)
    judge = json.loads(next((world.tmp / "jobs" / run_id).glob("*/judge.json")).read_text())
    assert judge["verdict"] == "done" and judge["files_changed"] == 1


async def test_a_change_that_doesnt_build_is_not_merged(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LECHONK_FAKE_PASS", "0")
    issue = add_issue(world.github, READY)
    outcome = await watch_once()
    assert outcome["handled"][0]["result"] == {"merged": False, "reason": "the Android APK didn't build"}
    assert issue["state"] == "open" and labels(issue) == {FAILED}
    assert not world.github.pulls[REPO] and not world.github.releases[REPO]
    assert "nothing merged" in world.slack.texts()[-1]


async def test_a_broken_run_frees_the_issue(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(params: Any) -> dict[str, Any]:
        raise RuntimeError("Docker isn't running")

    monkeypatch.setattr(demo, "run_harbor_job", broken)
    issue = add_issue(world.github, READY)
    outcome = await watch_once()
    assert outcome["handled"][0]["result"] == {"ok": False, "error": "RuntimeError: Docker isn't running"}
    assert labels(issue) == {FAILED}  # not ready any more: the watcher doesn't pick it up again
    assert "failed: RuntimeError: Docker isn't running" in world.github.comments[(REPO, 1)][-1]
    assert "the run failed" in world.slack.texts()[-1]


async def test_an_ab_compares_the_skill_and_releases_both_apks(world: SimpleNamespace) -> None:
    issue = add_issue(world.github, AB_REQUESTED, skills=[SKILL_URL], attempts=2)
    outcome = await watch_once()

    result = outcome["handled"][0]["result"]
    run_id = outcome["handled"][0]["run_id"]
    assert outcome["handled"][0]["kind"] == "ab" and run_id.startswith("issue-1-ab-")
    assert result["arms"]["skill"]["n"] == 2 and result["arms"]["no-skill"]["n"] == 2 and not result["errors"]
    assert labels(issue) == {AB_DONE} and issue["state"] == "open"
    release = world.github.releases[REPO][0]
    assert release["prerelease"] and release["tag_name"] == run_id
    assert sorted(a["name"] for a in release["assets"]) == ["ab-report.html", "le-chonk-issue-1-no-skill.apk", "le-chonk-issue-1-skill.apk"]
    assert not world.github.pulls[REPO]  # an A/B merges nothing
    for arm in ("no-skill", "skill"):
        config = json.loads(next((world.tmp / "jobs" / f"{run_id}-{arm}").glob("*/config.json")).read_text())
        assert config["agent"]["skills"] == ([SKILL_URL] if arm == "skill" else [])
    html = (world.tmp / "results" / run_id / "dashboard.html").read_text()
    assert "#1: Add a double jump, skill A/B" in html and "Vibe on mistral/mistral-large-4" in html
    kind, upload = world.slack.calls[-1]
    assert kind == "upload" and upload["file"].endswith("dashboard.html") and upload["initial_comment"].startswith("A/B for #1")
    assert "| skill | 2 |" in world.github.comments[(REPO, 1)][-1]


async def test_a_retried_publish_reuses_the_merged_pr(world: SimpleNamespace) -> None:
    """The release fails after the merge. The SDK retries the activity, which must not see "no change" then."""
    world.github.fail_next["release"] = 1
    add_issue(world.github, READY)
    outcome = await watch_once()
    result, run_id = outcome["handled"][0]["result"], outcome["handled"][0]["run_id"]
    posts = [path for method, path in world.github.requests if method == "POST" and path.endswith("/releases")]
    assert len(posts) == 2 and world.github.fail_next["release"] == 0  # the first one got the 502
    assert result["merged"] and result["done"] and result["pull"] == 2 and len(world.github.pulls[REPO]) == 1
    assert [r["tag_name"] for r in world.github.releases[REPO]] == [run_id]

    import subprocess

    bare = str(world.github.remotes / f"{REPO}.git")
    base = subprocess.run(["git", "--git-dir", bare, "rev-parse", "main^"], capture_output=True, text=True, check=True).stdout.strip()
    again = await demo.publish_issue_run(demo.PublishInput(repo=REPO, number=1, run_id=run_id, base=base, jobs=[run_id]))
    assert again["merged"] and len(world.github.pulls[REPO]) == 1 and len(world.github.releases[REPO]) == 1
    assert [a["name"] for a in world.github.releases[REPO][0]["assets"]] == ["le-chonk-issue-1.apk"]


async def test_an_ab_without_finished_trials_fails(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(params: Any) -> dict[str, Any]:
        raise RuntimeError(f"no Docker for {params.arm}")

    monkeypatch.setattr(demo, "run_harbor_job", broken)
    issue = add_issue(world.github, AB_REQUESTED, skills=[SKILL_URL], attempts=2)
    outcome = await watch_once()
    assert "no trial of the A/B finished: no-skill: RuntimeError: no Docker for no-skill" in outcome["handled"][0]["result"]["error"]
    assert labels(issue) == {"ab:failed"} and not world.github.releases[REPO]


def test_vibe_gets_the_prices_its_cost_limit_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    assert demo._cost_limit("mistral/mistral-large-4", 3.0) == (3.0, ["VIBE_INPUT_PRICE=0.68", "VIBE_OUTPUT_PRICE=2.09"], "")
    limit, env, note = demo._cost_limit("mistral/not-a-model", 3.0)
    assert limit is None and env == [] and "no cost limit" in note
    monkeypatch.setenv("LECHONK_PRICE_INPUT", "0.4")
    monkeypatch.setenv("LECHONK_PRICE_OUTPUT", "2")
    assert demo._cost_limit("mistral/not-a-model", 3.0)[1] == ["VIBE_INPUT_PRICE=0.4", "VIBE_OUTPUT_PRICE=2"]
    monkeypatch.setenv("VIBE_INPUT_PRICE", "1")
    monkeypatch.setenv("VIBE_OUTPUT_PRICE", "1")
    assert demo._cost_limit("mistral/not-a-model", 3.0) == (3.0, [], "")  # Harbor reads those itself


async def test_an_ab_needs_a_skill(world: SimpleNamespace) -> None:
    issue = add_issue(world.github, AB_REQUESTED)
    outcome = await watch_once()
    assert "has no skill" in outcome["handled"][0]["result"]["error"]
    assert labels(issue) == {"ab:failed"}


async def test_a_queue_is_handled_in_order_and_from_the_last_merge(world: SimpleNamespace) -> None:
    first, second = add_issue(world.github, READY), add_issue(world.github, READY)
    outcome = await watch_once()
    assert [h["number"] for h in outcome["handled"]] == [1, 2] and outcome["polls"] == 1
    runs = [h["run_id"] for h in outcome["handled"]]
    for run_id in runs:  # the second run started from the first one's merge, so main has both
        assert world.github.branch_file(REPO, "main", f"shared/src/{run_id}.kt")
    assert first["state"] == second["state"] == "closed"


async def test_the_watcher_is_started_once_per_repo() -> None:
    calls: list[dict[str, Any]] = []

    class Executions:
        def __init__(self, status: str | None) -> None:
            self.status = status

        async def get_workflow_execution_async(self, execution_id: str) -> Any:
            if self.status is None:
                raise RuntimeError("404 not found")
            return SimpleNamespace(status=self.status)

    def client(status: str | None) -> Any:
        async def execute(**kwargs: Any) -> None:
            calls.append(kwargs)

        return SimpleNamespace(workflows=SimpleNamespace(executions=Executions(status), execute_workflow_async=execute))

    params = WatchInput(repo=REPO, model="mistral/devstral-latest")
    assert (await runner.start_watcher_cloud(params, client(None))).startswith("started (Workflows execution le-chonk-issues-denis-le-chonk-game)")
    assert calls[0]["workflow_identifier"] == "le-chonk-issues" and calls[0]["input"]["model"] == "mistral/devstral-latest"
    assert (await runner.start_watcher_cloud(params, client("WorkflowExecutionStatus.RUNNING"))).startswith("already running")
    assert len(calls) == 1


@pytest.mark.parametrize("workflow", [IssueWatcher, IssueRun, IssueAb])
async def test_the_workflows_import_inside_the_sandbox(workflow: type) -> None:
    runner_ = SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions())
    runner_.prepare_workflow(temporalio.workflow._Definition.must_from_class(workflow))
