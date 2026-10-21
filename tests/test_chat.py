"""The chat: ChatSession with the local fakes, then DockerSandbox and `le-chonk chat` with a fake docker CLI."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import mistralai.client.models as models
import pytest
from harbor.models.agent.context import AgentContext

from conftest import FIXTURE_SKILL, TASK, make_executable, write_tree
from fakes import FakeClient, LocalEnvironment, call
from le_chonk_eval.agents import chat
from le_chonk_eval.agents.chat import INTERRUPTED, ChatSession, DockerSandbox, StopChat
from le_chonk_eval.agents.studio_agent import StudioAgent

MODEL = "mistral/mistral-large-4"


def text(content: str) -> models.MessageOutputEntry:
    return models.MessageOutputEntry(content=content)


def make_session(tmp_path: Path, script: list[list[Any]], skills_dir: str | None = None,
                 **kwargs: Any) -> tuple[ChatSession, FakeClient, LocalEnvironment, list[str]]:
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    client = FakeClient(script)
    agent = StudioAgent(logs_dir=tmp_path / "chat", model_name=MODEL, skills_dir=skills_dir)
    environment = LocalEnvironment(app)
    lines: list[str] = []
    session = ChatSession(agent, environment, client, workdir=str(app.resolve()), write=lines.append,
                          trace=tmp_path / "trace.jsonl", **kwargs)
    return session, client, environment, lines


# --- The conversation -------------------------------------------------------------------------------------

async def test_a_turn_runs_the_tools_until_the_agent_answers(tmp_path: Path) -> None:
    session, client, _, lines = make_session(tmp_path, [
        [text("Setting up."), call("t1", "write_file", path="shared/module.yaml", content="product: kmp/lib\n"),
         call("t2", "run_command", command="cat shared/module.yaml")],
        [text("Done: the shared module exists.")],
        [text("You're welcome.")],
    ])
    await session.setup()
    await session.send("Create the shared module")

    assert (tmp_path / "app" / "shared" / "module.yaml").read_text() == "product: kmp/lib\n"
    start, results = client.inputs[0], client.inputs[1]
    assert start == {"agent_id": "ag_1", "inputs": "Create the shared module", "store": True}
    sent = {entry.tool_call_id: entry.result for entry in results["inputs"]}
    assert sent["t1"].startswith("Created shared/module.yaml") and "product: kmp/lib" in sent["t2"]
    assert lines[0] == "\nagent › Setting up." and "  $ cat shared/module.yaml" in lines
    assert "\nagent › Done: the shared module exists." in lines
    assert lines[-1] == "(turn: 2 model calls, 2,200 tokens · session: 2 model calls, 2,200 tokens, $0.00)"

    await session.send("Thanks")
    assert client.inputs[2] == {"conversation_id": "conv_1", "inputs": "Thanks"}
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert kinds == ["user", "conversation", "agent", "tool", "tool", "agent", "user", "agent"]


async def test_the_chat_talks_to_the_agent_of_the_ab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same name, instructions and tools as in a trial, so the chat uses the trial's Studio agent as it is."""
    skills = tmp_path / "harbor-skills"
    write_tree(skills / "kotlin-tooling-kotlin-toolchain", {"SKILL.md": FIXTURE_SKILL})
    app = tmp_path / "app"
    app.mkdir()
    trial_client = FakeClient([[text("Done.")]])
    trial = StudioAgent(logs_dir=tmp_path / "trial", model_name=MODEL, skills_dir=str(skills))
    monkeypatch.setattr(trial, "_client", lambda: trial_client)
    await trial.run("Build", LocalEnvironment(app), AgentContext())
    assert trial_client.created[0]["name"] == "le-chonk-builder-skill"

    session, client, _, _ = make_session(tmp_path, [], skills_dir=str(skills))
    await session.setup()
    assert session.agent_name == "le-chonk-builder-skill" and session.skills == ["kotlin-tooling-kotlin-toolchain"]
    assert session.agent_id == "ag_1" and not client.created and not client.updated


async def test_an_interrupted_turn_is_closed_with_the_next_message(tmp_path: Path) -> None:
    session, client, environment, lines = make_session(tmp_path, [
        [call("t1", "run_command", command="echo first"), call("t2", "run_command", command="sleep 5")],
        [text("OK, no sleeping.")],
    ])
    await session.setup()
    turn = asyncio.create_task(session.send("Build"))
    for _ in range(200):
        if "t1" in session.finished and len(environment.commands) == 2:
            break
        await asyncio.sleep(0.05)
    turn.cancel()  # what Ctrl-C does
    await asyncio.gather(turn, return_exceptions=True)
    assert [c.tool_call_id for c in session.pending] == ["t1", "t2"] and list(session.finished) == ["t1"]

    await session.send("Skip the sleep")
    first, second, message = client.inputs[-1]["inputs"]
    assert first.tool_call_id == "t1" and first.result.startswith("[exit code 0") and "first" in first.result
    assert second.tool_call_id == "t2" and second.result == INTERRUPTED
    assert message.role == "user" and message.content == "Skip the sleep"
    assert lines[-2] == "\nagent › OK, no sleeping." and not session.pending


