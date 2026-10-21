"""The connector clients: GitHub's and Slack's MCP results read into the shapes the demo's code uses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FIXTURE_SKILL
from fake_connectors import FakeGitHubConnector, FakeSlackConnector
from fake_github import FakeGitHub
from le_chonk_eval.demo.github import check_skill
from le_chonk_eval.next.connectors import (
    MARK,
    ConnectorGitHub,
    ConnectorNotifier,
    ConnectorSlack,
    diff_summary,
    is_mine,
    is_person,
    normalize_issue,
    parse_channels,
    parse_messages,
)

REPO = "denis/le-chonk-game"
SKILL_URL = "https://github.com/Kotlin/kotlin-agent-skills/tree/main/skills/kotlin-tooling-kotlin-toolchain"


@pytest.fixture
def github(tmp_path: Path) -> ConnectorGitHub:
    fake = FakeGitHub(tmp_path / "remotes")
    fake.add_repo(REPO)
    fake.add_repo("Kotlin/kotlin-agent-skills")
    fake.add_file("Kotlin/kotlin-agent-skills", "skills/kotlin-tooling-kotlin-toolchain/SKILL.md", FIXTURE_SKILL)
    return ConnectorGitHub(FakeGitHubConnector(fake))


def test_issues_read_the_same_from_rest_or_graphql_shapes() -> None:
    graphql = {"number": 3, "title": "Jump", "body": None, "state": "OPEN", "labels": {"nodes": [{"name": "vibe:ready"}]},
               "url": "https://github.com/denis/le-chonk-game/issues/3"}
    rest = {"number": 3, "title": "Jump", "body": "", "state": "open", "labels": [{"name": "vibe:ready"}],
            "url": "https://api.github.com/repos/denis/le-chonk-game/issues/3", "pull_request": {"url": "x"}}
    first, second = normalize_issue(graphql, REPO), normalize_issue(rest, REPO)
    assert first == {k: v for k, v in second.items() if k != "pull_request"} and "pull_request" in second
    assert first["html_url"] == "https://github.com/denis/le-chonk-game/issues/3" and first["labels"] == [{"name": "vibe:ready"}]


async def test_issues_labels_and_comments_go_through_the_connector(github: ConnectorGitHub) -> None:
    created = await github.create_issue(REPO, "Double jump", "Jump twice.", ["vibe:ready", "game"])
    assert created["number"] == 1 and created["html_url"] == "https://github.com/denis/le-chonk-game/issues/1"
    await github.relabel(REPO, 1, add=["vibe:running"], remove=["vibe:ready"])
    assert [label["name"] for label in (await github.issue(REPO, 1))["labels"]] == ["game", "vibe:running"]  # yours stay
    await github.comment(REPO, 1, "Started.")
    assert github.connector.fake.comments[(REPO, 1)] == ["Started."]
    assert [i["number"] for i in await github.search_issues(REPO, "twice")] == [1]
    await github.update_issue(REPO, 1, state="closed", state_reason="completed", milestone=3)  # what issue_write can't take is dropped
    assert (await github.issue(REPO, 1))["state"] == "closed"


async def test_the_issue_list_follows_its_pages(github: ConnectorGitHub) -> None:
    for n in range(130):
        await github.create_issue(REPO, f"Task {n}", "", [])
    issues = await github.issues(REPO, state="open")
    assert len(issues) == 130 and issues[-1]["title"] == "Task 129"
    calls = [args for tool, args in github.connector.calls if tool == "list_issues"]
    assert [c.get("after") for c in calls] == [None, "100"] and calls[0]["state"] == "OPEN"


async def test_skills_are_read_through_the_connector(github: ConnectorGitHub) -> None:
    skill = await check_skill(github, SKILL_URL)
    assert skill["name"] == "kotlin-tooling-kotlin-toolchain"
    sha = "0123456789abcdef0123456789abcdef01234567"  # a skill pinned to a commit, for an A/B
    github.connector.fake.add_file("Kotlin/kotlin-agent-skills", "skills/kotlin-tooling-kotlin-toolchain/SKILL.md", FIXTURE_SKILL, ref=sha)
    assert (await check_skill(github, SKILL_URL.replace("/tree/main/", f"/tree/{sha}/")))["name"] == "kotlin-tooling-kotlin-toolchain"
    with pytest.raises(ValueError, match="No SKILL.md"):
        await check_skill(github, SKILL_URL.replace("kotlin-tooling-kotlin-toolchain", "missing"))
    assert await github.file_text("Kotlin/kotlin-agent-skills", "skills/missing/SKILL.md", "main") is None
    assert (await github.repo(REPO))["default_branch"] == "main"
    with pytest.raises(Exception, match="can't see it"):
        await github.repo("denis/missing")


def test_a_tools_error_keeps_its_text_and_can_be_retried() -> None:
    from mistralai.workflows.plugins.mistralai.connectors.exceptions import ConnectorToolCallError

    from le_chonk_eval.demo.github import GitHubError
    from le_chonk_eval.next.connectors import ConnectorError, tool_error

    def failed(message: str) -> ConnectorToolCallError:
        return ConnectorToolCallError("github_app", response={"content": [{"type": "text", "text": message}]})

    missing = tool_error("github_app", "issue_read", failed("failed to get issue: GET https://api.github.com/repos/a/b/issues/9: 404 Not Found []"))
    assert isinstance(missing, GitHubError) and missing.status == 404 and "issue_read: failed to get issue" in str(missing)
    conflict = tool_error("github_app", "merge_pull_request", failed("failed to merge: PUT https://api.github.com/x: 405 Pull Request is not mergeable []"))
    assert conflict.status == 405 and "not mergeable" in str(conflict)
    slack = tool_error("slack", "slack_read_thread", failed("thread_not_found"))
    assert isinstance(slack, ConnectorError) and slack.status == 404 and "thread_not_found" in str(slack)
    assert tool_error("github_app", "list_issues", RuntimeError("socket closed")).status == 502


def test_a_diff_is_summed_up_for_the_judge() -> None:
    diff = "diff --git a/shared/src/Game.kt b/shared/src/Game.kt\n--- a/shared/src/Game.kt\n+++ b/shared/src/Game.kt\n-old\n+new\n+more\n"
    summary = diff_summary(diff, 10)
    assert summary["files"] == ["shared/src/Game.kt"] and summary["stat"] == " 1 files changed, 2 insertions(+), 1 deletions(-)"
    assert summary["truncated"] and len(summary["diff"]) == 10


# As scripts/check_next.py saw them, with the names changed
CHANNEL_TEXT = json.dumps({"messages": (
    "Channel: #le-chonk (C0EXAMPLE1)\n\n"
    "=== Message from Denis Example <denis@example.com> (U0EXAMPLE1) at 2026-10-21 16:48:42 CEST === \nMessage TS: 1792594122.709679\n"
    "C'mon\n\n"
    "=== Message from Denis Example <denis@example.com> (U0EXAMPLE1) at 2026-10-21 16:48:22 CEST === \nMessage TS: 1792594102.216779\n"
    "Build Le Chonk Runner.\n\nLe Chonk jumps over obstacles.\nThread: 2 replies (latest: 2026-10-21 16:48:42 CEST)\n\n"
    "=== Message from Denis Example <denis@example.com> (U0EXAMPLE1) at 2026-10-21 14:13:23 CEST === \nMessage TS: 1792584803.510259\n"
    'has renamed the channel from "new-channel" to "le-chonk"\n\n'
    "=== Message from Denis Example <denis@example.com> (U0EXAMPLE1) at 2026-10-21 14:12:23 CEST === \nMessage TS: 1792584743.916209\n"
    "<@U0EXAMPLE1|Denis Example> has joined the channel"), "pagination_info": "There are no more messages available.\n"})
THREAD_TEXT = json.dumps({"messages": (
    "=== THREAD PARENT MESSAGE ===\nFrom: Denis Example <denis@example.com> (U0EXAMPLE1)\nTime: 2026-10-21 16:48:22 CEST\n"
    "Message TS: 1792594102.216779\nBuild Le Chonk Runner.\n\nLe Chonk jumps over obstacles.\n\n=== THREAD REPLIES (2 total) ===\n\n"
    "--- Reply 1 of 2 ---\nFrom: Ana Example <ana@example.com> (U0ANA00001)\nTime: 2026-10-21 16:48:31 CEST\n"
    "Message TS: 1792594111.294259\n:robot_face: Filed #1.\n\n"
    "--- Reply 2 of 2 ---\nFrom: Denis Example <denis@example.com> (U0EXAMPLE1)\nTime: 2026-10-21 16:48:42 CEST\n"
    "Message TS: 1792594122.709679\nC'mon\n"), "pagination_info": "There are no more messages in this thread.\n"})
NO_REPLIES_TEXT = json.dumps({"messages": (
    "=== THREAD PARENT MESSAGE ===\nFrom: Denis Example <denis@example.com> (U0EXAMPLE1)\nTime: 2026-10-21 16:48:42 CEST\n"
    "Message TS: 1792594122.709679\nC'mon\n\nNo thread messsages\n"), "pagination_info": "There are no more messages in this thread.\n"})
SEARCH_TEXT = json.dumps({"results": (
    "# Search Results for: \n\n## Channels (2 results)\n### Result 1 of 2\nName: #le-chonk-team\n"
    "Creator: Denis Example (<@U0EXAMPLE1)\nCreated: 2026-10-21 14:12:23 CEST\nPurpose: This channel is for everything #new-channel.\n"
    "Permalink: [link](https://example.slack.com/archives/C0EXAMPLE1)\nIs Archived: false\nChannel Type: public_channel\n\n---\n\n"
    "### Result 2 of 2\nName: #le-chonk\nPurpose: \nPermalink: [link](https://example.slack.com/archives/C0LECHONK1)\n\n---\n\n"),
    "pagination_info": "End of results - No more pages available.\\n"})


def test_slack_messages_are_read_from_slacks_text() -> None:
    broadcast, newest, renamed, joined = parse_messages(CHANNEL_TEXT)
    assert newest["ts"] == "1792594102.216779" and newest["user"] == "U0EXAMPLE1" and is_person(newest)
    assert newest["text"] == "Build Le Chonk Runner.\n\nLe Chonk jumps over obstacles."  # without Slack's line about its thread
    assert broadcast["text"] == "C'mon" and not is_person(renamed) and not is_person(joined)


def test_a_threads_replies_are_read_from_slacks_text() -> None:
    parent, filed, more = parse_messages(THREAD_TEXT)
    assert parent["text"] == "Build Le Chonk Runner.\n\nLe Chonk jumps over obstacles." and parent["ts"] == "1792594102.216779"
    assert (filed["user"], filed["text"], filed["ts"]) == ("U0ANA00001", ":robot_face: Filed #1.", "1792594111.294259") and not is_person(filed)
    assert (more["user"], more["text"], more["ts"]) == ("U0EXAMPLE1", "C'mon", "1792594122.709679") and is_person(more)
    alone, = parse_messages(NO_REPLIES_TEXT)
    assert alone["text"] == "C'mon"


def test_slack_messages_are_read_from_json_too() -> None:
    from_json = parse_messages('{"messages": [{"ts": "1800000001.000100", "user": "U1", "text": "hi", "thread_ts": "1800000000.000100"}]}')
    assert from_json[0]["ts"] == "1800000001.000100" and from_json[0]["thread_ts"] == "1800000000.000100" and from_json[0]["text"] == "hi"
    assert is_mine({"text": f"{MARK} Created #1"}) and is_mine({"text": ":robot_face: Created #1"}) and not is_mine({"text": "Created"})


def test_channels_are_read_from_slacks_search() -> None:
    assert parse_channels(SEARCH_TEXT) == [("le-chonk-team", "C0EXAMPLE1"), ("le-chonk", "C0LECHONK1")]
    assert parse_channels('{"channels": [{"name": "le-chonk", "id": "C0LECHONK1"}]}') == [("le-chonk", "C0LECHONK1")]


async def test_slack_reads_new_messages_oldest_first_and_replies_with_the_mark() -> None:
    fake = FakeSlackConnector()
    first = fake.say("C1", "one")
    fake.say("C1", "a reply", thread=first)
    second = fake.say("C1", "two")
    slack = ConnectorSlack(fake)
    assert [m["text"] for m in await slack.read_channel("C1", oldest=first)] == ["two"]
    assert [m["text"] for m in await slack.read_thread("C1", first)] == ["a reply"]  # not the parent
    await ConnectorNotifier(slack).post("C1", second, "Done.")
    await ConnectorNotifier(slack).post(None, None, "nowhere")  # an issue made on GitHub: no thread, no message
    assert fake.thread("C1", second)[0]["text"] == f"{MARK} Done."
