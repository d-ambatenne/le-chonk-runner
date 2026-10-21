"""A Harbor agent that runs a Mistral Studio agent on a task.

The agent is a Studio agent (Agents API), named le-chonk-builder-no-skill or le-chonk-builder-skill, so
every trial is a conversation you can open in Studio. The model calls go from this machine to the Mistral
API; the four tools run inside the trial's container through Harbor's environment.exec, so the API key
never enters the container.

    harbor run -p tasks/le-chonk-runner -a le_chonk_eval.agents.studio_agent:StudioAgent \\
        -m mistral/mistral-large-4 --env-file .env [--skill skills/kotlin-tooling-kotlin-toolchain]

With --skill, Harbor copies the skill into the container (/harbor/skills) and this agent lists it in the
instructions, the way coding agents show skills: name, description and the path of SKILL.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import (
    Agent as AtifAgent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.utils.trajectory_utils import format_trajectory_json


VERSION = "0.1.0"
HEAD_CHARS = 2_000
MAX_OUTPUT_CHARS = 10_000  # command output kept for the model
MAX_READ_CHARS = 60_000
MAX_WRITE_CHARS = 400_000
SKIP_DIRS = ("build", ".kotlin", ".gradle", ".git", ".idea", "node_modules")

BASE_INSTRUCTIONS = """You are an autonomous software engineer. You work alone on the task the user gives you, in a Linux container.

- The project directory is {workdir}. Relative paths are resolved there.
- Your tools: run_command runs a bash command (there is no interactive input, so a command that waits for a prompt fails); read_file, write_file and list_files work with files.
- Work until the task is done and checked with the builds and tests it asks for. Don't ask questions: make reasonable choices and keep going.
- When you are done, reply with a short summary and the commands that build and test the project, and call no more tools."""

SKILLS_SECTION = """

## Skills

A skill is a folder of instructions for a specific kind of work. Before you start, check whether one of these fits the task. If one does, read its SKILL.md with read_file and follow it.

