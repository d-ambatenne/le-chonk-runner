"""The next phase's cloud workflows, on the managed deployment le-chonk-control.

le-chonk-slack (one per channel)
    read_slack          the channel's new messages, and the replies in the threads the agent is part of
    triage_message      the triage agent answers each one: issues through the GitHub connector, the reply in the thread
le-chonk-tasks (one per repo)
    find_tasks          open issues labelled vibe:ready or ab:requested, oldest first
    claim_task          labels, a comment, a message in the thread
    start_vibe          le-chonk-vibe-run or le-chonk-vibe-ab on your Mac's deployment, through the Workflows API
    vibe_status         every minute, then every ten, until your Mac returns the result
    publish_task        the PR, the judge, the merge, labels, comments and the reply; for an A/B, the report's links

Both are on-behalf-of workflows: they run with the connectors and the Mistral account of whoever starts them, so
the deployment holds no token. That needs a hardened deployment, which Studio makes by default. They never
continue-as-new, which drops that identity: when their history grows long, they hand over to a new execution.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

import mistralai.workflows as workflows
from mistralai.workflows.plugins.mistralai.connectors import connector, uses_connectors
from pydantic import BaseModel, Field

from ..demo.workflows import DEFAULT_MODEL, SmokeJudge, _error, _in_workflow, _sleep, _task
from ..workflow.pipeline import _now
from .names import CONTROL, GITHUB, SLACK
from .names import VIBE as VIBE_DEPLOYMENT
from .runs import FINISHED, execution_status, other_generation, slack_watch_id, start_execution, start_watcher, tasks_watch_id, vibe_run_id

github_connector = connector(GITHUB)
slack_connector = connector(SLACK)
MAX_THREADS = 10  # threads read every poll
THREAD_DAYS = 2  # a thread stops being read after this long without a message
THREAD_FAILURES = 3  # a thread that can't be read this many times in a row is dropped
READ_FAILURES_TOLD = 3  # reads of the channel that fail in a row before the watcher says so in the channel
RUN_DAYS = 3  # how long to wait for your Mac
SLOW_POLL_AFTER = timedelta(hours=1)  # then check on the run every ten minutes, so the history stays short
SLOW_POLL_SECONDS = 600
STATUS_ERRORS = 10  # failed status checks in a row before the run counts as lost
HAND_OVER_POLLS = 1000  # a watcher hands over after this many polls, if Workflows hasn't asked it to before


def make_mistral() -> Any:
    """The Mistral API as the person who started the workflow: the agent's conversations, the judge, the Workflows API."""
    from mistralai.workflows.core.auth import ConnectorRunAs
    from mistralai.workflows.plugins.mistralai.utils import get_mistral_client

    return get_mistral_client(run_as=ConnectorRunAs.AUTO)


def make_github() -> Any:
    from .connectors import ConnectorGitHub

    return ConnectorGitHub()


def make_notifier() -> Any:
    from .connectors import ConnectorNotifier

    return ConnectorNotifier()


def control_deployment() -> str:
    return os.environ.get("DEPLOYMENT_NAME") or CONTROL


# --- Inputs ---------------------------------------------------------------------------------------------------

class TaskOptions(BaseModel):
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 3.0
    ab_attempts: int = 3
    smoke: bool = False
    poll_seconds: int = 60
    vibe_deployment: str = VIBE_DEPLOYMENT

    def options(self) -> dict[str, Any]:
        return TaskOptions.model_validate(self.model_dump(include=set(TaskOptions.model_fields))).model_dump()


class WatcherState(TaskOptions):
    generation: str = "a"  # this execution is <base>-a or <base>-b
    fresh: bool = True  # started by a person, not handed over: le-chonk-tasks then frees issues a stopped run left busy
    polls: int = 0
    max_polls: int | None = None
    hand_over_after: int | None = HAND_OVER_POLLS


class TasksInput(WatcherState):
    repo: str
    handled: int = 0


