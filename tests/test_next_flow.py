"""The next phase end to end, in this process: the cloud workflows with the GitHub and Slack connectors in memory,
the Mac's workflows with the fake harbor, and the Workflows API between them. From a Slack message to an issue,
from the issue to a run on the Mac, a release with the APK, and a PR the cloud judged and merged."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mistralai.client.models as models
import pytest
import temporalio.workflow
from mistralai.workflows.core.sandbox import get_sandbox_restrictions
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

import mistralai.workflows as workflows
from conftest import FIXTURE_SKILL
from fake_connectors import ME, FakeGitHubConnector, FakeMistral, FakeSlackConnector
from fake_github import FakeGitHub
from fakes import call
from le_chonk_eval.demo import workflows as demo_workflows
from le_chonk_eval.demo.github import GitHub
from le_chonk_eval.demo.issues import AB_DONE, AB_REQUESTED, DONE, FAILED, READY, Task
from le_chonk_eval.next import connectors, control, vibe
from le_chonk_eval.next.connectors import GITHUB, MARK, SLACK
from le_chonk_eval.next.control import SlackInput, SlackWatcher, TasksInput, TasksWatcher
from le_chonk_eval.next.vibe import VibeAb, VibeInput, VibeRun

REPO = "denis/le-chonk-game"
CHANNEL = "C0LECHONK1"
SKILL_URL = "https://github.com/Kotlin/kotlin-agent-skills/tree/main/skills/kotlin-tooling-kotlin-toolchain"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    remotes = tmp_path / "remotes"
    monkeypatch.setenv("LECHONK_GIT_BASE", remotes.as_uri())
    fake = FakeGitHub(remotes)
    fake.add_repo(REPO)
    fake.add_repo("Kotlin/kotlin-agent-skills")
    fake.add_file("Kotlin/kotlin-agent-skills", "skills/kotlin-tooling-kotlin-toolchain/SKILL.md", FIXTURE_SKILL)
    github, slack = FakeGitHubConnector(fake), FakeSlackConnector()
    monkeypatch.setattr(connectors, "make_connector", lambda name: {GITHUB: github, SLACK: slack}[name])

    def rest() -> GitHub:  # the Mac's GitHub: REST with the token
        return GitHub("t0ken", transport=fake.transport())

    monkeypatch.setattr(demo_workflows, "make_github", rest)
    monkeypatch.setattr(vibe, "make_github", rest)
    mistral = FakeMistral(workflows_by_name={"le-chonk-vibe-run": (VibeRun, VibeInput), "le-chonk-vibe-ab": (VibeAb, VibeInput)})
    monkeypatch.setattr(control, "make_mistral", lambda: mistral)
    monkeypatch.setenv("LECHONK_FAKE_PASS", "1")
    monkeypatch.delenv("DEPLOYMENT_NAME", raising=False)
    return SimpleNamespace(fake=fake, github=github, slack=slack, mistral=mistral, tmp=tmp_path)


def reply(content: str) -> models.MessageOutputEntry:
    return models.MessageOutputEntry(content=content)


def tool_call(tool_id: str, tool: str, arguments: dict[str, Any]) -> models.FunctionCallEntry:
    """A call whose arguments include `name`, which fakes.call takes for the tool's name."""
    import json

    return models.FunctionCallEntry(tool_call_id=tool_id, name=tool, arguments=json.dumps(arguments))


def add_issue(fake: FakeGitHub, label: str, skills: list[str] | None = None, attempts: int | None = None,
              thread: str = "1800000000.000100") -> dict[str, Any]:
    task = Task(title="Add a double jump", goal="Le Chonk can jump once more in the air.", criteria=["A second tap jumps again"],
                skills=skills or [], ab_attempts=attempts, slack_channel=CHANNEL, slack_thread=thread)
    number = fake._number(REPO)
    issue = {"number": number, "title": task.title, "body": task.body(), "state": "open", "labels": [{"name": label}],
             "html_url": f"https://github.com/{REPO}/issues/{number}"}
    fake.issues[REPO].append(issue)
    return issue


def labels(issue: dict[str, Any]) -> set[str]:
    return {label["name"] for label in issue["labels"]}