{skills}"""


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS = [
    _tool(
        "run_command",
        "Run a bash command in the project directory and return its exit code and output.",
        {
            "command": {"type": "string", "description": "The command line to run, for example a build or test command."},
            "timeout_seconds": {"type": "integer", "description": "Time limit in seconds, 10 to 1800. Default 600."},
        },
        ["command"],
    ),
    _tool(
        "read_file",
        "Read a text file, with line numbers.",
        {
            "path": {"type": "string", "description": "File path, absolute or relative to the project directory."},
            "start_line": {"type": "integer", "description": "First line to return, starting at 1."},
            "max_lines": {"type": "integer", "description": "Maximum number of lines to return. Default 400."},
        },
        ["path"],
    ),
    _tool(
        "write_file",
        "Create or overwrite a text file. Missing parent folders are created.",
        {
            "path": {"type": "string", "description": "File path, absolute or relative to the project directory."},
            "content": {"type": "string", "description": "The complete new content of the file."},
        },
        ["path", "content"],
    ),
    _tool(
        "list_files",
        "List files and folders. Build output and caches are skipped.",
        {
            "path": {"type": "string", "description": "Folder to list, absolute or relative to the project directory. Default: the project directory."},
            "max_depth": {"type": "integer", "description": "How many folder levels to show, 1 to 6. Default 3."},
        },
        [],
    ),
]
TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}


@dataclass
class Skill:
    name: str
    description: str
    location: str


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        import yaml

        data = yaml.safe_load(text[3:end]) or {}
    except Exception:
        return {}
    return {k: " ".join(str(v).split()) for k, v in data.items() if k in ("name", "description")} if isinstance(data, dict) else {}


async def discover_skills(environment: BaseEnvironment, skills_dir: str | None) -> list[Skill]:
    """Skills Harbor injected: <skills_dir>/<name>/SKILL.md, with name and description from the frontmatter."""
    if not skills_dir or not await environment.is_dir(skills_dir):
        return []
    found = await environment.exec(
        f"find {shlex.quote(skills_dir)} -mindepth 2 -maxdepth 2 -name SKILL.md -type f | sort", timeout_sec=30
    )
    skills = []
    for path in (found.stdout or "").split():
        text = await environment.exec(f"cat {shlex.quote(path)}", timeout_sec=30)
        meta = _frontmatter(text.stdout or "")
        if meta.get("name") and meta.get("description"):
            skills.append(Skill(meta["name"], meta["description"], path))
    return skills


def build_instructions(workdir: str, skills: list[Skill]) -> str:
    text = BASE_INSTRUCTIONS.format(workdir=workdir)
    if skills:
        entries = "\n".join(
            f"<skill>\n  <name>{s.name}</name>\n  <description>{s.description}</description>\n  <location>{s.location}</location>\n</skill>"
            for s in skills
        )
        text += SKILLS_SECTION.format(skills=f"<available_skills>\n{entries}\n</available_skills>")
    return text


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    tail = max(0, limit - HEAD_CHARS)
    return f"{text[:HEAD_CHARS]}\n... ({len(text) - HEAD_CHARS - tail} characters cut) ...\n{text[-tail:]}"


def _price_per_token(model_id: str) -> tuple[float | None, float | None]:
    if os.environ.get("LECHONK_PRICE_INPUT") and os.environ.get("LECHONK_PRICE_OUTPUT"):
        return float(os.environ["LECHONK_PRICE_INPUT"]) / 1e6, float(os.environ["LECHONK_PRICE_OUTPUT"]) / 1e6
    try:
        import litellm

        entry = litellm.model_cost.get(f"mistral/{model_id}") or litellm.model_cost.get(model_id) or {}
    except Exception:
        entry = {}
    return entry.get("input_cost_per_token"), entry.get("output_cost_per_token")


class StudioAgentOptions(AgentOptions):
    max_turns: int = Field(default=80, ge=1, le=500, description="Maximum number of model responses.")
    command_timeout_sec: int = Field(default=1800, ge=10, le=3600, description="Upper limit for one run_command call.")
    max_cost_usd: float | None = Field(default=None, gt=0, description="Stop the trial once its model calls cost this many USD.")
    temperature: float | None = Field(default=None, description="Sampling temperature of the Studio agent.")


class StudioAgent(BaseAgent):
    capabilities = AgentCapabilities(atif=True, skills=True)
    options_model = StudioAgentOptions
    options: StudioAgentOptions

    @staticmethod
    def name() -> str:
        return "mistral-studio"

    def version(self) -> str:
        return VERSION

    @classmethod
    def preflight(cls, kwargs: dict[str, Any] | None = None, env: Any = None) -> None:
        super().preflight(kwargs, env)
        if not ((env or {}).get("MISTRAL_API_KEY") or os.environ.get("MISTRAL_API_KEY")):
            raise ValueError("MISTRAL_API_KEY is not set: run harbor with --env-file .env")

    async def setup(self, environment: BaseEnvironment) -> None:
        """Nothing to install: the tools are plain shell commands run through environment.exec."""

    # --- Mistral ------------------------------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        if not self.model_name:
            raise ValueError("A model is required, e.g. -m mistral/mistral-large-4")
        return self.model_name.split("/", 1)[-1]

    def _client(self) -> Any:
        from mistralai.client import Mistral
        from mistralai.client.utils import BackoffStrategy, RetryConfig

        retry = RetryConfig("backoff", BackoffStrategy(2_000, 60_000, 1.8, 900_000), True, ["429", "5XX"])
        return Mistral(api_key=self._get_env("MISTRAL_API_KEY"), retry_config=retry, timeout_ms=600_000)

    async def ensure_agent(self, client: Any, name: str, instructions: str) -> str:
        """Create the Studio agent once, and update it (a new version) only when its configuration changes."""
        from .studio import ensure_studio_agent

        completion_args = {"temperature": self.options.temperature} if self.options.temperature is not None else None
        return await ensure_studio_agent(
            client, name, model=self.model_id, instructions=instructions, tools=TOOLS, completion_args=completion_args,
            description="Builds Le Chonk Runner with the Kotlin Toolchain; driven by Harbor (le-chonk-runner).",
            logger=self.logger,
        )

    # --- Tools, run inside the container ------------------------------------------------------------------

    def _resolve(self, workdir: str, path: str | None) -> str:
        path = (path or ".").strip() or "."
        return path if PurePosixPath(path).is_absolute() else str(PurePosixPath(workdir) / path)

    async def run_tool(self, environment: BaseEnvironment, workdir: str, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Returns the text for the model and a record for the trace."""
        if name not in TOOL_NAMES:
            return f"Error: unknown tool '{name}'. Use only run_command, read_file, write_file and list_files.", {"ok": False}
        try:
            if name == "run_command":
                return await self._run_command(environment, workdir, str(args["command"]), args.get("timeout_seconds"))
            if name == "read_file":
                return await self._read_file(environment, workdir, str(args["path"]), args.get("start_line"), args.get("max_lines"))
            if name == "write_file":
                return await self._write_file(environment, workdir, str(args["path"]), str(args.get("content", "")))
            return await self._list_files(environment, workdir, args.get("path"), args.get("max_depth"))
        except KeyError as exc:
            return f"Error: missing argument {exc}.", {"ok": False}
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}", {"ok": False}

    async def _run_command(self, environment: BaseEnvironment, workdir: str, command: str, timeout: Any) -> tuple[str, dict[str, Any]]:
        limit = max(10, min(int(timeout or 600), self.options.command_timeout_sec))
        # `timeout` kills the command inside the container too; exec's own timeout below is the backstop.
        quoted = shlex.quote(command)
        wrapped = f"if command -v timeout >/dev/null 2>&1; then timeout -k 10 {limit} bash -c {quoted}; else bash -c {quoted}; fi"
        env = {"TERM": "dumb", "NO_COLOR": "1", "KOTLIN_CLI_NO_WELCOME_BANNER": "1"}
        started = time.monotonic()
        try:
            result = await environment.exec(wrapped, cwd=workdir, env=env, timeout_sec=limit + 60)
        except RuntimeError as exc:
            if "timed out" not in str(exc):
                raise
            seconds = round(time.monotonic() - started, 1)
            return f"[timed out after {limit}s]", {"ok": False, "exit_code": None, "timed_out": True, "seconds": seconds}
        seconds = round(time.monotonic() - started, 1)
        output = (result.stdout or "") + (f"\n{result.stderr}" if result.stderr else "")
        timed_out = result.return_code == 124
        status = f"timed out after {limit}s" if timed_out else f"exit code {result.return_code}"
        record = {"ok": result.return_code == 0, "exit_code": result.return_code, "timed_out": timed_out, "seconds": seconds, "output_chars": len(output)}
        return f"[{status}, {seconds}s]\n{_trim(output, MAX_OUTPUT_CHARS)}", record

    async def _read_file(self, environment: BaseEnvironment, workdir: str, path: str, start: Any, count: Any) -> tuple[str, dict[str, Any]]:
        target = shlex.quote(self._resolve(workdir, path))
        first, lines = max(1, int(start or 1)), max(1, min(int(count or 400), 2000))
        script = (
            f"test -f {target} || {{ echo '__NOT_A_FILE__'; exit 3; }}; "
            f"awk -v s={first} -v n={lines} 'NR>=s && NR<s+n {{ printf \"%5d  %s\\n\", NR, $0 }} "
            f"END {{ if (NR >= s+n) printf \"... (%d more lines)\\n\", NR-(s+n)+1 }}' {target}"
        )
        result = await environment.exec(script, timeout_sec=60)
        if result.return_code != 0 or "__NOT_A_FILE__" in (result.stdout or ""):
            return f"Error: '{path}' does not exist or is not a file.", {"ok": False, "path": path}
        text = result.stdout or "(empty file)"
        return _trim(text, MAX_READ_CHARS), {"ok": True, "path": path, "chars": len(text)}

    async def _write_file(self, environment: BaseEnvironment, workdir: str, path: str, content: str) -> tuple[str, dict[str, Any]]:
        if len(content) > MAX_WRITE_CHARS:
            return f"Error: content is larger than {MAX_WRITE_CHARS} characters.", {"ok": False, "path": path}
        target = self._resolve(workdir, path)
        existed = (await environment.exec(f"test -e {shlex.quote(target)}", timeout_sec=30)).return_code == 0
        await environment.exec(f"mkdir -p {shlex.quote(str(PurePosixPath(target).parent))}", timeout_sec=30)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(content)
            local = handle.name
        try:
            os.chmod(local, 0o644)
            await environment.upload_file(local, target)
        finally:
            os.unlink(local)
        verb = "Overwrote" if existed else "Created"
        return f"{verb} {path} ({len(content)} characters).", {"ok": True, "path": path, "chars": len(content), "overwrote": existed}

    async def _list_files(self, environment: BaseEnvironment, workdir: str, path: Any, depth: Any) -> tuple[str, dict[str, Any]]:
        base = self._resolve(workdir, str(path) if path else ".")
        levels = max(1, min(int(depth or 3), 6))
        prune = " -o ".join(f"-name {shlex.quote(d)}" for d in SKIP_DIRS)
        # POSIX find and wc only, so the same command works with GNU and BSD tools.
        script = (
            f"cd {shlex.quote(base)} && find . -mindepth 1 -maxdepth {levels} \\( {prune} \\) -prune -o -print "
            "| sort | head -n 501 | while IFS= read -r p; do "
            'if [ -d "$p" ]; then echo "d 0 $p"; else echo "f $(wc -c < "$p" | tr -d \' \') $p"; fi; done'
        )
        result = await environment.exec(script, timeout_sec=60)
        if result.return_code != 0:
            return f"Error: '{path or '.'}' is not a folder.", {"ok": False}
        lines = []
        for row in (result.stdout or "").splitlines():
            kind, size, rel = (row.split(" ", 2) + ["", ""])[:3]
            rel = rel[2:] if rel.startswith("./") else rel
            lines.append(f"{rel}/" if kind == "d" else f"{rel}  ({size} bytes)")
        if len(lines) > 500:
            lines = lines[:500] + ["... (truncated at 500 entries)"]
        return "\n".join(lines) or "(empty)", {"ok": True, "entries": len(lines)}

    # --- The loop -----------------------------------------------------------------------------------------

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        import mistralai.client.models as models

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        trace_path = self.logs_dir / "trace.jsonl"
        workdir = ((await environment.exec("pwd", timeout_sec=30)).stdout or "/app").strip() or "/app"
        skills = await discover_skills(environment, self.skills_dir)
        arm = "skill" if skills else "no-skill"
        agent_name = f"le-chonk-builder-{arm}"
        instructions = build_instructions(workdir, skills)
        price_in, price_out = _price_per_token(self.model_id)

        steps: list[Step] = [Step(step_id=1, source="system", message=instructions), Step(step_id=2, source="user", message=instruction)]
        totals = {"prompt": 0, "completion": 0, "model_calls": 0}
        meta: dict[str, Any] = {"arm": arm, "studio_agent": agent_name, "skills": [s.name for s in skills], "stop_reason": None}

        def log(kind: str, **fields: Any) -> None:
            with trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **fields}, default=str) + "\n")

        def spent() -> float | None:
            if price_in is None or price_out is None:
                return None
            return round(totals["prompt"] * price_in + totals["completion"] * price_out, 6)

        def update_context() -> None:
            context.n_input_tokens = totals["prompt"]
            context.n_output_tokens = totals["completion"]
            context.cost_usd = spent()
            context.metadata = {**meta, "model_calls": totals["model_calls"]}

        def record(response: Any) -> None:
            usage = getattr(response, "usage", None)
            totals["model_calls"] += 1
            totals["prompt"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            totals["completion"] += int(getattr(usage, "completion_tokens", 0) or 0)

        cap = self.options.max_cost_usd
        if cap is not None and price_in is None:
            meta["warning"] = f"max_cost_usd is ignored: no price for {self.model_id} (set LECHONK_PRICE_INPUT and LECHONK_PRICE_OUTPUT)"
        try:
            client = self._client()
            agent_id = await self.ensure_agent(client, agent_name, instructions)
            meta["studio_agent_id"] = agent_id
            log("start", agent_id=agent_id, arm=arm, model=self.model_id, skills=meta["skills"])
            response = await client.beta.conversations.start_async(agent_id=agent_id, inputs=instruction, store=True)
            meta["conversation_id"] = response.conversation_id
            for _ in range(self.options.max_turns):
                record(response)
                calls = [o for o in response.outputs if isinstance(o, models.FunctionCallEntry)]
                texts = [o for o in response.outputs if isinstance(o, models.MessageOutputEntry)]
                message = "\n".join(_message_text(t.content) for t in texts).strip()
                usage = getattr(response, "usage", None)
                step = Step(
                    step_id=len(steps) + 1,
                    source="agent",
                    message=message,
                    model_name=self.model_id,
                    metrics=Metrics(prompt_tokens=getattr(usage, "prompt_tokens", None), completion_tokens=getattr(usage, "completion_tokens", None)),
                )
                if not calls:
                    steps.append(step)
                    meta["stop_reason"] = "done"
                    meta["final_message"] = message[:4000]
                    break
                cost = spent()
                if cap is not None and cost is not None and cost >= cap:
                    steps.append(step)  # its tool calls aren't run: the budget is spent
                    meta["stop_reason"] = "max_cost"
                    break
                results, observations, tool_calls = [], [], []
                for call in calls:
                    args = _arguments(call.arguments)
                    text, outcome = await self.run_tool(environment, workdir, call.name, args)
                    log("tool", tool=call.name, args=_short_args(args), **outcome)
                    tool_calls.append(ToolCall(tool_call_id=call.tool_call_id, function_name=call.name, arguments=args))
                    observations.append(ObservationResult(source_call_id=call.tool_call_id, content=text))
                    results.append(models.FunctionResultEntry(tool_call_id=call.tool_call_id, result=text))
                step.tool_calls, step.observation = tool_calls, Observation(results=observations)
                steps.append(step)
                update_context()
                response = await client.beta.conversations.append_async(conversation_id=response.conversation_id, inputs=results)
            else:
                record(response)
                meta["stop_reason"] = "max_turns"
        except asyncio.CancelledError:
            meta["stop_reason"] = "cancelled (agent timeout)"
            raise
        except Exception as exc:  # the trial is still verified: an agent that fails is data
            meta["stop_reason"] = "error"
            meta["error"] = f"{type(exc).__name__}: {exc}"[:2000]
            log("error", error=meta["error"])
        finally:
            update_context()
            log("end", **{k: v for k, v in meta.items() if k != "final_message"}, **totals)
            trajectory = Trajectory(
                session_id=meta.get("conversation_id"),
                agent=AtifAgent(name=self.name(), version=VERSION, model_name=self.model_id, tool_definitions=TOOLS,
                                extra={"studio_agent": agent_name, "studio_agent_id": meta.get("studio_agent_id")}),
                steps=steps,
                final_metrics=FinalMetrics(
                    total_prompt_tokens=totals["prompt"],
                    total_completion_tokens=totals["completion"],
                    total_cost_usd=context.cost_usd,
                    total_steps=len(steps),
                ),
            )
            (self.logs_dir / "trajectory.json").write_text(format_trajectory_json(trajectory.to_json_dict()), encoding="utf-8")


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for chunk in content or []:
        text = getattr(chunk, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"value": value}
        except json.JSONDecodeError:
            return {"input": raw}
    return {}


def _short_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:300] + f"... ({len(v)} chars)" if isinstance(v, str) and len(v) > 300 else v) for k, v in args.items()}
