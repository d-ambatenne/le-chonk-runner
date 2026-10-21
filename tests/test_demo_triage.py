"""The triage agent and the Slack app, with a scripted Mistral client, an in-memory GitHub and a fake Slack."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mistralai.client.models as models
import pytest

from conftest import FIXTURE_SKILL
from fake_github import FakeGitHub
from fakes import FakeClient, call
from le_chonk_eval.demo.github import GitHub
from le_chonk_eval.demo.issues import AB_REQUESTED, READY, Task
from le_chonk_eval.demo.slack_bot import answer, build_app, should_answer
from le_chonk_eval.demo.triage import PROJECTS, THREADS, SlackContext, Triage, TriageTools

REPO = "denis/le-chonk-game"
SKILL_URL = "https://github.com/Kotlin/kotlin-agent-skills/tree/main/skills/kotlin-tooling-kotlin-toolchain"
THREAD = SlackContext("C1", "1712.5", "U1")


def text(content: str) -> models.MessageOutputEntry:
    return models.MessageOutputEntry(content=content)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    monkeypatch.setenv("LECHONK_GIT_BASE", (tmp_path / "remotes").as_uri())
    fake = FakeGitHub(tmp_path / "remotes")
    fake.add_repo(REPO)
    fake.add_repo("Kotlin/kotlin-agent-skills")
    fake.add_file("Kotlin/kotlin-agent-skills", "skills/kotlin-tooling-kotlin-toolchain/SKILL.md", FIXTURE_SKILL)
    return fake


def results(client: FakeClient, index: int) -> dict[str, Any]:
    return {entry.tool_call_id: entry.result for entry in client.inputs[index]["inputs"]}


async def test_a_request_becomes_an_issue(fake: FakeGitHub) -> None:
    started: list[str] = []

    async def start_watcher(repo: str) -> str:
        started.append(repo)
        return "started"

    client = FakeClient([
        [call("t1", "get_project")],
        [call("t2", "set_project", url="https://github.com/denis/le-chonk-game")],
        [call("t3", "find_issues", words="double jump")],
        [call("t4", "create_issue", title="Add a double jump", goal="Le Chonk can jump once more in the air.",
              acceptance_criteria=["A second tap in the air jumps again", "A third tap does nothing"])],
        [text("Created #1: https://github.com/denis/le-chonk-game/issues/1. Vibe picks it up next.")],
    ])
    async with GitHub("t0ken", transport=fake.transport()) as github:
        triage = Triage(client, github, start_watcher)
        reply = await triage.handle(THREAD, "Use https://github.com/denis/le-chonk-game. Le Chonk should double jump.")

    assert reply.startswith("Created #1") and started == [REPO]
    assert json.loads(results(client, 1)["t1"]) == {"repo": None, "url": None}
    assert json.loads(results(client, 2)["t2"])["watcher"] == "started"
    assert json.loads(results(client, 3)["t3"]) == {"issues": []}
    assert PROJECTS.get("C1") == REPO and THREADS.get("C1:1712.5") == "conv_1" and Triage.knows(THREAD)
    issue = fake.issues[REPO][0]
    assert [label["name"] for label in issue["labels"]] == [READY] and len(fake.labels[REPO]) == 9
    task = Task.from_issue(issue["title"], issue["body"])
    assert task.criteria == ["A second tap in the air jumps again", "A third tap does nothing"]
    assert (task.slack_channel, task.slack_thread, task.requested_by) == ("C1", "1712.5", "U1")

    created = client.created[0]
    assert created["name"] == "le-chonk-triage" and created["model"] == "mistral-large-4"
    assert "No open issue asks for the same outcome" in created["instructions"]  # from demo/task-checklist.md
    assert {t["function"]["name"] for t in created["tools"]} >= {"create_issue", "add_skill", "mark_for_ab", "queue_issue"}


async def test_skills_and_ab_in_the_same_thread(fake: FakeGitHub) -> None:
    PROJECTS.set("C1", REPO)
    THREADS.set("C1:1712.5", "conv_1")
    fake.issues[REPO].append({"number": 1, "title": "Add a double jump", "state": "open", "labels": [{"name": READY}],
                              "body": Task(title="Add a double jump", goal="Jump twice.").body(), "html_url": "https://github.com/x"})
    client = FakeClient([
        [call("t1", "add_skill", number=1, url=SKILL_URL)],
        [call("t2", "mark_for_ab", number=1, attempts=20)],
        [text("Skill added; the A/B runs 8 times per arm.")],
    ])
    async with GitHub("t0ken", transport=fake.transport()) as github:
        reply = await Triage(client, github).handle(THREAD, f"Add {SKILL_URL} to #1 and A/B it")

    assert reply.startswith("Skill added") and client.inputs[0] == {"conversation_id": "conv_1", "inputs": f"Add {SKILL_URL} to #1 and A/B it"}
    assert json.loads(results(client, 1)["t1"])["skill"] == "kotlin-tooling-kotlin-toolchain"
    assert json.loads(results(client, 2)["t2"])["attempts_per_arm"] == 8
    issue = fake.issues[REPO][0]
    task = Task.from_issue(issue["title"], issue["body"])
    assert task.skills == [SKILL_URL] and task.ab_attempts == 8
    assert [label["name"] for label in issue["labels"]] == [AB_REQUESTED]


async def test_an_ab_on_a_done_issue_reopens_it(fake: FakeGitHub) -> None:
    PROJECTS.set("C1", REPO)
    body = Task(title="Double jump", goal="Jump twice.", skills=[SKILL_URL]).body()
    fake.issues[REPO].append({"number": 1, "title": "Double jump", "state": "closed", "labels": [{"name": "vibe:done"}, {"name": "ab:running"}],
                              "body": body})
    async with GitHub("t0ken", transport=fake.transport()) as github:
        assert json.loads(await TriageTools(github, THREAD).call("mark_for_ab", {"number": 1}))["attempts_per_arm"] == 3
    issue = fake.issues[REPO][0]
    assert issue["state"] == "open" and {label["name"] for label in issue["labels"]} == {"vibe:done", AB_REQUESTED}


async def test_tools_report_problems_to_the_agent(fake: FakeGitHub) -> None:
    async with GitHub("t0ken", transport=fake.transport()) as github:
        tools = TriageTools(github, SlackContext("C9", "1.0"))
        assert (await tools.call("create_issue", {"title": "x", "goal": "y", "acceptance_criteria": []})).startswith("Error: No project is set")
        assert (await tools.call("browse", {})).startswith("Error: unknown tool")
        PROJECTS.set("C9", REPO)
        assert "wrong arguments" in await tools.call("get_issue", {"id": 1})
        fake.issues[REPO].append({"number": 1, "title": "T", "body": "Goal.", "state": "closed", "labels": [{"name": "vibe:failed"}, {"name": "vibe:running"}]})
        assert "has no skill" in await tools.call("mark_for_ab", {"number": 1})
        assert "No SKILL.md" in await tools.call("add_skill", {"number": 1, "url": SKILL_URL + "-x"})
        assert json.loads(await tools.call("queue_issue", {"number": 1}))["labels"] == [READY]
        held = json.loads(await tools.call("create_issue", {"title": "Best score", "goal": "Show it.", "acceptance_criteria": ["Shown"], "hold": True}))
        assert held["labels"] == [] and fake.issues[REPO][-1]["labels"] == []
        assert fake.issues[REPO][0]["state"] == "open" and [label["name"] for label in fake.issues[REPO][0]["labels"]] == [READY]
        assert (await tools.call("set_project", {"url": "https://github.com/denis/missing"})).startswith("Error: GitHub 404")


async def test_a_failed_turn_starts_the_thread_over(fake: FakeGitHub) -> None:
    THREADS.set("C1:1712.5", "conv_1")
    client = FakeClient([], fail_after=0)
    async with GitHub("t0ken", transport=fake.transport()) as github:
        reply = await Triage(client, github).handle(THREAD, "Hello?")
    assert reply.startswith("Something went wrong: RuntimeError: API unavailable")
    assert THREADS.get("C1:1712.5") == "" and Triage.knows(THREAD)  # still followed: the next message starts over
    client = FakeClient([[text("Hello again.")]])
    async with GitHub("t0ken", transport=fake.transport()) as github:
        assert await Triage(client, github).handle(THREAD, "Hello?") == "Hello again."
    assert "agent_id" in client.inputs[0] and THREADS.get("C1:1712.5") == "conv_1"


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        async def record(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, kwargs))
            return {"ok": True}

        return record


class EchoTriage:
    async def handle(self, context: SlackContext, text: str) -> str:
        return f"{context.key} {context.user}: {text}"


def test_which_messages_the_app_answers() -> None:
    THREADS.set("C1:1712.5", "conv_1")
    base = {"channel": "C1", "ts": "1712.9", "user": "U1", "text": "hi"}
    assert should_answer({**base, "text": "<@B1> hi"}, "app_mention", "B1")
    assert not should_answer({**base, "text": "<@B1> hi"}, "message", "B1")  # answered as the mention
    assert should_answer({**base, "thread_ts": "1712.5"}, "message", "B1")
    assert not should_answer({**base, "thread_ts": "1700.0"}, "message", "B1")
    assert not should_answer(base, "message", "B1") and should_answer({**base, "channel_type": "im"}, "message", "B1")
    assert should_answer({**base, "channel_type": "im", "text": "<@B1> hi"}, "message", "B1")  # no app_mention in DMs
    assert not should_answer({**base, "bot_id": "B2"}, "app_mention", "B1")
    assert not should_answer({**base, "user": "B1"}, "app_mention", "B1")


async def test_the_reply_goes_to_the_thread() -> None:
    slack = FakeSlack()
    reply = await answer({"channel": "C1", "ts": "1712.5", "user": "U1", "text": "<@B1> add a double jump"}, slack, EchoTriage())
    assert reply == "C1:1712.5 U1: add a double jump"
    assert [name for name, _ in slack.calls] == ["reactions_add", "reactions_remove", "chat_postMessage"]
    assert slack.calls[-1][1] == {"channel": "C1", "thread_ts": "1712.5", "text": reply, "unfurl_links": False, "unfurl_media": False}


def test_the_bolt_app_is_built() -> None:
    assert build_app(EchoTriage(), bot_token="xoxb-test") is not None  # type: ignore[arg-type]