class SlackInput(WatcherState):
    channel: str
    repo: str | None = None
    since: str | None = None  # the newest top-level message seen; None: from the time the watcher starts
    threads: dict[str, dict[str, Any]] = Field(default_factory=dict)  # thread ts: conversation, newest reply seen, failures
    agents: dict[str, Any] = Field(default_factory=dict)  # the triage agent's id and configuration
    read_failures: int = 0  # reads of the channel that failed in a row


class FindInput(BaseModel):
    repo: str


class ClaimInput(BaseModel):
    repo: str
    number: int
    kind: str
    run_id: str
    model: str = DEFAULT_MODEL
    ab_attempts: int = 3


class StartInput(BaseModel):
    kind: str
    execution_id: str
    deployment: str
    params: dict[str, Any]


class StatusInput(BaseModel):
    execution_id: str


class PublishInput(BaseModel):
    repo: str
    number: int
    kind: str
    run_id: str
    result: dict[str, Any]
    smoke: bool = False


class FailInput(BaseModel):
    repo: str
    number: int
    kind: str
    run_id: str
    error: str


class HandOverInput(BaseModel):
    workflow: str
    execution_id: str
    params: dict[str, Any]


class ReadInput(BaseModel):
    channel: str
    since: str
    threads: dict[str, str] = Field(default_factory=dict)  # thread ts: the newest reply seen


class TellInput(BaseModel):
    channel: str
    text: str


class TriageInput(BaseModel):
    channel: str
    thread: str
    user: str | None = None
    text: str
    conversation: str | None = None
    repo: str | None = None
    agents: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


# --- Activities: a repo's tasks ---------------------------------------------------------------------------------

@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=3)
async def find_tasks(params: FindInput) -> list[dict[str, Any]]:
    """Open issues waiting for Vibe, oldest first."""
    from ..demo.issues import work_kind

    issues = await make_github().issues(params.repo, state="open")
    work = [{"number": i["number"], "kind": kind, "title": i["title"]} for i in issues if (kind := work_kind(i))]
    return sorted(work, key=lambda w: w["number"])


@workflows.activity(start_to_close_timeout=timedelta(minutes=10), retry_policy_max_attempts=3)
async def free_stale_tasks(params: FindInput) -> list[int]:
    """Issues a stopped watcher left running: nothing will report them, so they're failed and can be queued again."""
    from ..demo.issues import AB_FAILED, AB_RUNNING, FAILED, RUNNING, label_names

    github = make_github()
    freed = []
    for issue in await github.issues(params.repo, state="open"):
        busy = label_names(issue) & {RUNNING, AB_RUNNING}
        if not busy:
            continue
        await github.comment(params.repo, issue["number"], "The watcher was stopped during this run, so its result won't come. "
                                                           "Ask the triage agent in Slack to queue it again.")
        await github.relabel(params.repo, issue["number"], add=[AB_FAILED if AB_RUNNING in busy else FAILED], remove=sorted(busy))
        freed.append(issue["number"])
    return freed


@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=3)
async def claim_task(params: ClaimInput) -> dict[str, Any]:
    """Mark the issue as taken and say so where people look."""
    from ..demo.issues import AB_REQUESTED, AB_RUNNING, READY, RUNNING

    github = make_github()
    _, task = await _task(github, params.repo, params.number)
    if params.kind == "ab":
        await github.relabel(params.repo, params.number, add=[AB_RUNNING], remove=[AB_REQUESTED])
    else:
        await github.relabel(params.repo, params.number, add=[RUNNING], remove=[READY])
    model = task.model or params.model
    what = (f"the skill A/B, {task.ab_attempts or params.ab_attempts} runs per arm" if params.kind == "ab"
            else "the implementation" + (f" with {len(task.skills)} skill(s)" if task.skills else ""))
    await github.comment(params.repo, params.number, f"Started {what}: run `{params.run_id}`, Vibe on {model}, on your Mac.")
    await make_notifier().post(task.slack_channel, task.slack_thread, f"Started {what} of #{params.number} {task.title} (Vibe on {model}).")
    return {"model": model, "title": task.title}