def watch_slack(**fields: Any) -> Any:
    params = SlackInput(**{"channel": CHANNEL, "repo": REPO, "since": "1700000000.000000", "poll_seconds": 0, "max_polls": 1, **fields})
    return workflows.execute_workflow(SlackWatcher, params)


def run_tasks() -> Any:
    return workflows.execute_workflow(TasksWatcher, TasksInput(repo=REPO, smoke=True, poll_seconds=0, max_polls=1))


def mine(world: SimpleNamespace, thread: str) -> list[str]:
    return [m["text"] for m in world.slack.thread(CHANNEL, thread) if m["text"].startswith(MARK)]


# --- Slack: the triage agent ------------------------------------------------------------------------------------

async def test_a_slack_message_becomes_an_issue_and_a_reply_in_its_thread(world: SimpleNamespace) -> None:
    thread = world.slack.say(CHANNEL, "Le Chonk should jump once more in the air. A second tap jumps again.")
    world.mistral.script = [
        [call("t1", "find_issues", words="double jump")],
        [call("t2", "create_issue", title="Add a double jump", goal="Le Chonk can jump once more in the air.",
              acceptance_criteria=["A second tap in the air jumps again"])],
        [reply("Created #1: https://github.com/denis/le-chonk-game/issues/1. Vibe picks it up next.")],
    ]
    outcome = await watch_slack()

    issue = world.fake.issues[REPO][0]
    task = Task.from_issue(issue["title"], issue["body"])
    assert labels(issue) == {READY} and task.slack_channel == CHANNEL and task.slack_thread == thread and task.requested_by == ME
    assert mine(world, thread) == [f"{MARK} Created #1: https://github.com/denis/le-chonk-game/issues/1. Vibe picks it up next."]
    assert outcome["threads"][thread]["conversation"] == "conv_1"
    assert [kwargs["agent_id"] for kwargs in world.mistral.inputs[:1]] == ["ag_1"]  # the agent, made once
    assert outcome["answered"][0]["thread"] == thread and world.mistral.created[0]["name"] == "le-chonk-triage"


async def test_a_reply_in_the_thread_continues_its_conversation_and_own_messages_are_skipped(world: SimpleNamespace) -> None:
    thread = world.slack.say(CHANNEL, "Le Chonk should double jump.")
    world.mistral.script = [[reply("Which criteria should I check?")], [reply("Thanks, created #1.")]]
    first = await watch_slack()
    assert mine(world, thread) == [f"{MARK} Which criteria should I check?"]

    world.slack.say(CHANNEL, "A second tap jumps again.", thread=thread)
    world.slack.say(CHANNEL, f"{MARK} an old message of the agent")  # skipped: the agent posts as you, with the mark
    again = await watch_slack(since=first["threads"][thread]["active"], threads=first["threads"], agents={})
    assert world.mistral.inputs[-1]["conversation_id"] == "conv_1" and world.mistral.inputs[-1]["inputs"] == "A second tap jumps again."
    assert len(world.mistral.inputs) == 2 and len(again["answered"]) == 1
    assert mine(world, thread)[-1] == f"{MARK} Thanks, created #1."


async def test_setting_the_project_starts_the_tasks_watcher_on_your_behalf(world: SimpleNamespace) -> None:
    world.slack.say(CHANNEL, "Use https://github.com/denis/le-chonk-game")
    world.mistral.script = [[call("t1", "set_project", url="https://github.com/denis/le-chonk-game")], [reply("Watching it now.")]]
    outcome = await watch_slack(repo=None, smoke=True)
    started = world.mistral.started[0]
    assert started["workflow_identifier"] == "le-chonk-tasks" and started["deployment_name"] == "le-chonk-control"
    assert started["execution_id"] == "le-chonk-tasks-denis-le-chonk-game-a" and started["input"]["repo"] == REPO and started["input"]["smoke"]
    assert started["input"]["fresh"] and started["input"]["generation"] == "a"
    assert outcome["repo"] == REPO and outcome["answered"][0]["started"] == {REPO: "started (le-chonk-tasks-denis-le-chonk-game-a)"}


# --- A repo's tasks: the cloud and the Mac ----------------------------------------------------------------------

