"""The judge, and the reward it adds to a trial: APK, tests and the share of acceptance criteria met."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import write_tree
from le_chonk_eval.demo.judge import judge, judge_diff, judge_trial, normalize, project_diff
from le_chonk_eval.demo.issues import Task
from le_chonk_eval.utils.compare import compare
from le_chonk_eval.utils.results import TASK_DONE, read_job
from le_chonk_eval.workflow import fake_harbor

TASK = Task(title="Double jump", goal="Jump once more in the air.", criteria=["A second tap jumps again", "A third tap does nothing"])


class FakeChat:
    def __init__(self, answer: Any) -> None:
        self.answer, self.calls = answer, []
        self.chat = self

    async def complete_async(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=900, completion_tokens=80))


def base_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "base"
    write_tree(clone, {"shared/src/Game.kt": "class Game {\n  fun jump() {}\n}\n", "README.md": "# Game\n"})
    for args in (["init", "--quiet"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "base"]):
        subprocess.run(["git", *args], cwd=clone, check=True)
    return clone


def test_the_verdict_follows_the_criteria() -> None:
    both = {"criteria": [{"met": True, "evidence": "Game.kt counts jumps"}, {"met": True}], "verdict": "done", "summary": "ok"}
    assert normalize(both, TASK) | {"criteria": None} == {"verdict": "done", "score": 1.0, "criteria": None, "summary": "ok"}
    generous = normalize({"criteria": [{"met": True}, {"met": "yes"}], "verdict": "done"}, TASK)
    assert generous["verdict"] == "partial" and generous["score"] == 0.5 and generous["criteria"][1]["met"] is False
    assert normalize({}, TASK)["verdict"] == "not_done"
    no_criteria = normalize({"criteria": [{"met": True}]}, Task(title="T", goal="Show the score"))
    assert no_criteria["criteria"][0]["criterion"] == "Show the score" and no_criteria["verdict"] == "done"


def test_the_diff_is_the_change_from_the_base_commit(tmp_path: Path) -> None:
    clone = base_clone(tmp_path)
    project = tmp_path / "project"
    write_tree(project, {"shared/src/Game.kt": "class Game {\n  var jumps = 0\n  fun jump() { jumps++ }\n}\n", "README.md": "# Game\n",
                         "build/x.txt": "ignored"})
    diff = project_diff(clone, project)
    assert diff["files"] == ["shared/src/Game.kt"] and "+  var jumps = 0" in diff["diff"] and not diff["truncated"]
    same = tmp_path / "same"
    write_tree(same, {"shared/src/Game.kt": "class Game {\n  fun jump() {}\n}\n", "README.md": "# Game\n"})
    assert project_diff(clone, same)["files"] == []
    assert subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True).stdout == ""  # base untouched


def file_diff(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


async def test_the_judge_sees_the_code_before_wrappers_and_build_files() -> None:
    # A new project's diff, in git's order: wrappers, the Xcode project and icons first, the code at the end
    diff = (file_diff("androidApp/res/mipmap-hdpi/ic_launcher.png", []).replace("@@ -0,0 +1,0 @@\n", "Binary files /dev/null and b/x differ\n")
            + file_diff("iosApp/module.xcodeproj/project.pbxproj", ["/* xcode */"] * 200)
            + file_diff("kotlin", ["#!/bin/sh"] * 400)
            + file_diff("libs.versions.toml", ["[versions]", "compose = '1.9'"])
            + file_diff("shared/composeResources/drawable/cat.xml", ["<vector/>"] * 300)
            + file_diff("shared/src/Game.kt", ["class Game {", "  var jumps = 0", "}"])
            + file_diff("shared/test/GameTest.kt", ["class GameTest"]))
    seen = judge_diff(diff, 1000)
    assert seen["files"][0].endswith("ic_launcher.png") and len(seen["files"]) == 7
    assert seen["left_out"] == ["androidApp/res/mipmap-hdpi/ic_launcher.png", "iosApp/module.xcodeproj/project.pbxproj", "kotlin"]
    assert seen["not_shown"] == ["shared/composeResources/drawable/cat.xml"] and seen["truncated"]
    order = [seen["diff"].index(f"b/{name}") for name in ("shared/src/Game.kt", "shared/test/GameTest.kt", "libs.versions.toml")]
    assert order == sorted(order) and "#!/bin/sh" not in seen["diff"] and len(seen["diff"]) <= 1000
    assert " kotlin | +400 -0" in seen["stat"] and "ic_launcher.png | binary" in seen["stat"]
    assert seen["stat"].endswith(" 7 files changed, 906 insertions(+), 0 deletions(-)")

    client = FakeChat({"criteria": [{"met": True}, {"met": True}], "verdict": "done"})
    await judge(client, "m", TASK, {}, seen)
    prompt = client.calls[0]["messages"][1]["content"]
    assert "Named only, as generated, IDE or binary files: androidApp/res/mipmap-hdpi/ic_launcher.png, " in prompt
    assert "Not shown, to fit: shared/composeResources/drawable/cat.xml." in prompt and "cut to fit" not in prompt

    everything = judge_diff(diff)
    assert not everything["not_shown"] and not everything["truncated"] and "<vector/>" in everything["diff"]
    odd = judge_diff("something that isn't a git diff")
    assert odd["files"] == ["(the diff)"] and odd["diff"] == "something that isn't a git diff"
    assert judge_diff("") == {"files": [], "stat": "", "diff": "", "left_out": [], "not_shown": [], "truncated": False}


async def test_the_judge_reads_the_task_and_the_change() -> None:
    client = FakeChat({"criteria": [{"met": True, "evidence": "jumps counts taps"}, {"met": False}], "verdict": "partial", "summary": "Half."})
    diff = {"files": ["shared/src/Game.kt"], "stat": " Game.kt | 2 +-", "diff": "+var jumps", "truncated": True}
    result = await judge(client, "mistral/mistral-large-4", TASK, {"assertions": {"android_apk": True, "shared_tests_pass": False}}, diff)
    assert result["verdict"] == "partial" and result["score"] == 0.5 and result["prompt_tokens"] == 900
    request = client.calls[0]
    assert request["model"] == "mistral-large-4" and request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0 and request["top_p"] == 1  # greedy: the API refuses temperature 0 without top_p 1
    prompt = request["messages"][1]["content"]
    assert "- A third tap does nothing" in prompt and "Android APK built: yes. Shared tests pass: no." in prompt and "cut to fit" in prompt

    nothing = FakeChat({})
    result = await judge(nothing, "mistral/mistral-large-4", TASK, {}, {"files": [], "stat": "", "diff": "", "truncated": False})
    assert result["verdict"] == "not_done" and result["score"] == 0 and not nothing.calls
    broken = await judge(FakeChat("not json"), "m", TASK, {}, diff)
    assert broken["verdict"] == "not_done" and "wasn't JSON" in broken["summary"]


async def test_judged_trials_get_the_task_reward(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    for name, skill in (("ab-no-skill", False), ("ab-skill", True)):
        fake_harbor.main(["run", "-p", str(tmp_path), "-m", "mistral/mistral-large-4", "-k", "2", "--job-name", name, "-o", str(jobs)]
                         + (["--skill", "x"] if skill else []))
    clone = base_clone(tmp_path)
    client = FakeChat({"criteria": [{"met": True}, {"met": True}], "verdict": "done"})
    for trial in sorted(p for p in (jobs / "ab-skill").iterdir() if p.is_dir()):
        assert (await judge_trial(client, "mistral/mistral-large-4", TASK, trial, clone))["verdict"] == "done"
    assert await judge_trial(client, "m", TASK, tmp_path / "nothing", clone) is None

    run = read_job(jobs / "ab-skill")[0]
    apk, tests = run["assertions"]["android_apk"], run["assertions"]["shared_tests_pass"]
    assert run["reward"] == pytest.approx(round((bool(apk) + bool(tests) + 1.0) / 3, 4)) and run["assertions"][TASK_DONE] is True
    assert run["toolchain_reward"] is not None and run["judge"]["score"] == 1.0
    assert read_job(jobs / "ab-no-skill")[0]["judge"] is None

    summary = compare([jobs / "ab-no-skill", jobs / "ab-skill"], ab_id="issue-7-ab", title="Issue #7: Double jump", agent_label="Vibe",
                      reward_note="Reward = APK, tests and the judge.")
    assert summary["assertions"][0] == TASK_DONE and summary["arms"]["skill"]["judge_score_mean"] == 1.0
    html = Path(summary["dashboard"]).read_text()
    assert "<h1>Issue #7: Double jump</h1>" in html and "Vibe on mistral/mistral-large-4" in html and "Judge: task done" in html