@workflows.activity(start_to_close_timeout=timedelta(minutes=2), retry_policy_max_attempts=3)
async def start_vibe(params: StartInput) -> str:
    """Start the run on your Mac's deployment. It waits in its queue while the Mac sleeps."""
    workflow = "le-chonk-vibe-ab" if params.kind == "ab" else "le-chonk-vibe-run"
    return await start_execution(make_mistral(), workflow, params.execution_id, params.deployment, params.params)


@workflows.activity(start_to_close_timeout=timedelta(minutes=2), retry_policy_max_attempts=3)
async def vibe_status(params: StatusInput) -> dict[str, Any]:
    return await execution_status(make_mistral(), params.execution_id)


@workflows.activity(start_to_close_timeout=timedelta(minutes=2), retry_policy_max_attempts=3)
async def cancel_vibe(params: StatusInput) -> None:
    await make_mistral().workflows.executions.cancel_workflow_execution_async(execution_id=params.execution_id)


@workflows.activity(start_to_close_timeout=timedelta(minutes=15), retry_policy_max_attempts=2)
async def publish_task(params: PublishInput) -> dict[str, Any]:
    from .publish import merge_run, report_ab

    github = make_github()
    _, task = await _task(github, params.repo, params.number)
    if params.kind == "ab":
        return await report_ab(github, make_notifier(), params.repo, params.number, task, params.run_id, params.result)
    judge_client = SmokeJudge() if params.smoke else make_mistral()
    model = os.environ.get("LECHONK_JUDGE_MODEL") or DEFAULT_MODEL
    return await merge_run(github, make_notifier(), judge_client, model, params.repo, params.number, task, params.run_id, params.result)


@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=3)
async def fail_task(params: FailInput) -> None:
    from ..demo.publish import report_failure

    github = make_github()
    try:
        _, task = await _task(github, params.repo, params.number)
    except Exception:
        task = None
    await report_failure(github, make_notifier(), params.repo, params.number, task, params.kind, params.run_id, params.error)


@workflows.activity(start_to_close_timeout=timedelta(minutes=2), retry_policy_max_attempts=3)
async def hand_over(params: HandOverInput) -> str:
    """Start this watcher's next execution with its state, on behalf of the same person."""
    return await start_execution(make_mistral(), params.workflow, params.execution_id, control_deployment(), params.params)


# --- Activities: a Slack channel --------------------------------------------------------------------------------

@workflows.activity(start_to_close_timeout=timedelta(minutes=3), retry_policy_max_attempts=3)
async def read_slack(params: ReadInput) -> dict[str, Any]:
    """New top-level messages are new requests; new replies continue a thread the agent is in. Its own are skipped."""
    from .connectors import ConnectorSlack, is_person

    slack = ConnectorSlack()
    since = params.since
    messages: list[dict[str, Any]] = []
    for message in await slack.read_channel(params.channel, oldest=params.since):
        since = max(since, message["ts"], key=float)
        if message.get("thread_ts") and message["thread_ts"] != message["ts"]:
            continue  # a reply also sent to the channel: its thread's read finds it
        if is_person(message):
            messages.append({"thread": message["ts"], **message})
    seen, failed, replies_read = dict(params.threads), [], set()
    for thread, newest in params.threads.items():
        try:
            replies = await slack.read_thread(params.channel, thread, oldest=newest)
        except Exception:  # deleted, or rate-limited: the other threads and the channel go on
            failed.append(thread)
            continue
        for reply in replies:
            seen[thread] = max(seen[thread], reply["ts"], key=float)
            replies_read.add(reply["ts"])
            if is_person(reply):
                messages.append({"thread": thread, **reply})
    # Slack's text doesn't say which channel messages are replies also sent to the channel: their thread has them
    messages = [m for m in messages if m["thread"] != m["ts"] or m["ts"] not in replies_read]
    messages.sort(key=lambda m: float(m["ts"]))
    merged: list[dict[str, Any]] = []
    for message in messages:  # one answer written as several messages in a thread is one request
        last = merged[-1] if merged else None
        if last and last["thread"] == message["thread"] != message["ts"] and last.get("user") == message.get("user"):
            last.update(ts=message["ts"], text=f"{last.get('text') or ''}\n\n{message.get('text') or ''}".strip())
        else:
            merged.append(dict(message))
    messages = merged
    return {"since": since, "seen": seen, "failed": failed,
            "messages": [{k: m.get(k) for k in ("thread", "ts", "user", "text")} for m in messages]}


