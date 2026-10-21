"""Ship a change to the next phase, in one go: a running workflow keeps the code it started with, so all of it restarts.

    uv run python scripts/redeploy_next.py --smoke --queue 1    # a smoke test: no Vibe, made-up results
    uv run python scripts/redeploy_next.py -m "What changed"    # real runs: Vibe in Docker, up to $3 each

1. Stop the watchers: every running le-chonk-slack and le-chonk-tasks execution, also ones started from Studio.
2. Commit what changed (git add -A: .gitignore keeps .env and runs/ out) and push.
3. Restart le-chonk-control, unless it already runs this commit, and wait until a new worker is live.
4. Stop the worker on this Mac, if one runs.
5. Start the watchers of the channel and the repo.
6. With --queue N: queue issue N again.
7. Run the worker here, in this terminal. Ctrl+C stops it.

The channel and the repo come from --channel and --repo, or from LECHONK_CHANNEL and LECHONK_REPO in .env. Like
the rest of the project, it reads MISTRAL_API_KEY and GITHUB_TOKEN from .env, and prints no secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
WATCHERS = ["le-chonk-slack", "le-chonk-tasks"]
WORKER = "le-chonk next worker"  # how the worker's processes show in ps


def say(text: str = "") -> None:
    print(text, flush=True)


def stop(reason: str) -> None:
    raise SystemExit(f"\nStopped: {reason}")


def run(command: list[str], capture: bool = False, check: bool = True) -> str:
    if not capture:
        say("$ " + " ".join(command))
    done = subprocess.run(command, cwd=ROOT, text=True, capture_output=capture)
    if check and done.returncode != 0:
        stop(f"`{' '.join(command)}` failed with exit code {done.returncode}. {(done.stderr or '').strip()[:500]}")
    return (done.stdout or "") if capture else ""


def dump(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value


def status_name(value: Any) -> str:
    return str(getattr(value, "value", value)).rsplit(".", 1)[-1].upper()


def cli() -> list[str]:
    """This project's le-chonk command, in its virtual environment."""
    local = Path(sys.executable).with_name("le-chonk")
    return [str(local)] if local.exists() else ["uv", "run", "le-chonk"]


# --- 1. The watchers -------------------------------------------------------------------------------------------

def running_watchers(client: Any) -> list[str]:
    found: list[str] = []
    for workflow in WATCHERS:
        try:
            page = client.workflows.runs.list_runs(workflow_identifier=workflow, status="RUNNING", page_size=50)
            executions = page.result.executions if page is not None else []
        except Exception:  # noqa: BLE001 - an SDK that doesn't take the filter: list them all
            page = client.workflows.runs.list_runs(workflow_identifier=workflow, page_size=50)
            executions = [e for e in (page.result.executions if page is not None else []) if status_name(e.status) == "RUNNING"]
        found += [e.execution_id for e in executions]
    return found


def stop_watchers(client: Any, wait_seconds: int = 180, pause: float = 5) -> None:
    say("\n1. Stopping the watchers")
    running = running_watchers(client)
    if not running:
        say("   none is running")
        return
    for execution in running:
        client.workflows.executions.cancel_workflow_execution(execution_id=execution)
        say(f"   stopping {execution}")
    deadline = time.monotonic() + wait_seconds
    left = running
    while left and time.monotonic() < deadline:
        time.sleep(pause)
        left = [e for e in left if status_name(client.workflows.executions.get_workflow_execution(execution_id=e).status) == "RUNNING"]
    if left:
        stop(f"still running after {wait_seconds // 60} minutes: {', '.join(left)}. Cancel them in Studio, then run this again.")
    say("   stopped")


# --- 2. Git ----------------------------------------------------------------------------------------------------