async def test_if_the_late_results_are_refused_the_message_goes_alone(tmp_path: Path) -> None:
    session, client, _, lines = make_session(tmp_path, [[text("First.")], [text("Hello again.")]])
    await session.setup()
    await session.send("Hi")
    session.pending = [call("t9", "run_command", command="true")]
    append = client.beta.conversations.append_async

    async def picky(**kwargs: Any) -> Any:
        if isinstance(kwargs["inputs"], list):
            client.inputs.append(kwargs)
            raise RuntimeError("400: no function call t9 waits for a result")
        return await append(**kwargs)

    client.beta.conversations.append_async = picky
    await session.send("Are you there?")
    assert isinstance(client.inputs[-2]["inputs"], list) and client.inputs[-1]["inputs"] == "Are you there?"
    assert lines[-2] == "\nagent › Hello again."


async def test_the_cost_limit_ends_the_chat(tmp_path: Path) -> None:
    # Each response costs 1000 * 0.68e-6 + 100 * 2.09e-6 = $0.000889, so the limit falls with the second.
    looping = [[call(f"t{i}", "run_command", command=f"echo {i}")] for i in range(5)]
    session, client, environment, _ = make_session(tmp_path, looping, max_cost_usd=0.0015)
    await session.setup()
    with pytest.raises(StopChat, match="--max-cost"):
        await session.send("Loop")
    assert len(environment.commands) == 1 and "echo 0" in environment.commands[0] and len(client.inputs) == 2
    with pytest.raises(StopChat):
        await session.send("One more")
    assert len(client.inputs) == 2


async def test_new_starts_another_conversation(tmp_path: Path) -> None:
    session, client, _, _ = make_session(tmp_path, [[text("One.")], [text("Two.")]])
    await session.setup()
    await session.send("First")
    session.reset()
    await session.send("Second")
    assert [("agent_id" in i, i["inputs"]) for i in client.inputs] == [(True, "First"), (True, "Second")]


# --- The container, with a fake docker CLI ----------------------------------------------------------------

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""A stand-in for the docker CLI: a container is a folder, and its commands run on this machine.

Paths under the container's mounts (/app, and /tmp and /harbor as folders of their own) point at those folders,
in arguments and in commands alike.
"""
import json, os, re, shutil, sys
from pathlib import Path

root = Path(os.environ["FAKE_DOCKER_ROOT"])
args = sys.argv[1:]
root.mkdir(parents=True, exist_ok=True)
with open(root / "calls.jsonl", "a") as log:
    log.write(json.dumps(args) + "\n")
state_file = root / "state.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {
    "images": [], "mounts": {"/tmp": str(root / "tmp"), "/harbor": str(root / "harbor")}}
for folder in state["mounts"].values():
    os.makedirs(folder, exist_ok=True)


def save():
    state_file.write_text(json.dumps(state))


def outside(text):
    mounts = state["mounts"]
    pattern = r"(?<![\w/.-])(" + "|".join(re.escape(m) for m in sorted(mounts, key=len, reverse=True)) + r")\b"

    def swap(match):  # a path that's already outside, as `find` prints it, stays as it is
        already = any(text.startswith(there, match.start()) for there in mounts.values())
        return match.group(0) if already else mounts[match.group(1)]

    return re.sub(pattern, swap, text)


command = args.pop(0)
if command == "info":
    if "--format" in args:
        print(os.environ.get("FAKE_DOCKER_INFO", "2 4294967296"))
elif command == "image":
    if args[-1] not in state["images"]:
        sys.exit(1)
    if "--format" in args:
        print("linux/amd64")
elif command == "build":
    state["images"].append(args[args.index("-t") + 1])
    save()
elif command == "run":
    there, inside = args[args.index("-v") + 1].rsplit(":", 1)
    state["mounts"][inside] = there
    save()
    print("c0ffee")
elif command == "cp":
    source, target = args[0], outside(args[1].split(":", 1)[1])
    if source.endswith("/."):
        shutil.copytree(source[:-2], target, dirs_exist_ok=True)
    else:
        shutil.copy(source, target)
elif command == "exec":
    cwd, env = None, dict(os.environ)
    while args[0] in ("-w", "-e", "-u"):
        flag, value = args.pop(0), args.pop(0)
        if flag == "-w":
            cwd = outside(value)
        elif flag == "-e":
            key, _, val = value.partition("=")
            env[key] = val
    args = [outside(arg) for arg in args[1:]]
    if cwd:
        os.chdir(cwd)
    os.execvpe(args[0], args, env)
elif command != "rm":
    sys.exit("fake docker: no " + command)
'''


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`docker` on the PATH is the fake; returns the folder with its calls and its containers' folders."""
    make_executable(tmp_path / "docker-bin" / "docker", FAKE_DOCKER)
    monkeypatch.setenv("PATH", f"{tmp_path / 'docker-bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_ROOT", str(tmp_path / "docker"))
    return tmp_path / "docker"


