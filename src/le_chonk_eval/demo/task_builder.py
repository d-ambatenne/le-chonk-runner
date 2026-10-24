"""An issue as a Harbor task: the A/B task's machine and verifier, with the repo in /app and the issue as the prompt.

runs/<run id>/task/
    task.toml          the A/B task's resources and timeouts, renamed for the issue
    instruction.md     the issue, and how to work in the container
    environment/       the A/B task's Dockerfile and warm-up, plus the repo at its base commit, copied to /app
    tests/             the A/B task's verifier, which here also keeps the APK it builds
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..utils.paths import task_dir
from .github import EXCLUDED, starter_files
from .issues import Task

INSTRUCTION = """# {title}

This is issue #{number} of {url}. {where}

{brief}

## How to work

- The project is a Kotlin Multiplatform app built with the Kotlin Toolchain (JetBrains' `kotlin` command-line tool), not with Gradle or Maven. The `kotlin` command is installed.
- This machine runs Linux, so it can't build or run the iOS app: keep the iOS app's setup correct, and check your work by building the Android app and running the tests.
- Leave your changes in /app and don't commit them: the pipeline commits them after its own checks.

## Testability

After your checks, a tester checks the Android app against the acceptance criteria on an emulator. It reads the screen's accessibility tree, so make every state the criteria name readable there, with Compose semantics:
- What a player sees but you draw on a Canvas, such as hearts, lives or a status, gets a content description in every build, such as "4 of 5 hearts". Screen readers use it too.
- Internal state, such as whether a character is in the air or where obstacles are, is described only in debug builds: check at run time that the app is debuggable (on Android, `ApplicationInfo.FLAG_DEBUGGABLE`), through an expect/actual function, and add those descriptions only then.
- A tree read takes a second or two, so a state that lasts less, such as a jump or a hit, can be over before it is read. Count such events in the debug descriptions too, such as "jumps: 3" and "hits: 1": the count still shows them afterwards.
- Keep buttons, titles and scores as real text, not drawn on a Canvas.

When you are done, reply with a short summary of what you changed.
"""

REPO_IN_IMAGE = """
# The repository at the issue's base commit, with its history, so the agent starts from the current code.
COPY repo/ /app/
"""


def instruction(repo: str, number: int, task: Task, empty: bool, kept: list[str] | None = None) -> str:
    """Vibe's instruction. kept: the files a repository with no code yet already has, such as a README and a license."""
    if empty and kept:
        names = " and ".join([", ".join(kept[:-1]), kept[-1]] if len(kept) > 1 else kept)
        where = (f"The repository has no code yet, only {names}: create the project in /app, the current directory, and keep "
                 f"{'those files' if len(kept) > 1 else 'that file'}. If a tool won't create a project in a folder that isn't "
                 "empty, create it in an empty folder and move it into /app.")
    elif empty:
        where = "The repository is empty: create the project in /app, the current directory."
    else:
        where = "The repository is checked out in /app, the current directory."
    return INSTRUCTION.format(title=task.title, number=number, url=f"https://github.com/{repo}", where=where, brief=task.brief())


def build_task(repo: str, number: int, task: Task, clone_dir: Path, out: Path, empty: bool) -> Path:
    template = task_dir()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(template / "environment", out / "environment")
    shutil.copytree(template / "tests", out / "tests")
    shutil.copytree(clone_dir, out / "environment" / "repo", ignore=shutil.ignore_patterns(*[n for n in EXCLUDED if n != ".git"], "*.apk"))

    with (out / "environment" / "Dockerfile").open("a", encoding="utf-8") as handle:
        handle.write(REPO_IN_IMAGE)
    test_sh = out / "tests" / "test.sh"
    test_sh.write_text(test_sh.read_text().replace("--out /logs/verifier", "--out /logs/verifier --keep-apk"))

    owner, name = repo.split("/", 1)
    config = (template / "task.toml").read_text()
    config = re.sub(r'(?m)^name = ".*"$', f'name = "{owner}/{name}-issue-{number}"', config, count=1)
    description = task.title.replace("\\", "").replace('"', "'")
    config = re.sub(r'(?m)^description = ".*"$', f'description = "Issue #{number} of {repo}: {description}"', config, count=1)
    (out / "task.toml").write_text(config)
    kept = starter_files(clone_dir) if empty else None
    (out / "instruction.md").write_text(instruction(repo, number, task, empty, kept), encoding="utf-8")
    return out