def push(message: str) -> str:
    say("\n2. Committing and pushing")
    changes = run(["git", "status", "--porcelain"], capture=True)
    if changes.strip():
        say(changes.rstrip())
        run(["git", "add", "-A"])
        run(["git", "commit", "-q", "-m", message])
    ahead = run(["git", "rev-list", "--count", "@{u}..HEAD"], capture=True, check=False).strip()
    if ahead == "0":
        say("   nothing to push")
    else:
        run(["git", "push", "-q"])
    return run(["git", "rev-parse", "HEAD"], capture=True).strip()


# --- 3. The deployment ------------------------------------------------------------------------------------------

def _state(detail: dict[str, Any]) -> dict[str, Any]:
    managed = detail.get("managed") or {}
    status = managed.get("status") or {}
    build = status.get("build_state") or {}
    return {"deployed": _sha(status.get("deployed_revision")), "built": _sha(build.get("commit_sha")), "build": build.get("phase"),
            "build_message": build.get("message"), "phase": status.get("phase"), "rollout": managed.get("rollout_status"),
            "message": status.get("message"), "branch": (managed.get("spec") or {}).get("revision")}


def _sha(value: Any) -> str | None:
    """A commit, not a branch's name: Mistral may name the revision either way."""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{7,40}", value) else None


def _same_commit(revision: str | None, sha: str) -> bool:
    return bool(revision) and len(str(revision)) >= 7 and (sha.startswith(str(revision)) or str(revision).startswith(sha[:7]))


def _failed(state: dict[str, Any]) -> str | None:
    for key, message in (("build", "build_message"), ("phase", "message"), ("rollout", "message")):
        value = str(state.get(key) or "")
        if any(word in value.lower() for word in ("fail", "error", "crash")):
            return f"{key} {value}: {state.get(message) or 'see the deployment in Studio'}"
    return None


def _live(detail: dict[str, Any], state: dict[str, Any], sha: str, since: str) -> bool:
    """The pushed commit runs: Mistral says it's deployed, with a live worker. If it doesn't say which revision runs,
    a worker that registered after the restart is the sign."""
    if not detail.get("is_active"):
        return False
    if state["deployed"]:
        return _same_commit(state["deployed"], sha)
    if state["built"] and not _same_commit(state["built"], sha):
        return False  # the old commit's build
    return any(w.get("is_active") and str(w.get("created_at") or "") >= since for w in detail.get("workers") or [])


def redeploy(client: Any, name: str, sha: str, timeout_minutes: int = 25, pause: float = 15) -> None:
    say(f"\n3. Restarting {name} on {sha[:7]}")
    detail = dump(client.workflows.deployments.get_deployment(name=name))
    if not detail.get("managed"):
        stop(f"{name} isn't a managed deployment, or Mistral doesn't say its state.")
    state = _state(detail)
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True).strip()
    if state["branch"] and state["branch"] != branch:
        say(f"   note: {name} builds the branch {state['branch']}, and you're on {branch}")
    if detail.get("is_active") and _same_commit(state["deployed"] or state["built"], sha):
        say("   it already runs this commit")
        return
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    client.workflows.deployments.restart_deployment(name=name)
    deadline, last = time.monotonic() + timeout_minutes * 60, ""
    while time.monotonic() < deadline:
        time.sleep(pause)
        detail = dump(client.workflows.deployments.get_deployment(name=name))
        state = _state(detail)
        revision = state["deployed"] or state["built"]
        line = (f"build {state['build'] or '?'}" + (f", revision {str(revision)[:7]}" if revision else "")
                + f", {state['phase'] or '?'}" + (f", rollout {state['rollout']}" if state["rollout"] else "")
                + f", live workers: {detail.get('active_worker_count', 0)}")
        if line != last:
            say(f"   {line}")
            last = line
        if problem := _failed(state):
            stop(f"{name} didn't start: {problem}")
        if _live(detail, state, sha, since):
            say("   live")
            return
    stop(f"{name} isn't live on {sha[:7]} after {timeout_minutes} minutes: see its page in Studio. When it is, run this "
         "again with --no-push to do the rest.")


