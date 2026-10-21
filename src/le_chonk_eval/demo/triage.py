"""The triage agent: a Studio agent that turns Slack requests into GitHub issues Vibe can implement.

It checks each request against demo/task-checklist.md, looks for duplicates among the repo's issues, and
creates the issue. In the same thread, people attach skills, ask for an A/B, or queue a task again. Every
Slack thread is one Studio conversation. In the demo, the tools run on this Mac with its GitHub token, and the
ids are kept in .state/; the next phase runs the same code in a workflow, with the GitHub connector and the
ids in the workflow's state (le_chonk_eval.next).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mistralai.client.models as models

from ..agents.studio import ensure_studio_agent
from ..agents.studio_agent import _arguments, _message_text
from ..utils.paths import demo_dir, state_dir
from .github import GitHubError, check_skill, parse_repo
from .issues import AB_DONE, AB_FAILED, AB_REQUESTED, AB_RUNNING, DONE, FAILED, NOT_DONE, READY, RUNNING, Task, label_names

log = logging.getLogger(__name__)

AGENT_NAME = "le-chonk-triage"
DEFAULT_AB_ATTEMPTS = 3
MAX_TOOL_ROUNDS = 12

INSTRUCTIONS = """You are the task intake of a software project, in Slack. People describe work for Vibe, a coding agent, and you turn each request into a GitHub issue that Vibe can implement. A pipeline then picks up the issue, runs Vibe, and posts the result in the same Slack thread.

## The project
The channel's project is a GitHub repository. Call get_project first. If there is none, ask for the repository's URL and call set_project with it.
When the person asks for a new repository, call create_repo with the name they give, then set_project with its URL. Never create one they didn't ask for.

## A new task
1. Check the request against the checklist below. If something is missing or unclear, ask for it in one short message and suggest wording. Don't add requirements nobody asked for.
2. Look for duplicates: call find_issues with the request's key words. If an issue asks for the same outcome, give its link and ask whether to go on.
3. When the checklist passes and there's no duplicate, call create_issue right away, without asking for a confirmation. Vibe starts on it within a minute, so use hold=true when the person wants to add a skill or check the issue first; queue_issue starts it later.
4. Reply with the issue link and one line on what happens next.

## Other requests
- A skill for a task: add_skill, with the issue number and the link to the skill's folder on GitHub.
- An A/B for a task: mark_for_ab. It needs a skill on the issue, and runs {ab_attempts} times per arm unless asked otherwise.
- Implement a task again, or after an A/B: queue_issue.
- Questions about tasks: get_issue or find_issues.

## Style
Short Slack messages in plain text, without headings. Always give issue links. If a tool fails, say what failed in one line.

