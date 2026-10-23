"""An issue turned into a Harbor task for Vibe."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harbor.models.task.config import TaskConfig

from conftest import TASK, write_tree
from le_chonk_eval.demo.issues import Task
from le_chonk_eval.demo.task_builder import build_task
from le_chonk_eval.workflow.harbor_jobs import VIBE, run_command

ISSUE = Task(title='Add a "double" jump', goal="Le Chonk can jump once more in the air.", criteria=["A second tap jumps again"],
             skills=["https://github.com/Kotlin/kotlin-agent-skills/tree/main/skills/x"], slack_channel="C1", slack_thread="1.2")


def test_an_issue_becomes_a_harbor_task(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    write_tree(clone, {"project.yaml": "modules: [shared]\n", "shared/src/Game.kt": "class Game\n", "build/out.bin": "x", "app.apk": "x"})
    subprocess.run(["git", "init", "--quiet"], cwd=clone, check=True)
    task = build_task("denis/le-chonk-game", 7, ISSUE, clone, tmp_path / "task", empty=False)

    config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
    original = TaskConfig.model_validate_toml((TASK / "task.toml").read_text())
    assert config.task.name == "denis/le-chonk-game-issue-7" and "Issue #7 of denis/le-chonk-game" in config.task.description
    assert config.agent.timeout_sec == original.agent.timeout_sec and config.environment.cpus == original.environment.cpus
    assert config.artifacts == original.artifacts

    instruction = (task / "instruction.md").read_text()
    assert instruction.startswith('# Add a "double" jump') and "issue #7 of https://github.com/denis/le-chonk-game" in instruction
    assert "checked out in /app" in instruction and "- A second tap jumps again" in instruction
    assert "kotlin-agent-skills" not in instruction and "1.2" not in instruction  # no skills or Slack thread for the agent
    assert "## Testability" in instruction and '"4 of 5 hearts"' in instruction and "FLAG_DEBUGGABLE" in instruction

    repo = task / "environment" / "repo"
    assert (repo / "shared" / "src" / "Game.kt").is_file() and (repo / ".git").is_dir()
    assert not (repo / "build").exists() and not (repo / "app.apk").exists()
    assert (task / "environment" / "Dockerfile").read_text().rstrip().endswith("COPY repo/ /app/")
    assert (task / "environment" / "warmup.sh").is_file() and not (task / "solution").exists()
    assert "--keep-apk" in (task / "tests" / "test.sh").read_text() and (task / "tests" / "score.py").is_file()

    again = build_task("denis/le-chonk-game", 7, ISSUE, clone, tmp_path / "task", empty=True)  # rebuilt from scratch
    assert "The repository is empty" in (again / "instruction.md").read_text()


def test_the_vibe_command_for_an_issue(tmp_path: Path) -> None:
    skill = "https://github.com/Kotlin/kotlin-agent-skills/tree/main/skills/x"
    command = run_command("issue-7-run", "skill", attempts=1, max_cost_usd=3, harbor=["harbor"], task=tmp_path, agent=VIBE,
                          skills=[skill], agent_env=["VIBE_BACKEND=mistral"])
    text = " ".join(command)
    assert f"-p {tmp_path} -a vibe -m mistral/mistral-large-4 -k 1" in text
    assert f"--skill {skill}" in text and "--ak max_price=3" in text and "--ae VIBE_BACKEND=mistral" in text
    assert "--skill" not in run_command("issue-7-ab-no-skill", "no-skill", harbor=["harbor"], task=tmp_path, agent=VIBE, skills=[skill])


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    write_tree(repo, files)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", message], cwd=repo, check=True)


def test_a_repo_github_started_with_a_readme_and_a_license_is_a_new_project(tmp_path: Path) -> None:
    from le_chonk_eval.demo import github as gh

    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=clone, check=True)
    _commit(clone, {"README.md": "# le-chonk-game\n", "LICENSE": "MIT License\n"}, "Initial commit")  # what GitHub makes
    assert gh.is_fresh(clone) and gh.starter_files(clone) == ["LICENSE", "README.md"]
    task = build_task("denis/le-chonk-game", 1, ISSUE, clone, tmp_path / "task", empty=True)
    instruction = (task / "instruction.md").read_text()
    assert "no code yet, only LICENSE and README.md: create the project in /app" in instruction and "keep those files" in instruction
    assert "create it in an empty folder and move it into /app" in instruction

    _commit(clone, {"docs/README.md": "more\n"}, "Docs")  # a file in a folder: not what a new repo starts with
    assert not gh.is_fresh(clone) and gh.starter_files(clone) is None


def test_the_instruction_says_what_to_keep_only_for_a_new_project() -> None:
    from le_chonk_eval.demo.task_builder import instruction

    assert "keep that file" in instruction("d/x", 1, ISSUE, True, ["README.md"])
    assert "The repository is empty: create the project in /app" in instruction("d/x", 1, ISSUE, True, [])
    assert "checked out in /app" in instruction("d/x", 1, ISSUE, False, ["README.md"])