async def test_a_task_runs_on_the_mac_and_the_cloud_merges_it(world: SimpleNamespace) -> None:
    thread = world.slack.say(CHANNEL, "the request")
    issue = add_issue(world.fake, READY, thread=thread)
    outcome = await run_tasks()

    handled = outcome["handled"][0]
    run_id = handled["run_id"]
    assert handled["result"]["merged"] and handled["result"]["done"] and handled["result"]["verdict"] == "done"
    started = world.mistral.started[0]
    assert started["workflow_identifier"] == "le-chonk-vibe-run" and started["deployment_name"] == "le-chonk-vibe"
    assert started["execution_id"] == f"le-chonk-vibe-denis-le-chonk-game-{run_id}" and started["input"]["run_id"] == run_id
    fake = world.fake
    release = fake.releases[REPO][0]  # made by the Mac, with its token
    assert release["tag_name"] == run_id and [a["name"] for a in release["assets"]] == ["le-chonk-issue-1.apk"]
    pull = fake.pulls[REPO][0]  # made and merged by the cloud, with the connector
    assert pull["merged"] and pull["head"] == f"vibe/{run_id}" and "Release with the APK" in pull["body"]
    assert "Made up by the fake harbor" in fake.branch_file(REPO, "main", f"shared/src/{run_id}.kt")
    assert issue["state"] == "closed" and labels(issue) == {DONE}
    comments = fake.comments[(REPO, 1)]
    assert comments[0].startswith("Started the implementation: run") and comments[-1].startswith("Merged Vibe's change: PR #2")
    assert "**Judge: done**" in comments[-1]
    said = mine(world, thread)
    assert said[0].startswith(f"{MARK} Started the implementation of #1") and "merged in [PR #2](https://github.com/denis/le-chonk-game/pull/2)" in said[-1]
    assert [tool for tool, _ in world.github.calls if tool in ("create_pull_request", "pull_request_read", "merge_pull_request")] == [
        "create_pull_request", "pull_request_read", "merge_pull_request"]