## Checklist
{checklist}"""

DEFAULT_CHECKLIST = """1. One outcome: one feature or one fix.
2. Acceptance criteria: one to five statements that can be checked in the Android app or with a unit test.
3. Small enough for one session of a coding agent.
4. Testable on Android or in the shared tests: iOS can't be built.
5. About this project's code: no secrets, accounts or outside services.
6. No open issue asks for the same outcome."""


def _tool(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or []}}}


TOOLS = [
    _tool("get_project", "The GitHub repository of this Slack channel, if one is set."),
    _tool("set_project", "Set the channel's GitHub repository and start watching its issues.",
          {"url": {"type": "string", "description": "https://github.com/<owner>/<repo>"}}, ["url"]),
    _tool("create_repo", "Create an empty private GitHub repository for a new app, in the connected account. Only when asked.", {
        "name": {"type": "string", "description": "The repository's name: letters, digits, '-', '_' and '.'"},
        "description": {"type": "string", "description": "One line on the app; empty if none was given"},
    }, ["name"]),
    _tool("find_issues", "Search the project's issues, open and closed, to find duplicates or a task by its words.",
          {"words": {"type": "string", "description": "A few key words of the request"}}, ["words"]),
    _tool("create_issue", "Create the task as a GitHub issue, queued for Vibe.", {
        "title": {"type": "string", "description": "Short, imperative, under 70 characters"},
        "goal": {"type": "string", "description": "What to change and why, in one to three sentences"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "One to five checkable statements"},
        "notes": {"type": "string", "description": "Constraints or hints from the request; empty if none"},
        "skills": {"type": "array", "items": {"type": "string"}, "description": "Skill folder links given with the request"},
        "hold": {"type": "boolean", "description": "True to create it without queueing it for Vibe yet"},
    }, ["title", "goal", "acceptance_criteria"]),
    _tool("get_issue", "Read one issue: its task, labels and state.", {"number": {"type": "integer"}}, ["number"]),
    _tool("add_skill", "Attach a skill to an issue, for its implementation and A/B.", {
        "number": {"type": "integer"},
        "url": {"type": "string", "description": "https://github.com/<owner>/<repo>/tree/<branch>/<folder with SKILL.md>"},
    }, ["number", "url"]),
    _tool("mark_for_ab", "Run the skill A/B for an issue: Vibe without the issue's skills and with them.", {
        "number": {"type": "integer"},
        "attempts": {"type": "integer", "description": "Runs per arm, 1 to 8"},
    }, ["number"]),
    _tool("queue_issue", "Queue an issue for Vibe again: after a failure, or to implement it after an A/B.",
          {"number": {"type": "integer"}}, ["number"]),
]


class JsonStore:
    """A small JSON file of keys and values under .state/, written atomically."""

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def path(self) -> Path:
        return state_dir() / self.name

    def read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, key: str) -> Any:
        return self.read().get(key)

    def set(self, key: str, value: Any) -> None:
        data = self.read()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)


class MemoryStore:
    """The same keys and values in memory: a workflow passes them in and keeps what comes back."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})

    def read(self) -> dict[str, Any]:
        return dict(self.data)

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def set(self, key: str, value: Any) -> None:
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value


Store = JsonStore | MemoryStore
PROJECTS = JsonStore("slack-projects.json")
THREADS = JsonStore("slack-threads.json")


@dataclass
class SlackContext:
    channel: str
    thread: str
    user: str | None = None

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.thread}"


StartWatcher = Callable[[str], Awaitable[str]]


def _issue_summary(issue: dict[str, Any]) -> dict[str, Any]:
    task = Task.from_issue(issue["title"], issue.get("body"))
    return {"number": issue["number"], "title": issue["title"], "state": issue.get("state"), "url": issue.get("html_url"),
            "labels": sorted(label_names(issue)), "goal": task.goal[:300]}