@workflows.activity(start_to_close_timeout=timedelta(minutes=2), retry_policy_max_attempts=3)
async def tell_channel(params: TellInput) -> None:
    """A message to the whole channel, such as that the watcher can't read it."""
    await make_notifier().post(params.channel, None, params.text)


@workflows.activity(start_to_close_timeout=timedelta(minutes=10), retry_policy_max_attempts=2)
async def triage_message(params: TriageInput) -> dict[str, Any]:
    """The triage agent answers one message: its tools use the GitHub connector, its reply goes to the thread."""
    from ..demo.triage import MemoryStore, SlackContext, Triage

    client = make_mistral()
    context = SlackContext(params.channel, params.thread, params.user)
    projects = MemoryStore({params.channel: params.repo} if params.repo else {})
    threads = MemoryStore({context.key: params.conversation} if params.conversation is not None else {})
    agents = dict(params.agents)
    started: dict[str, str] = {}

    async def start_tasks(repo: str) -> str:
        """set_project: start le-chonk-tasks for the repo, on behalf of the same person."""
        started[repo] = await start_watcher(client, "le-chonk-tasks", tasks_watch_id(repo), control_deployment(),
                                            TasksInput(repo=repo, **params.options).model_dump())
        return started[repo]

    triage = Triage(client, make_github(), start_tasks, threads=threads, projects=projects, agents=agents)
    reply = await triage.handle(context, params.text)
    await make_notifier().post(params.channel, params.thread, reply)
    return {"conversation": threads.get(context.key), "repo": projects.get(params.channel), "agents": agents, "started": started}


# --- Workflows ------------------------------------------------------------------------------------------------

def _run_id(item: dict[str, Any]) -> str:
    return f"issue-{item['number']}-{'ab-' if item['kind'] == 'ab' else ''}{_now().strftime('%Y%m%d-%H%M%S')}"


def _time_to_hand_over(params: WatcherState) -> bool:
    if params.hand_over_after is not None and params.polls >= params.hand_over_after:
        return True
    return _in_workflow() and workflows.workflow.should_continue_as_new()


async def _hand_over(workflow: str, base: str, params: WatcherState) -> str | None:
    """Start the other execution of this watcher with its state. None if that failed: this one goes on."""
    successor = other_generation(params.generation)
    state = {**params.model_dump(), "generation": successor, "fresh": False, "polls": 0}
    try:
        return await hand_over(HandOverInput(workflow=workflow, execution_id=f"{base}-{successor}", params=state))
    except Exception:
        return None


