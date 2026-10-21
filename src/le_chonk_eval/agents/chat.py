"""Chat with the Studio agent in your terminal, while its tools run in the task's container.

    uv run le-chonk chat            the agent without the skill
    uv run le-chonk chat --skill    with the kotlin-tooling-kotlin-toolchain skill

It's the agent of the A/B: the same Studio agent, instructions and tools, in a container built from the
task's Dockerfile. The container's /app is a folder on your Mac (chat/<session>/app), so you can watch the
files in your IDE while you chat. The model calls go from your Mac: the container never sees the API key.
Type /help in the chat for its commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import mistralai.client.models as models
from harbor.environments.base import ExecResult

from ..utils.paths import chat_dir, skill_dir, task_dir
from .studio_agent import StudioAgent, _arguments, _message_text, _price_per_token, _short_args, build_instructions, discover_skills

WORKDIR = "/app"
SKILLS_DIR = "/harbor/skills"
CPUS, MEMORY_MB = 4, 6144  # as in task.toml, or what Docker has if that's less
INTERRUPTED = "Interrupted by the user: not run."
HELP = '''Commands:
  /task      send the A/B task (tasks/le-chonk-runner/instruction.md) as your message
  """        on a line of its own: start or end a message of several lines
  /sh CMD    run a command in the container yourself; the agent doesn't see it
  /new       start a new conversation; the container and the files stay
  /cost      tokens and cost so far
  /exit      stop; Ctrl-D works too