class TriageTools:
    """The tools, run here for one Slack thread."""

    def __init__(self, github: Any, context: SlackContext, start_watcher: StartWatcher | None = None,
                 projects: Store | None = None) -> None:
        self.github, self.context, self.start_watcher = github, context, start_watcher
        self.projects = projects if projects is not None else PROJECTS

    async def call(self, name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"_{name}", None)
        if name not in {t["function"]["name"] for t in TOOLS} or handler is None:
            return f"Error: unknown tool {name!r}."
        try:
            return json.dumps(await handler(**args), ensure_ascii=False)
        except TypeError as exc:
            return f"Error: wrong arguments for {name}: {exc}"
        except (ValueError, GitHubError) as exc:
            return f"Error: {exc}"
        except Exception as exc:  # the network, for example: the agent tells the person
            log.exception(f"tool {name} failed")
            return f"Error: {type(exc).__name__}: {str(exc)[:300]}"

    def _repo(self) -> str:
        repo = self.projects.get(self.context.channel)
        if not repo:
            raise ValueError("No project is set for this channel: ask for the GitHub repository's URL and call set_project.")
        return repo

    async def _get_project(self) -> dict[str, Any]:
        repo = self.projects.get(self.context.channel)
        return {"repo": repo, "url": f"https://github.com/{repo}" if repo else None}

    async def _set_project(self, url: str) -> dict[str, Any]:
        repo = parse_repo(url)
        info = await self.github.repo(repo)
        await self.github.ensure_labels(repo)
        # The watcher first: if it can't start, the channel keeps its project rather than one nothing runs.
        watcher = await self.start_watcher(repo) if self.start_watcher else "not started: run `uv run le-chonk watch` on the Mac"
        self.projects.set(self.context.channel, repo)
        return {"repo": repo, "default_branch": info.get("default_branch"), "watcher": watcher}

    async def _create_repo(self, name: str, description: str = "") -> dict[str, Any]:
        name = name.strip().removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name) or name in (".", ".."):
            raise ValueError(f"{name!r} can't be a repository name: use letters, digits, '-', '_' and '.'")
        try:
            repo = await self.github.create_repo(name, description.strip())
        except GitHubError as exc:
            if exc.status != 403:
                raise
            raise ValueError(f"GitHub refused to create {name} ({exc}): the GitHub access this agent has may not create repositories. "
                             f"Ask the person to create an empty private repository named {name} on GitHub themselves, to give the "
                             "agent's GitHub access to it (for Studio's GitHub connector: github.com/apps/mistralai › Configure › "
                             "Repository access), and to send you its URL; then call set_project with it.") from exc
        return {"repo": repo["full_name"], "url": repo["html_url"], "private": True, "next": "call set_project with this URL"}

    async def _find_issues(self, words: str) -> dict[str, Any]:
        repo = self._repo()
        found = await self.github.search_issues(repo, words)
        if not found:  # the search index lags behind new issues: look at the open ones too
            found = await self.github.issues(repo, state="open", limit=50)
        return {"issues": [_issue_summary(i) for i in found if "pull_request" not in i][:20]}

    async def _create_issue(self, title: str, goal: str, acceptance_criteria: list[str], notes: str = "",
                            skills: list[str] | None = None, hold: bool = False) -> dict[str, Any]:
        repo = self._repo()
        checked = [(await check_skill(self.github, url))["url"] for url in skills or []]
        task = Task(title=title.strip(), goal=goal, criteria=[c for c in acceptance_criteria if c.strip()], notes=notes,
                    skills=checked, slack_channel=self.context.channel, slack_thread=self.context.thread,
                    requested_by=self.context.user)
        labels = [] if hold else [READY]
        issue = await self.github.create_issue(repo, task.title, task.body(), labels)
        return {"number": issue["number"], "url": issue.get("html_url"), "labels": labels,
                "next": "waits for queue_issue" if hold else "Vibe picks it up within a minute"}

    async def _get_issue(self, number: int) -> dict[str, Any]:
        issue = await self.github.issue(self._repo(), int(number))
        task = Task.from_issue(issue["title"], issue.get("body"))
        return {**_issue_summary(issue), "acceptance_criteria": task.criteria, "skills": task.skills, "ab_attempts": task.ab_attempts}

    async def _add_skill(self, number: int, url: str) -> dict[str, Any]:
        repo = self._repo()
        skill = await check_skill(self.github, url)
        issue = await self.github.issue(repo, int(number))
        task = Task.from_issue(issue["title"], issue.get("body"))
        if skill["url"] not in task.skills:
            task.skills.append(skill["url"])
            await self.github.update_issue(repo, int(number), body=task.body())
        return {"number": int(number), "skill": skill["name"], "description": skill["description"], "skills": task.skills}

    async def _mark_for_ab(self, number: int, attempts: int | None = None) -> dict[str, Any]:
        repo = self._repo()
        issue = await self.github.issue(repo, int(number))
        task = Task.from_issue(issue["title"], issue.get("body"))
        if not task.skills:
            raise ValueError(f"Issue #{number} has no skill: an A/B compares Vibe without and with one. Use add_skill first.")
        task.ab_attempts = max(1, min(8, int(attempts or DEFAULT_AB_ATTEMPTS)))
        fields: dict[str, Any] = {"body": task.body()}
        if issue.get("state") != "open":  # a done issue is closed, and the watcher only looks at open ones
            fields["state"] = "open"
        await self.github.update_issue(repo, int(number), **fields)
        # A run that died may have left its label: asking again is the way out.
        await self.github.relabel(repo, int(number), add=[AB_REQUESTED], remove=[READY, AB_DONE, AB_FAILED, AB_RUNNING])
        return {"number": int(number), "attempts_per_arm": task.ab_attempts, "skills": task.skills}

    async def _queue_issue(self, number: int) -> dict[str, Any]:
        repo = self._repo()
        issue = await self.github.issue(repo, int(number))
        if issue.get("state") != "open":
            await self.github.update_issue(repo, int(number), state="open")
        await self.github.relabel(repo, int(number), add=[READY], remove=[FAILED, NOT_DONE, DONE, AB_REQUESTED, RUNNING])
        return {"number": int(number), "labels": [READY]}


