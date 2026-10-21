"""The demo's GitHub side: the issue format, the REST client against an in-memory GitHub, and git on this machine."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from conftest import FIXTURE_SKILL, write_tree
from fake_github import FakeGitHub
from le_chonk_eval.demo import github as gh
from le_chonk_eval.demo.github import GitHub, check_skill, parse_repo, parse_skill_url
from le_chonk_eval.demo.issues import AB_REQUESTED, LABELS, READY, RUNNING, Task, work_kind

SKILL_URL = "https://github.com/Kotlin/kotlin-agent-skills/tree/main/skills/kotlin-tooling-kotlin-toolchain"


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    remotes = tmp_path / "remotes"
    monkeypatch.setenv("LECHONK_GIT_BASE", remotes.as_uri())
    fake = FakeGitHub(remotes)
    fake.add_repo("denis/le-chonk-game")
    fake.add_repo("Kotlin/kotlin-agent-skills")
    fake.add_file("Kotlin/kotlin-agent-skills", "skills/kotlin-tooling-kotlin-toolchain/SKILL.md", FIXTURE_SKILL)
    return fake


def test_repos_and_skill_links_are_read_from_urls() -> None:
    for value in ("https://github.com/denis/le-chonk-game", "github.com/denis/le-chonk-game.git", "<https://github.com/denis/le-chonk-game/issues>",
                  "denis/le-chonk-game"):
        assert parse_repo(value) == "denis/le-chonk-game"
    with pytest.raises(ValueError):
        parse_repo("https://gitlab.com/denis/le-chonk-game")
    assert parse_skill_url(SKILL_URL) == {"repo": "Kotlin/kotlin-agent-skills", "ref": "main", "path": "skills/kotlin-tooling-kotlin-toolchain",
                                          "url": SKILL_URL}
    with pytest.raises(ValueError, match="link to its folder"):
        parse_skill_url("https://github.com/Kotlin/kotlin-agent-skills")


def test_a_task_round_trips_through_the_issue_body() -> None:
    task = Task(title="Double jump", goal="Le Chonk can jump again in the air, once.", criteria=["A second tap in the air jumps again", "A third tap does nothing"],
                notes="Keep the jump height.", skills=[SKILL_URL], ab_attempts=3, slack_channel="C1", slack_thread="1712.5", requested_by="U1")
    body = task.body()
    assert "### Acceptance criteria\n- A second tap in the air jumps again" in body and "<!-- le-chonk {" in body
    assert Task.from_issue("Double jump", body) == task
    assert "Skills" not in task.brief() and "1712.5" not in task.brief() and "A third tap does nothing" in task.brief()


def test_an_issue_written_by_hand_is_read_too() -> None:
    body = "Show the best score.\r\n\r\n## Acceptance Criteria\r\n- [ ] The best score stays after a restart\r\n* [x] It shows under the score\r\n"
    task = Task.from_issue("Best score", body)
    assert task.goal == "Show the best score." and task.criteria == ["The best score stays after a restart", "It shows under the score"]
    assert task.skills == [] and task.slack_channel is None


def test_sections_of_an_issue_form_are_kept() -> None:
    body = "### Description\n\nShow the best score.\n\n### Acceptance criteria\n\nThe best score survives a restart.\n\n### Platform\n\nAndroid"
    task = Task.from_issue("Best score", body)
    assert task.goal == "" and task.criteria == []
    assert task.other == [("Description", "Show the best score."), ("Acceptance criteria", "The best score survives a restart."), ("Platform", "Android")]
    task.skills.append(SKILL_URL)  # what add_skill does, then the body is written back
    again = Task.from_issue("Best score", task.body())
    assert again.other == task.other and again.skills == [SKILL_URL]
    assert "Description:\nShow the best score." in task.brief() and "Platform:\nAndroid" in task.brief()


def test_labels_decide_what_the_watcher_does() -> None:
    def issue(*labels: str, **extra: object) -> dict:
        return {"state": "open", "labels": [{"name": n} for n in labels], **extra}

    assert work_kind(issue(READY)) == "run" and work_kind(issue(READY, AB_REQUESTED)) == "ab"
    assert work_kind(issue(READY, RUNNING)) is None and work_kind(issue()) is None
    assert work_kind(issue(READY, state="closed")) is None and work_kind(issue(READY, pull_request={})) is None


async def test_the_client_works_with_issues_labels_and_skills(fake: FakeGitHub) -> None:
    async with GitHub("t0ken", transport=fake.transport()) as github:
        repo = "denis/le-chonk-game"
        await github.ensure_labels(repo)
        assert set(fake.labels[repo]) == set(LABELS)
        await github.ensure_labels(repo)  # already there: nothing new
        issue = await github.create_issue(repo, "Double jump", "Jump twice.", [READY])
        await github.relabel(repo, issue["number"], add=[RUNNING], remove=[READY, "not-there"])
        await github.comment(repo, issue["number"], "Started.")
        assert [label["name"] for label in (await github.issue(repo, 1))["labels"]] == [RUNNING]
        assert [i["number"] for i in await github.search_issues(repo, "twice")] == [1]
        assert fake.comments[(repo, 1)] == ["Started."]
        assert await github.file_text(repo, "missing.md") is None
        skill = await check_skill(github, SKILL_URL)
        assert skill == {"url": SKILL_URL, "name": "kotlin-tooling-kotlin-toolchain",
                         "description": "Load when building a Kotlin project with the Kotlin Toolchain."}
        with pytest.raises(ValueError, match="No SKILL.md"):
            await check_skill(github, SKILL_URL.replace("kotlin-tooling-kotlin-toolchain", "nothing"))
        with pytest.raises(gh.GitHubError, match="404"):
            await github.repo("denis/missing")


def test_the_token_reaches_git_only_as_a_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LECHONK_GIT_BASE", raising=False)
    args = gh._auth("t0ken")
    assert args[0] == "-c" and args[1].startswith("http.extraHeader=Authorization: Basic ")
    assert base64.b64decode(args[1].rsplit(" ", 1)[1]).decode() == "x-access-token:t0ken"
    assert gh.clone_url("denis/le-chonk-game") == "https://github.com/denis/le-chonk-game.git"
    with pytest.raises(RuntimeError) as failure:
        gh.git(["ls-remote", "https://t0ken@127.0.0.1:9/x.git"], token="t0ken")
    assert "t0ken" not in str(failure.value)


async def test_clone_commit_push_and_merge(tmp_path: Path, fake: FakeGitHub) -> None:
    repo = "denis/le-chonk-game"
    clone = tmp_path / "work"
    assert gh.clone(repo, clone, None, "main") is None  # empty
    first = gh.initial_commit(clone, None, "main", "Le Chonk Runner")
    assert gh.clone(repo, clone, None, "main") == first and (clone / ".gitignore").is_file()
    assert gh.is_fresh(clone)  # only the demo's first commit: Vibe is told the repo is empty
    assert "token" not in subprocess.run(["git", "config", "--list"], cwd=clone, capture_output=True, text=True).stdout.lower()

    project = tmp_path / "project"
    write_tree(project, {"project.yaml": "modules: [shared]\n", "shared/src/Game.kt": "class Game\n"})
    (clone / "old.txt").write_text("stale\n")
    (clone / "build").mkdir()
    (clone / "build" / "out.bin").write_text("keep me\n")
    gh.sync_tree(project, clone)
    assert not (clone / "old.txt").exists() and (clone / "build" / "out.bin").is_file() and (clone / "README.md").exists() is False
    assert gh.commit_all(clone, "Issue #1: the game") is not None and gh.commit_all(clone, "again") is None
    assert not gh.is_fresh(clone)
    tracked = subprocess.run(["git", "ls-files"], cwd=clone, capture_output=True, text=True).stdout.split()
    assert tracked == ["project.yaml", "shared/src/Game.kt"]  # not the build output, though no .gitignore covers it
    gh.push(clone, "vibe/issue-1", None)

    async with GitHub("t0ken", transport=fake.transport()) as github:
        pull = await github.create_pull(repo, "#1 the game", "vibe/issue-1", "main", "Closes #1")
        await github.merge_pull(repo, pull["number"], "#1 the game")
    assert fake.branch_file(repo, "main", "shared/src/Game.kt") == "class Game"


def test_sync_keeps_dot_git_and_build_output(tmp_path: Path) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    write_tree(source, {"a/b.txt": "new\n", "a/build/x.txt": "from source\n", "app.apk": "binary"})
    write_tree(dest, {".git/HEAD": "ref\n", "a/b.txt": "old\n", "gone/c.txt": "x\n", ".kotlin/cache": "c\n"})
    gh.sync_tree(source, dest)
    assert (dest / "a" / "b.txt").read_text() == "new\n" and not (dest / "gone").exists()
    assert (dest / ".git" / "HEAD").is_file() and (dest / ".kotlin" / "cache").is_file()
    assert not (dest / "a" / "build").exists() and not (dest / "app.apk").exists()


async def test_a_new_app_repo_is_private_and_empty(fake: FakeGitHub) -> None:
    async with GitHub("t0ken", transport=fake.transport()) as github:
        made = await github.create_repo("le-chonk-app", "Le Chonk Runner")
        assert made["full_name"] == "denis/le-chonk-app" and fake.repos["denis/le-chonk-app"]["private"]
        with pytest.raises(gh.GitHubError, match="422"):
            await github.create_repo("le-chonk-app")
