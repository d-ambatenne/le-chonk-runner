"""scripts/redeploy_next.py: stopping the watchers, pushing, waiting for the deployment, queueing, against fakes."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_github import FakeGitHub

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("redeploy_next", ROOT / "scripts" / "redeploy_next.py")
redeploy_next = importlib.util.module_from_spec(spec)
spec.loader.exec_module(redeploy_next)

OLD, NEW = "1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "2222222bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class FakeClient:
    def __init__(self, running: dict[str, list[str]] | None = None, states: list[dict[str, Any]] | None = None,
                 takes_status: bool = True) -> None:
        self.running = running or {}
        self.cancelled: list[str] = []
        self.states = states or []
        self.restarts = 0
        self.takes_status = takes_status
        executions = SimpleNamespace(cancel_workflow_execution=self._cancel, get_workflow_execution=self._get)
        deployments = SimpleNamespace(get_deployment=self._deployment, restart_deployment=self._restart)
        self.workflows = SimpleNamespace(runs=SimpleNamespace(list_runs=self._list), executions=executions, deployments=deployments)

    def _list(self, workflow_identifier: str, page_size: int, status: str | None = None) -> Any:
        if status is not None and not self.takes_status:
            raise TypeError("status")
        found = [SimpleNamespace(execution_id=e, status="WorkflowExecutionStatus.RUNNING") for e in self.running.get(workflow_identifier, [])]
        return SimpleNamespace(result=SimpleNamespace(executions=found))

    def _cancel(self, execution_id: str) -> None:
        self.cancelled.append(execution_id)

    def _get(self, execution_id: str) -> Any:
        return SimpleNamespace(status="CANCELED" if execution_id in self.cancelled else "RUNNING")

    def _deployment(self, name: str) -> dict[str, Any]:
        return self.states[0] if self.restarts == 0 or len(self.states) == 1 else self.states.pop(1) if len(self.states) > 2 else self.states[-1]

    def _restart(self, name: str) -> None:
        self.restarts += 1


def state(deployed: str | None, built: str | None = None, phase: str = "Running", build: str = "succeeded", active: bool = True,
          workers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    status = {"phase": phase, "deployed_revision": deployed, "build_state": {"phase": build, "commit_sha": built, "message": "boom"}}
    return {"is_active": active, "active_worker_count": int(active), "workers": workers or [],
            "managed": {"status": status, "rollout_status": None, "spec": {"revision": "main"}}}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone with a remote, on main, so pushes work."""
    remote, clone = tmp_path / "remote.git", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True, capture_output=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
        subprocess.run(["git", "config", key, value], cwd=clone, check=True)
    (clone / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=clone, check=True, capture_output=True)
    monkeypatch.setattr(redeploy_next, "ROOT", clone)
    return clone


def test_every_running_watcher_is_stopped_also_ones_started_from_studio() -> None:
    client = FakeClient({"le-chonk-slack": ["12e6552916325643"], "le-chonk-tasks": ["le-chonk-tasks-you-app-a"]})
    redeploy_next.stop_watchers(client, pause=0)
    assert client.cancelled == ["12e6552916325643", "le-chonk-tasks-you-app-a"]
    older = FakeClient({"le-chonk-slack": ["s1"]}, takes_status=False)  # an SDK without the status filter
    redeploy_next.stop_watchers(older, pause=0)
    assert older.cancelled == ["s1"]


def test_a_watcher_that_wont_stop_stops_the_script() -> None:
    client = FakeClient({"le-chonk-slack": ["stuck"]})
    client._cancel = lambda execution_id: None  # noqa: SLF001
    client.workflows.executions.cancel_workflow_execution = client._cancel  # noqa: SLF001
    with pytest.raises(SystemExit, match="still running after 0 minutes: stuck"):
        redeploy_next.stop_watchers(client, wait_seconds=0, pause=0)


def test_changes_are_committed_and_pushed_once(repo: Path) -> None:
    (repo / "a.txt").write_text("two\n")
    (repo / "b.txt").write_text("new\n")
    sha = redeploy_next.push("Explain failed runs")
    log = subprocess.run(["git", "log", "--format=%H %s", "origin/main", "-1"], cwd=repo, capture_output=True, text=True).stdout
    assert log.split() == [sha, "Explain", "failed", "runs"]
    assert redeploy_next.push("again") == sha  # nothing new: nothing committed, nothing pushed


def test_the_deployment_restarts_and_the_script_waits_for_the_new_commit(repo: Path) -> None:
    client = FakeClient(states=[state(OLD), state(OLD, NEW, build="building"), state(OLD, NEW, phase="Deploying"), state(NEW, NEW)])
    redeploy_next.redeploy(client, "le-chonk-control", NEW, pause=0)
    assert client.restarts == 1 and len(client.states) == 2  # it read the states until the new commit was deployed


def test_a_deployment_already_on_the_commit_isnt_restarted(repo: Path) -> None:
    client = FakeClient(states=[state(NEW, NEW)])
    redeploy_next.redeploy(client, "le-chonk-control", NEW, pause=0)
    assert client.restarts == 0


def test_a_failed_build_stops_the_script_with_its_message(repo: Path) -> None:
    client = FakeClient(states=[state(OLD), state(OLD, NEW, build="failed")])
    with pytest.raises(SystemExit, match="build failed: boom"):
        redeploy_next.redeploy(client, "le-chonk-control", NEW, pause=0)


def test_without_a_revision_a_worker_registered_after_the_restart_means_live(repo: Path) -> None:
    old_worker = {"is_active": True, "created_at": "2020-01-01T00:00:00Z"}
    new_worker = {"is_active": True, "created_at": "2999-01-01T00:00:00Z"}
    client = FakeClient(states=[state(None, workers=[old_worker]), state(None, workers=[old_worker]), state(None, workers=[new_worker])])
    redeploy_next.redeploy(client, "le-chonk-control", NEW, pause=0)
    assert client.restarts == 1 and len(client.states) == 2


def test_a_revision_named_by_its_branch_falls_back_to_a_new_worker(repo: Path) -> None:
    new_worker = {"is_active": True, "created_at": "2999-01-01T00:00:00Z"}
    client = FakeClient(states=[state("main"), state("main"), state("main", workers=[new_worker])])
    redeploy_next.redeploy(client, "le-chonk-control", NEW, pause=0)
    assert client.restarts == 1


async def test_queued_issues_are_ready_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import le_chonk_eval.demo.github as github_module

    fake = FakeGitHub(tmp_path / "remotes")
    fake.add_repo("you/app")
    fake.issues["you/app"].append({"number": 1, "title": "Runner", "body": "", "state": "open", "html_url": "https://github.com/you/app/issues/1",
                                   "labels": [{"name": "vibe:failed"}]})
    real = github_module.GitHub
    monkeypatch.setattr(github_module, "GitHub", lambda: real("t0ken", transport=fake.transport()))
    await redeploy_next.queue("https://github.com/you/app", [1])
    assert [label["name"] for label in fake.issues["you/app"][0]["labels"]] == ["vibe:ready"]