Ctrl-C while the agent works stops its turn; then tell it what to do next.
Anything else is a message to the agent.'''

# Each command runs in a session of its own in the container, with the session's id in a pid file, so
# stopping a command stops everything it started: killing the `docker exec` client alone leaves it running.
RUN_WRAPPER = 'echo $$ > "$0"; bash -c "$1"; status=$?; rm -f "$0"; exit $status'
STOP_SCRIPT = (
    'sid=$(cat "$0" 2>/dev/null) && [ -n "$sid" ] && { pkill -TERM -s "$sid"; '
    'for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -s "$sid" > /dev/null || break; sleep 0.3; done; '
    'pkill -KILL -s "$sid"; }; rm -f "$0"'
)


class StopChat(Exception):
    pass


# --- The container ----------------------------------------------------------------------------------------

def image_tag() -> str:
    """The image is tagged with a hash of the task's environment folder, so a changed Dockerfile is rebuilt."""
    environment = task_dir() / "environment"
    digest = hashlib.sha256()
    for path in sorted(p for p in environment.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(environment)).encode() + b"\0" + path.read_bytes())
    return f"le-chonk-runner-env:{digest.hexdigest()[:12]}"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {(result.stderr or result.stdout).strip()[-1000:]}")
    return result


def _limits() -> tuple[int, int]:
    """The task's CPUs and memory, capped to what Docker has: `docker run` refuses more than that."""
    info = _docker("info", "--format", "{{.NCPU}} {{.MemTotal}}", check=False).stdout.split()
    if len(info) != 2 or not all(part.isdigit() for part in info):
        return CPUS, MEMORY_MB
    return min(CPUS, int(info[0])), min(MEMORY_MB, int(info[1]) // 2**20)


class DockerSandbox:
    """A container from the task's image, with the bits of Harbor's environment API the Studio agent uses."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._ids = itertools.count(1)

    @staticmethod
    def ensure_image(tag: str, write: Callable[[str], None] = print) -> None:
        if _docker("image", "inspect", tag, check=False).returncode == 0:
            return
        write(f"Building the task's image as {tag}. Only the first time; it takes a while.")
        if subprocess.run(["docker", "build", "-t", tag, str(task_dir() / "environment")]).returncode != 0:
            raise SystemExit("The image build failed; its output is above.")

    @classmethod
    def start(cls, tag: str, project: Path, name: str) -> DockerSandbox:
        platform = _docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", tag).stdout.strip()
        cpus, memory_mb = _limits()
        _docker("run", "-d", "--init", "--name", name, "--platform", platform, "--cpus", str(cpus), "--memory", f"{memory_mb}m",
                "-v", f"{project.resolve()}:{WORKDIR}", "-w", WORKDIR, tag, "sleep", "infinity")
        return cls(name)

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: Any = None) -> ExecResult:
        pid_file = f"/tmp/.le-chonk-exec-{next(self._ids)}"
        args = ["docker", "exec"]
        if cwd:
            args += ["-w", cwd]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        if user is not None:
            args += ["-u", str(user)]
        args += [self.name, "setsid", "-w", "bash", "-c", RUN_WRAPPER, pid_file, command]
        # A session of its own here too, so Ctrl-C reaches only the chat, which then stops the command.
        process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            await asyncio.shield(self._stop(process, pid_file))
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds") from None
        return ExecResult(stdout=out.decode(errors="replace") or None, stderr=err.decode(errors="replace") or None,
                          return_code=process.returncode)

    async def _stop(self, process: asyncio.subprocess.Process, pid_file: str) -> None:
        """Kill the `docker exec` client, then everything its command started in the container."""
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        stopper = await asyncio.create_subprocess_exec(
            "docker", "exec", self.name, "sh", "-c", STOP_SCRIPT, pid_file, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            await asyncio.wait_for(stopper.wait(), timeout=30)
        except asyncio.TimeoutError:
            stopper.kill()
        with contextlib.suppress(asyncio.TimeoutError):  # its pipes may never close if nobody read them
            await asyncio.wait_for(process.wait(), timeout=5)

    async def is_dir(self, path: str, user: Any = None) -> bool:
        return (await self.exec(f"test -d {shlex.quote(path)}", timeout_sec=30)).return_code == 0

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        await asyncio.to_thread(_docker, "cp", str(source_path), f"{self.name}:{target_path}")

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await self.exec(f"mkdir -p {shlex.quote(target_dir)}", timeout_sec=30)
        await asyncio.to_thread(_docker, "cp", f"{source_dir}/.", f"{self.name}:{target_dir}")

    def remove(self) -> None:
        _docker("rm", "-f", self.name, check=False)


# --- The conversation -------------------------------------------------------------------------------------

def _describe(name: str, args: dict[str, Any]) -> str:
    if name == "run_command":
        command = str(args.get("command", "")).strip()
        first, _, rest = command.partition("\n")
        return f"  $ {first}{' …' if rest else ''}"
    if name == "write_file":
        return f"  write {args.get('path')} ({len(str(args.get('content', ''))):,} characters)"
    if name == "read_file":
        return f"  read {args.get('path')}"
    if name == "list_files":
        return f"  list {args.get('path') or '.'}"
    return f"  {name} {json.dumps(args)[:200]}"


def _result(name: str, text: str, outcome: dict[str, Any]) -> list[str]:
    if name == "run_command":
        lines = text.splitlines()
        tail = [line for line in lines[1:] if line.strip()][-4:]
        return [f"    {lines[0] if lines else ''}"] + [f"    | {line[:160]}" for line in tail]
    return [] if outcome.get("ok") else [f"    {text[:300]}"]


class ChatSession:
    """One conversation with the Studio agent; every message is a turn that runs tools until the agent answers."""

    def __init__(self, agent: StudioAgent, sandbox: Any, client: Any, workdir: str = WORKDIR,
                 write: Callable[[str], None] = print, max_cost_usd: float | None = None, trace: Path | None = None) -> None:
        self.agent, self.sandbox, self.client, self.workdir, self.write = agent, sandbox, client, workdir, write
        self.max_cost_usd, self.trace = max_cost_usd, trace
        self.price_in, self.price_out = _price_per_token(agent.model_id)
        self.totals = {"prompt": 0, "completion": 0, "model_calls": 0}
        self.conversation_id: str | None = None
        self.pending: list[Any] = []  # tool calls of an interrupted turn, still without a result sent
        self.finished: dict[str, str] = {}  # results of those calls that ran before the interruption
        self.agent_name = self.agent_id = ""
        self.skills: list[str] = []

    async def setup(self) -> None:
        skills = await discover_skills(self.sandbox, self.agent.skills_dir)
        self.skills = [s.name for s in skills]
        self.agent_name = f"le-chonk-builder-{'skill' if skills else 'no-skill'}"
        self.agent_id = await self.agent.ensure_agent(self.client, self.agent_name, build_instructions(self.workdir, skills))

    def reset(self) -> None:
        """A new conversation with the same agent, container and files."""
        self.conversation_id, self.pending, self.finished = None, [], {}
        self._log("reset")

    def cost(self) -> float | None:
        if self.price_in is None or self.price_out is None:
            return None
        return self.totals["prompt"] * self.price_in + self.totals["completion"] * self.price_out

    def _check_cost(self) -> None:
        cost = self.cost()
        if self.max_cost_usd is not None and cost is not None and cost >= self.max_cost_usd:
            raise StopChat(f"Stopped: the chat has cost ${cost:.2f}, the limit is ${self.max_cost_usd:.2f} (--max-cost).")

    def usage_line(self, before: dict[str, int] | None = None) -> str:
        cost = self.cost()
        session = f"session: {self.totals['model_calls']} model calls, {self.totals['prompt'] + self.totals['completion']:,} tokens"
        session += f", ${cost:.2f}" if cost is not None else ""
        if before is None:
            return f"({session})"
        calls = self.totals["model_calls"] - before["model_calls"]
        tokens = self.totals["prompt"] + self.totals["completion"] - before["prompt"] - before["completion"]
        return f"(turn: {calls} model calls, {tokens:,} tokens · {session})"

    def _log(self, kind: str, **fields: Any) -> None:
        if self.trace:
            with self.trace.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **fields}, default=str) + "\n")

    async def _send(self, inputs: Any) -> Any:
        if self.conversation_id is None:
            response = await self.client.beta.conversations.start_async(agent_id=self.agent_id, inputs=inputs, store=True)
            self.conversation_id = response.conversation_id
            self._log("conversation", conversation_id=self.conversation_id, agent=self.agent_name, agent_id=self.agent_id)
            return response
        return await self.client.beta.conversations.append_async(conversation_id=self.conversation_id, inputs=inputs)

    async def _open_turn(self, text: str) -> Any:
        if not self.pending:
            return await self._send(text)
        # The last turn was interrupted: send what its tool calls returned, or that they didn't run, then the message.
        inputs: list[Any] = [
            models.FunctionResultEntry(tool_call_id=c.tool_call_id, result=self.finished.get(c.tool_call_id, INTERRUPTED))
            for c in self.pending
        ]
        inputs.append(models.MessageInputEntry(role="user", content=text))
        self.pending, self.finished = [], {}
        try:
            return await self._send(inputs)
        except asyncio.CancelledError:
            raise
        except Exception:  # the interruption hit while those results were being sent: send the message alone
            return await self._send(text)

    async def send(self, text: str) -> None:
        """One turn: your message, then the agent's tool calls, until it answers with text only."""
        self._check_cost()
        before = dict(self.totals)
        self._log("user", text=text)
        response = await self._open_turn(text)
        while True:
            usage = getattr(response, "usage", None)
            self.totals["model_calls"] += 1
            self.totals["prompt"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.totals["completion"] += int(getattr(usage, "completion_tokens", 0) or 0)
            for entry in response.outputs:
                if isinstance(entry, models.MessageOutputEntry) and _message_text(entry.content).strip():
                    message = _message_text(entry.content).strip()
                    self.write(f"\nagent › {message}")
                    self._log("agent", text=message)
            calls = [entry for entry in response.outputs if isinstance(entry, models.FunctionCallEntry)]
            if not calls:
                self.write(self.usage_line(before))
                return
            self._check_cost()
            self.pending, self.finished = list(calls), {}
            for call in calls:
                args = _arguments(call.arguments)
                self.write(_describe(call.name, args))
                result, outcome = await self.agent.run_tool(self.sandbox, self.workdir, call.name, args)
                self.finished[call.tool_call_id] = result
                for line in _result(call.name, result, outcome):
                    self.write(line)
                self._log("tool", tool=call.name, args=_short_args(args), **outcome)
            results = [models.FunctionResultEntry(tool_call_id=c.tool_call_id, result=self.finished[c.tool_call_id]) for c in calls]
            response = await self._send(results)
            self.pending, self.finished = [], {}


# --- The command ------------------------------------------------------------------------------------------

def read_message() -> str:
    """One line, or the lines between two lines of three double quotes."""
    print()  # not in the prompt: readline miscounts a prompt with a line break
    line = input("you › ")
    if line.strip() != '"""':
        return line.strip()
    lines = []
    while (line := input("... ")).strip() != '"""':
        lines.append(line)
    return "\n".join(lines).strip()


def run_chat(skill: bool, model: str, project: Path | None, max_cost_usd: float | None, keep: bool) -> None:
    if not os.environ.get("MISTRAL_API_KEY"):
        raise SystemExit("MISTRAL_API_KEY is not set. Put it in .env (see .env.example).")
    if not shutil.which("docker") or _docker("info", check=False).returncode != 0:
        raise SystemExit("Docker isn't running. Start Docker Desktop or OrbStack first.")
    try:
        import readline  # noqa: F401  (line editing and history for input())
    except ImportError:
        pass

    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = chat_dir() / session_id
    project = (project or session_dir / "app").resolve()
    project.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        tag = image_tag()
        DockerSandbox.ensure_image(tag)
        sandbox = DockerSandbox.start(tag, project, f"le-chonk-chat-{session_id}")
    except KeyboardInterrupt:
        raise SystemExit("\nStopped before the chat started.") from None
    loop = asyncio.new_event_loop()

    def interruptible(coroutine: Any) -> Any:
        """Run one turn or command; Ctrl-C cancels it, which stops its command in the container too."""
        task = loop.create_task(coroutine)
        stopped = False

        def stop() -> None:
            nonlocal stopped
            stopped = True
            task.cancel()

        loop.add_signal_handler(signal.SIGINT, stop)
        try:
            return loop.run_until_complete(task)
        except asyncio.CancelledError:
            if not stopped:
                raise
            print("\n(stopped; tell the agent what to do next)")
            return None
        finally:
            loop.remove_signal_handler(signal.SIGINT)

    try:
        if skill:
            loop.run_until_complete(sandbox.upload_dir(skill_dir(), f"{SKILLS_DIR}/{skill_dir().name}"))
        agent = StudioAgent(logs_dir=session_dir, model_name=model, skills_dir=SKILLS_DIR if skill else None)
        session = ChatSession(agent, sandbox, agent._client(), max_cost_usd=max_cost_usd, trace=session_dir / "trace.jsonl")
        try:
            loop.run_until_complete(session.setup())
        except Exception as exc:
            print(f"Couldn't set up the Studio agent: {type(exc).__name__}: {str(exc)[:500]}")
            return
        print(f"\nChatting with the Studio agent {session.agent_name} on {agent.model_id}"
              f"{', with the skill ' + ', '.join(session.skills) if session.skills else ', without the skill'}.")
        print(f"Its project folder is {project} (/app in the container). /help lists the commands.")

        while True:
            try:
                text = read_message()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("\n(/exit or Ctrl-D ends the chat)")
                continue
            if not text:
                continue
            command, _, rest = text.partition(" ")
            if command in ("/exit", "/quit"):
                break
            if command == "/help":
                print(HELP)
                continue
            if command == "/cost":
                print(session.usage_line())
                continue
            if command == "/new":
                session.reset()
                print("(a new conversation; the files stay)")
                continue
            if command == "/sh":
                if not rest.strip():
                    print("Usage: /sh CMD")
                    continue
                try:
                    result = interruptible(sandbox.exec(rest, cwd=WORKDIR, timeout_sec=1800))
                except Exception as exc:
                    print(f"(error: {exc})")
                    continue
                if result is not None:
                    output = ((result.stdout or "") + (result.stderr or ""))[-4000:]
                    print(f"{output}{'' if not output or output.endswith(chr(10)) else chr(10)}[exit code {result.return_code}]")
                continue
            if command == "/task":
                text = (task_dir() / "instruction.md").read_text(encoding="utf-8")
                print("(sending the A/B task)")
            elif command.startswith("/") and command[1:].isalpha():
                print(f"Unknown command {command}. /help lists the commands.")
                continue
            try:
                interruptible(session.send(text))
            except StopChat as stop:
                print(f"\n{stop}")
                break
            except Exception as exc:
                print(f"\n(error: {type(exc).__name__}: {str(exc)[:500]})")
                print("(send another message, or /new for a new conversation; the files stay as they are)")
    except KeyboardInterrupt:  # Ctrl-C while setting up
        print()
    finally:
        with contextlib.suppress(KeyboardInterrupt):
            leftovers = asyncio.all_tasks(loop)  # e.g. a command still being stopped
            if leftovers:
                loop.run_until_complete(asyncio.gather(*leftovers, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
        print(f"\nConversation: {session_dir / 'trace.jsonl'} · project: {project}")
        if keep:
            print(f"The container is still running: docker exec -it {sandbox.name} bash")
        else:
            sandbox.remove()