def docker_calls(root: Path) -> list[list[str]]:
    return [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]


def running(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


def started(tmp_path: Path) -> tuple[DockerSandbox, Path]:
    project = tmp_path / "project"
    project.mkdir()
    tag = chat.image_tag()
    DockerSandbox.ensure_image(tag, lambda _: None)
    return DockerSandbox.start(tag, project, "le-chonk-chat-test"), project.resolve()


def test_the_image_is_built_once_and_the_container_gets_the_task_limits(tmp_path: Path, fake_docker: Path) -> None:
    messages: list[str] = []
    tag = chat.image_tag()
    DockerSandbox.ensure_image(tag, messages.append)
    DockerSandbox.ensure_image(tag, messages.append)
    project = tmp_path / "project"
    project.mkdir()
    sandbox = DockerSandbox.start(tag, project, "le-chonk-chat-test")

    calls = docker_calls(fake_docker)
    builds = [c for c in calls if c[0] == "build"]
    assert builds == [["build", "-t", tag, str(TASK / "environment")]] and len(messages) == 1
    run = next(c for c in calls if c[0] == "run")
    assert run[run.index("--platform") + 1] == "linux/amd64" and "--init" in run
    assert run[run.index("--cpus") + 1] == "2" and run[run.index("--memory") + 1] == "4096m"  # all this Docker has
    assert run[run.index("-v") + 1] == f"{project.resolve()}:/app" and run[-3:] == [tag, "sleep", "infinity"]
    sandbox.remove()
    assert docker_calls(fake_docker)[-1] == ["rm", "-f", "le-chonk-chat-test"]


def test_the_image_tag_follows_the_environment_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "task"
    shutil.copytree(TASK, copy)
    monkeypatch.setenv("LECHONK_TASK_DIR", str(copy))
    before = chat.image_tag()
    assert before.startswith("le-chonk-runner-env:") and chat.image_tag() == before
    with (copy / "environment" / "warmup.sh").open("a") as handle:
        handle.write("# changed\n")
    assert chat.image_tag() != before


async def test_commands_run_in_the_container_with_their_folder_and_environment(tmp_path: Path, fake_docker: Path) -> None:
    sandbox, project = started(tmp_path)
    (project / "sub").mkdir()
    result = await sandbox.exec("pwd; echo $GREETING; echo oops >&2; exit 3", cwd="/app/sub", env={"GREETING": "hello"})
    assert result.return_code == 3 and result.stdout.split() == [str(project / "sub"), "hello"] and result.stderr == "oops\n"
    command = [c for c in docker_calls(fake_docker) if c[0] == "exec"][-1]
    assert command[:6] == ["exec", "-w", "/app/sub", "-e", "GREETING=hello", "le-chonk-chat-test"]
    assert command[6:9] == ["setsid", "-w", "bash"] and command[-1] == "pwd; echo $GREETING; echo oops >&2; exit 3"
    assert not list((fake_docker / "tmp").glob(".le-chonk-exec-*"))  # the pid file is gone


async def test_stopping_a_command_stops_what_it_started_in_the_container(tmp_path: Path, fake_docker: Path) -> None:
    sandbox, _ = started(tmp_path)
    marker = f"never-{uuid.uuid4().hex}"
    # As the agent's run_command does: `timeout` moves the command to a process group of its own.
    command = asyncio.create_task(sandbox.exec(f"timeout -k 10 600 bash -c 'sleep 30; echo {marker}'"))
    for _ in range(100):
        if running(marker):
            break
        await asyncio.sleep(0.05)
    assert running(marker)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    assert not running(marker) and not list((fake_docker / "tmp").glob(".le-chonk-exec-*"))


async def test_a_command_over_its_time_limit_is_stopped(tmp_path: Path, fake_docker: Path) -> None:
    sandbox, _ = started(tmp_path)
    marker = f"never-{uuid.uuid4().hex}"
    with pytest.raises(RuntimeError, match="timed out after 1 seconds"):
        await sandbox.exec(f"bash -c 'sleep 30; echo {marker}'", timeout_sec=1)
    assert not running(marker)


async def test_files_and_folders_are_copied_into_the_container(tmp_path: Path, fake_docker: Path) -> None:
    sandbox, project = started(tmp_path)
    local = tmp_path / "local.txt"
    local.write_text("hi")
    await sandbox.upload_file(local, "/app/copied.txt")
    assert (project / "copied.txt").read_text() == "hi"
    await sandbox.upload_dir(tmp_path / "skills" / "kotlin-tooling-kotlin-toolchain", "/app/skills/kotlin-tooling-kotlin-toolchain")
    assert (project / "skills" / "kotlin-tooling-kotlin-toolchain" / "SKILL.md").read_text() == FIXTURE_SKILL
    assert await sandbox.is_dir("/app/skills") and not await sandbox.is_dir("/app/nothing")


# --- `le-chonk chat` --------------------------------------------------------------------------------------

def scripted_input(monkeypatch: pytest.MonkeyPatch, lines: list[Any]) -> None:
    """input() returns these lines in turn, raises the exceptions among them, then raises EOFError (Ctrl-D)."""
    queue = list(lines)

    def fake_input(prompt: str = "") -> str:
        if not queue:
            raise EOFError
        line = queue.pop(0)
        if isinstance(line, type) and issubclass(line, BaseException):
            raise line
        return line

    monkeypatch.setattr(builtins, "input", fake_input)


def test_the_chat_command(tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    marker = f"never-{uuid.uuid4().hex}"
    client = FakeClient([
        # The command presses Ctrl-C for you, then waits.
        [text("Starting with a long command."), call("t1", "run_command", command=f"kill -INT {os.getpid()}; sleep 30; echo {marker}")],
        [text("OK, I won't wait for it.")],
    ])
    monkeypatch.setattr(StudioAgent, "_client", lambda self: client)
    scripted_input(monkeypatch, ["/help", KeyboardInterrupt, "/sh echo hello from the container", "/unknown", "Build it",
                                 '"""', "go on,", "without sleeping", '"""', "/cost", "/exit"])

    chat.run_chat(skill=False, model=MODEL, project=None, max_cost_usd=3.0, keep=False)
    out = capsys.readouterr().out

    assert "Chatting with the Studio agent le-chonk-builder-no-skill on mistral-large-4, without the skill." in out
    assert "/task      send the A/B task" in out and "(/exit or Ctrl-D ends the chat)" in out
    assert "hello from the container\n[exit code 0]" in out and "Unknown command /unknown" in out
    assert "agent › Starting with a long command." in out and "(stopped; tell the agent what to do next)" in out
    assert not running(marker)
    interrupted, message = client.inputs[-1]["inputs"]
    assert interrupted.result == INTERRUPTED and message.content == "go on,\nwithout sleeping"
    assert "agent › OK, I won't wait for it." in out and "(session: 2 model calls, 2,200 tokens, $0.00)" in out
    assert docker_calls(fake_docker)[-1][:2] == ["rm", "-f"]
    session = next((tmp_path / "chat").iterdir())
    assert (session / "app").is_dir() and (session / "trace.jsonl").is_file()


def test_the_chat_command_with_the_skill(tmp_path: Path, fake_docker: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    client = FakeClient([[text("I see the skill.")]])
    monkeypatch.setattr(StudioAgent, "_client", lambda self: client)
    scripted_input(monkeypatch, ["/task"])

    chat.run_chat(skill=True, model=MODEL, project=tmp_path / "mine", max_cost_usd=None, keep=True)
    out = capsys.readouterr().out

    assert "le-chonk-builder-skill on mistral-large-4, with the skill kotlin-tooling-kotlin-toolchain." in out
    assert client.inputs[0]["inputs"] == (TASK / "instruction.md").read_text()
    assert (fake_docker / "harbor" / "skills" / "kotlin-tooling-kotlin-toolchain" / "SKILL.md").read_text() == FIXTURE_SKILL
    assert "The container is still running: docker exec -it le-chonk-chat-" in out
    assert not any(c[0] == "rm" for c in docker_calls(fake_docker)) and (tmp_path / "mine").is_dir()


def test_the_chat_needs_a_key_and_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="MISTRAL_API_KEY"):
        chat.run_chat(skill=False, model=MODEL, project=None, max_cost_usd=None, keep=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setattr(chat.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="Docker isn't running"):
        chat.run_chat(skill=False, model=MODEL, project=None, max_cost_usd=None, keep=False)


def test_the_cli_passes_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    from le_chonk_eval.cli import main

    seen: dict[str, Any] = {}
    monkeypatch.setattr(chat, "run_chat", lambda **kwargs: seen.update(kwargs))
    main(["chat", "--skill", "--max-cost", "0", "--keep"])
    assert seen == {"skill": True, "model": MODEL, "project": None, "max_cost_usd": None, "keep": True}