async def test_a_change_that_doesnt_build_is_not_merged(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LECHONK_FAKE_PASS", "0")
    thread = world.slack.say(CHANNEL, "the request")
    issue = add_issue(world.fake, READY, thread=thread)
    outcome = await run_tasks()
    assert outcome["handled"][0]["result"] == {"merged": False, "reason": "the Android APK didn't build"}
    assert labels(issue) == {FAILED} and not world.fake.pulls[REPO] and not world.fake.releases[REPO]
    assert "nothing merged" in mine(world, thread)[-1]


async def test_a_broken_run_on_the_mac_frees_the_issue(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(params: Any) -> dict[str, Any]:
        raise RuntimeError("Docker isn't running")

    monkeypatch.setattr(vibe, "run_harbor_job", broken)
    thread = world.slack.say(CHANNEL, "the request")
    issue = add_issue(world.fake, READY, thread=thread)
    outcome = await run_tasks()
    assert outcome["handled"][0]["result"] == {"ok": False, "error": "RuntimeError: Docker isn't running"}
    assert labels(issue) == {FAILED}
    assert "failed: RuntimeError: Docker isn't running" in world.fake.comments[(REPO, 1)][-1]
    assert "the run failed" in mine(world, thread)[-1]


async def test_a_run_that_never_comes_back_fails_the_task(world: SimpleNamespace) -> None:
    world.mistral.status_override = "WorkflowExecutionStatus.TERMINATED"
    issue = add_issue(world.fake, READY)
    outcome = await run_tasks()
    assert outcome["handled"][0]["result"]["error"] == "the run on your Mac ended: terminated"
    assert labels(issue) == {FAILED}


async def test_an_ab_runs_on_the_mac_and_its_report_comes_back(world: SimpleNamespace) -> None:
    thread = world.slack.say(CHANNEL, "the request")
    issue = add_issue(world.fake, AB_REQUESTED, skills=[SKILL_URL], attempts=2, thread=thread)
    outcome = await run_tasks()

    result = outcome["handled"][0]["result"]
    run_id = outcome["handled"][0]["run_id"]
    assert world.mistral.started[0]["workflow_identifier"] == "le-chonk-vibe-ab"
    release = world.fake.releases[REPO][0]
    assert release["prerelease"] and release["tag_name"] == run_id and result["release"] == release["html_url"]
    assert sorted(a["name"] for a in release["assets"]) == ["ab-report.html", "le-chonk-issue-1-no-skill.apk", "le-chonk-issue-1-skill.apk"]
    assert labels(issue) == {AB_DONE} and not world.fake.pulls[REPO]
    assert "| skill | 2 |" in world.fake.comments[(REPO, 1)][-1]
    assert mine(world, thread)[-1].startswith(f"{MARK} A/B for #1 Add a double jump: ") and "[Report and APKs](" in mine(world, thread)[-1]


# --- Wiring -----------------------------------------------------------------------------------------------------

async def test_start_resolves_the_channel_and_starts_both_watchers(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from le_chonk_eval.next import start

    client = world.mistral
    client.beta.connectors = SimpleNamespace(call_tool_async=_slack_search)
    monkeypatch.setattr(start, "_mistral", lambda: client)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    started = await start.start("#le-chonk", "https://github.com/denis/le-chonk-game", {"smoke": True})
    assert started == {"channel": "C0LECHONK1", "tasks": "started (le-chonk-tasks-denis-le-chonk-game-a)",
                       "slack": "started (le-chonk-slack-c0lechonk1-a)"}
    assert [s["deployment_name"] for s in client.started] == ["le-chonk-control", "le-chonk-control"]
    assert client.started[1]["input"]["channel"] == "C0LECHONK1" and client.started[1]["input"]["since"] is None
    again = await start.start("C0LECHONK1", REPO, {})
    assert again["slack"] == "already running (le-chonk-slack-c0lechonk1-a)" and len(client.started) == 2
    assert (await start.stop("C0LECHONK1", REPO)) == {"le-chonk-slack-c0lechonk1-a": "stopped", "le-chonk-tasks-denis-le-chonk-game-a": "stopped"}
    assert (await start.stop("C0LECHONK1", None)) == {"le-chonk-slack-c0lechonk1": "not running"}


async def _slack_search(**kwargs: Any) -> dict[str, Any]:
    return await FakeSlackConnector().call_tool("slack_search_channels", kwargs["arguments"])


async def test_a_channel_is_found_by_its_exact_name_only() -> None:
    """Every new message in the channel is a request for the agent: a near match isn't taken for it."""
    from le_chonk_eval.next.start import find_channel

    client = SimpleNamespace(beta=SimpleNamespace(connectors=SimpleNamespace(call_tool_async=_slack_search)))
    assert await find_channel(client, "le-chonk-old") == "C0LECHONK2"
    assert await find_channel(client, "C0LECHONK2") == "C0LECHONK2"  # an ID isn't looked up
    with pytest.raises(SystemExit, match="No channel is named #le-ch. Found: #le-chonk, #le-chonk-old"):
        await find_channel(client, "le-ch")
    with pytest.raises(SystemExit, match="No channel is named #chonk-team. Give its exact name"):
        await find_channel(client, "#chonk-team")


@pytest.mark.parametrize("workflow", [SlackWatcher, TasksWatcher, VibeRun, VibeAb])
async def test_the_workflows_import_inside_the_sandbox(workflow: type) -> None:
    runner_ = SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions())
    runner_.prepare_workflow(temporalio.workflow._Definition.must_from_class(workflow))


# --- What the review found -------------------------------------------------------------------------------------

async def test_a_watcher_hands_over_to_a_new_execution_instead_of_continuing_as_new(world: SimpleNamespace) -> None:
    """Continue-as-new drops the on-behalf-of identity: a new execution, started through the API, gets it again."""
    thread = world.slack.say(CHANNEL, "hello")
    world.mistral.script = [[reply("Hi.")]]
    params = SlackInput(channel=CHANNEL, repo=REPO, since="1700000000.000000", poll_seconds=0, hand_over_after=1)
    outcome = await workflows.execute_workflow(SlackWatcher, params)
    assert outcome["handed_over"] == "started (le-chonk-slack-c0lechonk1-b)"
    successor = world.mistral.started[-1]
    assert successor["workflow_identifier"] == "le-chonk-slack" and successor["deployment_name"] == "le-chonk-control"
    state = successor["input"]
    assert state["generation"] == "b" and not state["fresh"] and state["polls"] == 0 and state["since"] == thread
    assert state["threads"][thread]["conversation"] == "conv_1" and state["agents"]["le-chonk-triage"]["id"] == "ag_1"


async def test_a_fresh_tasks_watcher_frees_issues_a_stopped_one_left_busy(world: SimpleNamespace) -> None:
    from le_chonk_eval.demo.issues import RUNNING

    issue = add_issue(world.fake, RUNNING)
    outcome = await run_tasks()
    assert outcome["handled"][0] == {"freed": [1]} and labels(issue) == {FAILED}
    assert "stopped during this run" in world.fake.comments[(REPO, 1)][-1]


async def test_a_status_check_that_fails_once_doesnt_lose_the_run(world: SimpleNamespace) -> None:
    world.mistral.status_errors = 2
    issue = add_issue(world.fake, READY)
    outcome = await run_tasks()
    assert outcome["handled"][0]["result"]["merged"] and labels(issue) == {DONE}


async def test_a_reply_also_sent_to_the_channel_is_answered_and_system_messages_arent(world: SimpleNamespace) -> None:
    thread = world.slack.say(CHANNEL, "Le Chonk should double jump.")
    world.mistral.script = [[reply("Which criteria?")], [reply("Got it.")]]
    first = await watch_slack()
    world.slack.say(CHANNEL, "<@U2|Ana Example> has joined the channel", user="U2")
    world.slack.say(CHANNEL, 'has renamed the channel from "new-channel" to "le-chonk"')
    world.slack.say(CHANNEL, "set the channel topic: the demo")
    broadcast = world.slack.say(CHANNEL, "A second tap jumps.", thread=thread, broadcast=True)
    again = await watch_slack(since=first["threads"][thread]["active"], threads=first["threads"], agents={})
    assert [a.get("ts") for a in again["answered"]] == [broadcast]  # once, in its thread
    assert world.mistral.inputs[-1]["inputs"] == "A second tap jumps." and len(world.mistral.inputs) == 2


async def test_replies_in_a_thread_are_answered_when_nothing_new_is_in_the_channel(world: SimpleNamespace) -> None:
    """As on the first real run: the agent asked for criteria, and the answer came in two messages."""
    thread = world.slack.say(CHANNEL, "Build Le Chonk Runner: an endless runner where Le Chonk jumps over obstacles.")
    world.mistral.script = [[reply("Which acceptance criteria?")], [reply("Thanks, filing it.")]]
    first = await watch_slack()
    world.slack.say(CHANNEL, "1. Yes\n2. Yes\n3. Let's have 5 hearts as health", thread=thread)
    world.slack.say(CHANNEL, "4. YES\n5. Yes", thread=thread)
    again = await watch_slack(since=first["threads"][thread]["active"], threads=first["threads"], agents={})
    assert len(again["answered"]) == 1 and "error" not in again["answered"][0]  # one request, not two
    assert world.mistral.inputs[-1]["inputs"] == "1. Yes\n2. Yes\n3. Let's have 5 hearts as health\n\n4. YES\n5. Yes"
    assert mine(world, thread)[-1] == f"{MARK} Thanks, filing it."


async def test_a_channel_that_cant_be_read_is_said_once_and_its_return_too(world: SimpleNamespace) -> None:
    original, failing = world.slack.call_tool, {"left": 9}  # 3 polls: each read is tried 3 times

    async def flaky(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if tool == "slack_read_channel" and failing["left"]:
            failing["left"] -= 1
            raise RuntimeError("ratelimited")
        return await original(tool, arguments)

    world.slack.call_tool = flaky
    outcome = await watch_slack(max_polls=5)
    told = [m["text"] for m in world.slack.messages.get(CHANNEL, []) if "thread_ts" not in m and m["text"].startswith(MARK)]
    assert len(told) == 2 and "I can't read this channel" in told[0] and "ratelimited" in told[0] and "again" in told[1]
    assert sum(1 for a in outcome["answered"] if "error" in a) == 3


async def test_one_thread_that_cant_be_read_doesnt_stop_the_channel(world: SimpleNamespace) -> None:
    broken = world.slack.say(CHANNEL, "first")
    world.mistral.script = [[reply("ok")], [reply("ok too")]]
    first = await watch_slack()
    original = world.slack.call_tool

    async def failing(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if tool == "slack_read_thread" and (arguments or {}).get("message_ts") == broken:
            raise RuntimeError("thread_not_found")
        return await original(tool, arguments)

    world.slack.call_tool = failing
    world.slack.say(CHANNEL, "second")
    again = await watch_slack(since=first["threads"][broken]["active"], threads=first["threads"], agents={})
    assert len(again["answered"]) == 1 and again["threads"][broken]["failures"] == 1


def test_execution_ids_take_any_repo_name() -> None:
    from le_chonk_eval.next.runs import tasks_watch_id, vibe_run_id

    assert tasks_watch_id("denis/le-chonk-game") == "le-chonk-tasks-denis-le-chonk-game"
    odd = tasks_watch_id("you/le.chonk")
    assert odd.startswith("le-chonk-tasks-you-le-chonk-") and len(odd) == len("le-chonk-tasks-you-le-chonk-") + 6
    assert tasks_watch_id("you/le.chonk") != tasks_watch_id("you/le-chonk")
    assert vibe_run_id("a/b", "issue-1-20261021-120000") != vibe_run_id("c/d", "issue-1-20261021-120000")


async def test_the_agent_creates_the_apps_repo_when_asked_and_watches_it(world: SimpleNamespace) -> None:
    world.slack.say(CHANNEL, "Create a private repo le-chonk-app for the app, and use it.")
    world.mistral.script = [
        [tool_call("t1", "create_repo", {"name": "le-chonk-app", "description": "Le Chonk Runner, built by Vibe"})],
        [call("t2", "set_project", url="https://github.com/denis/le-chonk-app")],
        [reply("Created https://github.com/denis/le-chonk-app and watching it.")],
    ]
    outcome = await watch_slack(repo=None)
    assert world.fake.repos["denis/le-chonk-app"]["private"] and outcome["repo"] == "denis/le-chonk-app"
    made = next(args for tool, args in world.github.calls if tool == "create_repository")
    assert made["private"] is True and made["autoInit"] is False and made["name"] == "le-chonk-app"
    assert world.mistral.started[0]["execution_id"] == "le-chonk-tasks-denis-le-chonk-app-a"  # found though the search hasn't indexed it
    assert '"repo": "denis/le-chonk-app"' in world.mistral.inputs[1]["inputs"][0].result


async def test_a_repo_github_wont_make_comes_back_as_what_to_do(world: SimpleNamespace) -> None:
    """As on the first real run: the Mistral GitHub App may not create repositories."""
    world.fake.refuse_repos = True
    thread = world.slack.say(CHANNEL, "Create a private repo le-chonk-game for the app, and use it.")
    world.mistral.script = [
        [tool_call("t1", "create_repo", {"name": "le-chonk-game"})],
        [reply("The repo creation failed. Let me check the current project setup."), call("t2", "get_project")],
        [reply("GitHub won't let me create it. Create le-chonk-game, give the Mistral GitHub App access, and send me its URL.")],
    ]
    await watch_slack(repo=None)
    refused = world.mistral.inputs[1]["inputs"][0].result
    assert "403" in refused and "github.com/apps/mistralai" in refused and "call set_project" in refused
    assert mine(world, thread) == [f"{MARK} GitHub won't let me create it. Create le-chonk-game, give the Mistral GitHub App access, "
                                   "and send me its URL."]  # its words between tool calls aren't posted


async def test_a_bad_repo_name_is_refused_before_anything_is_made(world: SimpleNamespace) -> None:
    world.slack.say(CHANNEL, "Create a repo called my app")
    world.mistral.script = [[tool_call("t1", "create_repo", {"name": "my app"})], [reply("That name has a space.")]]
    await watch_slack(repo=None)
    assert "can't be a repository name" in world.mistral.inputs[1]["inputs"][0].result
    assert not any(tool == "create_repository" for tool, _ in world.github.calls)