@workflows.workflow.define(
    name="le-chonk-tasks",
    workflow_display_name="Le Chonk: run a repo's tasks on your Mac",
    workflow_description="Watches a repo's issues through the GitHub connector, runs each on your Mac with Vibe, then makes the PR, judges and merges it.",
    execution_timeout=timedelta(days=365),
    search_keys=["repo"],
    on_behalf_of=True,
)
@uses_connectors(github_connector, slack_connector)
class TasksWatcher:
    @workflows.workflow.entrypoint
    async def run(self, params: TasksInput) -> dict[str, Any]:
        handled: list[dict[str, Any]] = []
        if params.fresh:
            try:
                freed = await free_stale_tasks(FindInput(repo=params.repo))
                if freed:
                    handled.append({"freed": freed})
            except Exception as exc:
                handled.append({"error": _error(exc)})
            params.fresh = False
        while True:
            try:
                work = await find_tasks(FindInput(repo=params.repo))
            except Exception as exc:  # GitHub down, a sign-in expired: try again later
                handled.append({"error": _error(exc)})
                work = []
            if work:
                item = work[0]
                run_id = _run_id(item)
                try:
                    result = await self.task(params, item, run_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # even the failure report failed: the issue may stay busy, the watcher goes on
                    result = {"ok": False, "error": _error(exc)}
                handled.append({"number": item["number"], "kind": item["kind"], "run_id": run_id, "result": result})
                params.handled += 1
                if result.get("ok") is False:
                    await _sleep(params.poll_seconds)  # so an issue that keeps failing can't spin the loop
                continue
            params.polls += 1
            if params.max_polls is not None and params.polls >= params.max_polls:
                return {"repo": params.repo, "polls": params.polls, "handled": handled}
            if _time_to_hand_over(params):
                successor = await _hand_over("le-chonk-tasks", tasks_watch_id(params.repo), params)
                if successor:
                    return {"repo": params.repo, "handed_over": successor, "handled": handled}
            await _sleep(params.poll_seconds)

    async def task(self, params: TasksInput, item: dict[str, Any], run_id: str) -> dict[str, Any]:
        kind, number = item["kind"], item["number"]
        execution = vibe_run_id(params.repo, run_id)
        started = False
        try:
            claimed = await claim_task(ClaimInput(repo=params.repo, number=number, kind=kind, run_id=run_id, model=params.model,
                                                  ab_attempts=params.ab_attempts))
            run = {"repo": params.repo, "number": number, "run_id": run_id, "model": claimed["model"], "max_cost_usd": params.max_cost_usd,
                   "ab_attempts": params.ab_attempts, "smoke": params.smoke}
            await start_vibe(StartInput(kind=kind, execution_id=execution, deployment=params.vibe_deployment, params=run))
            started = True
            result = await self.wait(params, execution)
            if result.get("ok") is False:
                error = str(result.get("error") or "the run failed")
            else:
                return await publish_task(PublishInput(repo=params.repo, number=number, kind=kind, run_id=run_id, result=result,
                                                       smoke=params.smoke))
        except asyncio.CancelledError:  # `le-chonk next stop`: stop the Mac's run too, and free the issue
            if started:
                try:
                    await cancel_vibe(StatusInput(execution_id=execution))
                except Exception:
                    pass
            await fail_task(FailInput(repo=params.repo, number=number, kind=kind, run_id=run_id, error="the watcher was stopped"))
            raise
        except Exception as exc:
            error = _error(exc)
        await fail_task(FailInput(repo=params.repo, number=number, kind=kind, run_id=run_id, error=error))
        return {"ok": False, "error": error}

    @staticmethod
    async def wait(params: TasksInput, execution: str) -> dict[str, Any]:
        """The run's result from your Mac: every poll for an hour, then every ten minutes, up to the deadline."""
        began = _now()
        errors = 0
        while True:
            slow = _now() - began > SLOW_POLL_AFTER
            await _sleep(max(params.poll_seconds, SLOW_POLL_SECONDS) if slow else params.poll_seconds)
            try:
                status = await vibe_status(StatusInput(execution_id=execution))
            except Exception as exc:  # an API blip: the run goes on, so keep checking
                errors += 1
                if errors >= STATUS_ERRORS:
                    return {"ok": False, "error": f"lost track of the run on your Mac: {_error(exc)}"}
                continue
            errors = 0
            if status["status"] == "COMPLETED":
                return status["result"] if isinstance(status["result"], dict) else {"ok": False, "error": f"unexpected result: {status['result']!r:.300}"}
            if status["status"] in FINISHED:
                return {"ok": False, "error": f"the run on your Mac ended: {status['status'].lower()}"}
            if _now() - began > timedelta(days=RUN_DAYS):
                return {"ok": False, "error": f"no result after {RUN_DAYS} days: is `uv run le-chonk next worker` running on your Mac?"}


@workflows.workflow.define(
    name="le-chonk-slack",
    workflow_display_name="Le Chonk: a Slack channel's tasks",
    workflow_description="Reads a Slack channel through the Slack connector; the triage agent turns its messages into GitHub issues and replies in the thread.",
    execution_timeout=timedelta(days=365),
    search_keys=["channel"],
    on_behalf_of=True,
)
@uses_connectors(github_connector, slack_connector)
class SlackWatcher:
    @workflows.workflow.entrypoint
    async def run(self, params: SlackInput) -> dict[str, Any]:
        answered: list[dict[str, Any]] = []
        if params.since is None:  # start with the messages that come after this
            params.since = f"{_now().timestamp():.6f}"
        while True:
            try:
                read = await read_slack(ReadInput(channel=params.channel, since=params.since,
                                                  threads={t: s["seen"] for t, s in params.threads.items()}))
            except Exception as exc:  # Slack down, a sign-in expired: try again later
                answered.append({"error": _error(exc)})
                params.read_failures += 1
                if params.read_failures == READ_FAILURES_TOLD:  # once, not every minute
                    await self.tell(params.channel, f"I can't read this channel, so I don't answer here for now: {_error(exc)[:300]}. "
                                                    "I keep trying every minute.")
                read = {"since": params.since, "seen": {}, "failed": [], "messages": []}
            else:
                if params.read_failures >= READ_FAILURES_TOLD:
                    await self.tell(params.channel, "I can read this channel again. Write again what I missed.")
                params.read_failures = 0
            params.since = read["since"]
            for thread, state in params.threads.items():
                if thread in read["seen"]:
                    state["seen"] = read["seen"][thread]
                state["failures"] = state.get("failures", 0) + 1 if thread in read["failed"] else 0
            for message in read["messages"]:
                state = params.threads.get(message["thread"]) or {}
                try:
                    out = await triage_message(TriageInput(channel=params.channel, thread=message["thread"], user=message.get("user"),
                                                           text=message.get("text") or "", conversation=state.get("conversation"),
                                                           repo=params.repo, agents=params.agents, options=params.options()))
                except Exception as exc:
                    answered.append({"ts": message["ts"], "error": _error(exc)})
                    continue
                params.threads[message["thread"]] = {"conversation": out["conversation"], "seen": max(state.get("seen") or "0", message["ts"], key=float),
                                                     "active": message["ts"], "failures": 0}
                params.repo = out.get("repo") or params.repo
                params.agents = out.get("agents") or params.agents
                answered.append({"ts": message["ts"], "thread": message["thread"], "started": out.get("started") or {}})
            self.forget_threads(params)
            params.polls += 1
            if params.max_polls is not None and params.polls >= params.max_polls:
                return {"channel": params.channel, "repo": params.repo, "polls": params.polls, "answered": answered,
                        "threads": params.threads}
            if _time_to_hand_over(params):
                successor = await _hand_over("le-chonk-slack", slack_watch_id(params.channel), params)
                if successor:
                    return {"channel": params.channel, "handed_over": successor, "answered": answered}
            await _sleep(params.poll_seconds)

    @staticmethod
    async def tell(channel: str, text: str) -> None:
        try:
            await tell_channel(TellInput(channel=channel, text=text))
        except Exception:  # the channel can't be written to either: the run's history keeps the errors
            pass

    @staticmethod
    def forget_threads(params: SlackInput) -> None:
        """Keep reading the threads with recent messages only, a few, and none that keeps failing."""
        oldest = _now().timestamp() - THREAD_DAYS * 86400
        recent = sorted(((t, s) for t, s in params.threads.items()
                         if float(s.get("active") or t) >= oldest and s.get("failures", 0) < THREAD_FAILURES),
                        key=lambda item: float(item[1].get("active") or item[0]), reverse=True)
        params.threads = dict(recent[:MAX_THREADS])


WORKFLOWS = [SlackWatcher, TasksWatcher]