# --- 4. The worker on this Mac ---------------------------------------------------------------------------------

def worker_pids() -> list[int]:
    found = subprocess.run(["pgrep", "-f", WORKER], text=True, capture_output=True).stdout.split()
    return [int(p) for p in found if p.isdigit() and int(p) != os.getpid()]


def stop_worker(wait_seconds: int = 60) -> None:
    say("\n4. Stopping the worker on this Mac")
    pids = worker_pids()
    if not pids:
        say("   none is running")
        return
    for sig, wait in ((signal.SIGINT, wait_seconds), (signal.SIGTERM, 10)):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait
        while worker_pids() and time.monotonic() < deadline:
            time.sleep(1)
        if not worker_pids():
            say("   stopped")
            return
    stop(f"the worker didn't stop: process {', '.join(map(str, worker_pids()))}. Stop it with Ctrl+C in its terminal.")


# --- 5 to 7 -----------------------------------------------------------------------------------------------------

async def queue(repo: str, numbers: list[int]) -> None:
    from le_chonk_eval.demo.github import GitHub, parse_repo
    from le_chonk_eval.demo.issues import AB_REQUESTED, DONE, FAILED, NOT_DONE, READY, RUNNING

    repo = parse_repo(repo)
    async with GitHub() as github:
        for number in numbers:
            issue = await github.issue(repo, number)
            if issue.get("state") != "open":
                await github.update_issue(repo, number, state="open")
            await github.relabel(repo, number, add=[READY], remove=[FAILED, NOT_DONE, DONE, AB_REQUESTED, RUNNING])
            say(f"   #{number} is queued: the watcher picks it up within a minute")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel", help="the Slack channel: its name or ID (default: LECHONK_CHANNEL in .env)")
    parser.add_argument("--repo", help="the app's repo, https://github.com/<you>/<repo> (default: LECHONK_REPO in .env)")
    parser.add_argument("--smoke", action="store_true", help="a smoke test: the fake harbor and judge, no Vibe")
    parser.add_argument("--queue", type=int, action="append", default=[], metavar="N", help="queue issue N again; repeatable")
    parser.add_argument("-m", "--message", default="Update the next phase", help="the commit message, if something changed")
    parser.add_argument("--control", default="le-chonk-control", help="the managed deployment (default le-chonk-control)")
    parser.add_argument("--no-push", action="store_true", help="skip git and the restart of the deployment")
    parser.add_argument("--no-worker", action="store_true", help="don't run the worker in this terminal at the end")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    channel = args.channel or os.environ.get("LECHONK_CHANNEL")
    repo = args.repo or os.environ.get("LECHONK_REPO")
    missing = [name for name, value in (("--channel or LECHONK_CHANNEL", channel), ("--repo or LECHONK_REPO", repo),
                                        ("MISTRAL_API_KEY", os.environ.get("MISTRAL_API_KEY")),
                                        ("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN"))) if not value]
    if missing:
        stop("missing " + ", ".join(missing) + " (see docs/next.md).")
    if not args.smoke and shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        stop("Docker isn't running, and real runs need it. Start Docker Desktop, or add --smoke.")

    from mistralai.client import Mistral

    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    stop_watchers(client)
    if not args.no_push:
        sha = push(args.message)
        redeploy(client, args.control, sha)
    stop_worker()
    say("\n5. Starting the watchers")
    run([*cli(), "next", "start", "--channel", channel, "--repo", repo, "--control", args.control] + (["--smoke"] if args.smoke else []))
    if args.queue:
        say("\n6. Queueing")
        asyncio.run(queue(repo, args.queue))
    if args.no_worker:
        say(f"\nDone. Start the worker with: {' '.join(cli())} next worker")
        return 0
    say("\n7. The worker, in this terminal: Ctrl+C stops it\n")
    command = [*cli(), "next", "worker"]
    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