class Triage:
    """One Studio conversation per Slack thread, kept in .state/slack-threads.json or in the given stores."""

    def __init__(self, client: Any, github: Any, start_watcher: StartWatcher | None = None, model: str | None = None, *,
                 threads: Store | None = None, projects: Store | None = None, agents: dict[str, Any] | None = None) -> None:
        self.client, self.github, self.start_watcher = client, github, start_watcher
        self.model = (model or os.environ.get("LECHONK_TRIAGE_MODEL") or "mistral/mistral-large-4").split("/", 1)[-1]
        self.threads = threads if threads is not None else THREADS
        self.projects = projects
        self.agents = agents  # the agent's id and configuration, when a workflow keeps them instead of .state/
        self._locks: dict[str, asyncio.Lock] = {}

    def instructions(self) -> str:
        path = demo_dir() / "task-checklist.md"
        checklist = path.read_text(encoding="utf-8").strip() if path.is_file() else DEFAULT_CHECKLIST
        return INSTRUCTIONS.format(checklist=checklist, ab_attempts=DEFAULT_AB_ATTEMPTS)

    async def agent_id(self) -> str:
        return await ensure_studio_agent(
            self.client, AGENT_NAME, model=self.model, instructions=self.instructions(), tools=TOOLS,
            description="Turns Slack requests into GitHub issues for Vibe (le-chonk demo).", store=self.agents,
        )

    @staticmethod
    def knows(context: SlackContext) -> bool:
        """A thread the agent is part of; its conversation is "" after a failure, to start over there."""
        return THREADS.get(context.key) is not None

    async def handle(self, context: SlackContext, text: str) -> str:
        """Your message in, the agent's reply out, after it ran its tools."""
        lock = self._locks.setdefault(context.key, asyncio.Lock())
        async with lock:
            tools = TriageTools(self.github, context, self.start_watcher, self.projects)
            conversation = self.threads.get(context.key)
            replies: list[str] = []  # what the agent wrote in each round: the last round is its answer
            try:
                if conversation:
                    response = await self.client.beta.conversations.append_async(conversation_id=conversation, inputs=text)
                else:
                    response = await self.client.beta.conversations.start_async(agent_id=await self.agent_id(), inputs=text, store=True)
                    self.threads.set(context.key, response.conversation_id)
                for _ in range(MAX_TOOL_ROUNDS):
                    said = [_message_text(o.content).strip() for o in response.outputs if isinstance(o, models.MessageOutputEntry)]
                    replies.append("\n\n".join(words for words in said if words))
                    calls = [o for o in response.outputs if isinstance(o, models.FunctionCallEntry)]
                    if not calls:
                        break
                    results = [models.FunctionResultEntry(tool_call_id=c.tool_call_id, result=await tools.call(c.name, _arguments(c.arguments)))
                               for c in calls]
                    response = await self.client.beta.conversations.append_async(conversation_id=response.conversation_id, inputs=results)
                else:
                    replies.append("(I stopped after too many steps. Write again and I'll start over.)")
                    self.threads.set(context.key, "")
            except Exception as exc:  # the conversation may be left waiting for tool results: start over next time
                log.exception("triage failed")
                self.threads.set(context.key, "")
                return f"Something went wrong: {type(exc).__name__}: {str(exc)[:300]}. Write again and I'll start over."
            # Its words between tool calls, such as "Let me check the project", aren't for the person
            return next((words for words in reversed(replies) if words), "Done.")
