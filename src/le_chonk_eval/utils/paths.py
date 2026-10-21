"""Where things live. Every path can be moved with an environment variable."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

TASK_NAME = "le-chonk-runner"
SKILL_NAME = "kotlin-tooling-kotlin-toolchain"


def project_root() -> Path:
    return Path(os.environ.get("LECHONK_ROOT") or Path(__file__).resolve().parents[3])


def task_dir() -> Path:
    return Path(os.environ.get("LECHONK_TASK_DIR") or project_root() / "tasks" / TASK_NAME)


def skill_dir() -> Path:
    return Path(os.environ.get("LECHONK_SKILL_DIR") or project_root() / "skills" / SKILL_NAME)


def jobs_dir() -> Path:
    """Harbor's job folders: one per arm, e.g. jobs/ab-20261020-1200-skill/."""
    return Path(os.environ.get("LECHONK_JOBS_DIR") or project_root() / "jobs")


def results_dir() -> Path:
    """Comparisons: results/<ab id>/ with summary.json, runs.jsonl and dashboard.html."""
    return Path(os.environ.get("LECHONK_RESULTS_DIR") or project_root() / "results")


def chat_dir() -> Path:
    """Chat sessions: chat/<session>/ with the agent's project folder (app/) and the conversation (trace.jsonl)."""
    return Path(os.environ.get("LECHONK_CHAT_DIR") or project_root() / "chat")


def runs_dir() -> Path:
    """The demo's issue runs: runs/<run id>/ with the clone of the repo and the Harbor task made from the issue."""
    return Path(os.environ.get("LECHONK_RUNS_DIR") or project_root() / "runs")


def demo_dir() -> Path:
    """The demo's editable files: the task checklist and the Slack app manifest."""
    return Path(os.environ.get("LECHONK_DEMO_DIR") or project_root() / "demo")


def state_dir() -> Path:
    """Ids kept between runs: Studio agents, Slack threads and the channels' projects."""
    return Path(os.environ.get("LECHONK_STATE_DIR") or project_root() / ".state")


def env_file() -> Path:
    return project_root() / ".env"


def harbor_bin() -> list[str]:
    """The harbor CLI of this project's environment, so the pinned version and the Studio agent are used."""
    if os.environ.get("LECHONK_HARBOR_BIN"):
        return shlex.split(os.environ["LECHONK_HARBOR_BIN"])
    local = Path(sys.executable).parent / "harbor"
    return [str(local) if local.exists() else (shutil.which("harbor") or "harbor")]


def fake_harbor() -> list[str]:
    """A stand-in for harbor that writes made-up job folders: for the free, offline smoke run."""
    return [sys.executable, "-m", "le_chonk_eval.workflow.fake_harbor"]

