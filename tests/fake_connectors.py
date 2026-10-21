"""Studio's GitHub and Slack connectors in memory, for the next phase's tests.

FakeGitHubConnector answers the GitHub MCP server's tools from FakeGitHub, so the cloud's connector calls and
the Mac's REST calls see the same issues, PRs, releases and git remotes. FakeSlackConnector keeps a channel's
messages and threads. FakeMistral is the scripted conversation client plus the Workflows API: a run started on
the Mac's deployment runs here, in this process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
from mistralai.workflows.plugins.mistralai.connectors.exceptions import ConnectorToolCallError

from fake_github import FakeGitHub
from fakes import FakeClient

ME = "U0DENIS"  # Slack's connector posts as you


def text(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value if isinstance(value, str) else json.dumps(value)}]}


class FakeGitHubConnector:
    def __init__(self, fake: FakeGitHub) -> None:
        self.fake = fake
        self.http = httpx.Client(transport=fake.transport(), base_url="https://api.github.com")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.unindexed: set[str] = set()  # repos made here: GitHub's search finds them only later

    def _rest(self, method: str, path: str, **kwargs: Any) -> Any:
        """As the real connector fails: the SDK's ConnectorToolCallError, with the MCP server's text in its details."""
        response = self.http.request(method, path, **kwargs)
        if response.status_code >= 400:
            message = f"failed: {method} https://api.github.com{path}: {response.status_code} {response.json().get('message')} []"
            raise ConnectorToolCallError("github_app", response=text(message))
        return response.json()

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        self.calls.append((tool, args))
        handler = getattr(self, f"_{tool}", None)
        if handler is None:
            raise RuntimeError(f"no fake for the tool {tool}")
        return handler(**args)

    @staticmethod
    def _minimal(item: dict[str, Any]) -> dict[str, Any]:
        return text({"id": str(item["number"] * 1000), "url": item["html_url"]})

    def _list_issues(self, owner: str, repo: str, state: str | None = None, perPage: int = 30, after: str | None = None,  # noqa: N803
                     **_: Any) -> dict[str, Any]:
        items = self._rest("GET", f"/repos/{owner}/{repo}/issues", params={"state": (state or "all").lower()})
        issues = [{"number": i["number"], "title": i["title"], "body": i["body"], "state": i["state"].upper(),
                   "labels": [label["name"] for label in i["labels"]], "url": i["html_url"]} for i in items]
        start = int(after or 0)
        page = issues[start:start + perPage]
        more = start + perPage < len(issues)
        return text({"issues": page, "pageInfo": {"hasNextPage": more, "endCursor": str(start + perPage) if more else None},
                     "totalCount": len(issues)})

    def _issue_read(self, method: str, owner: str, repo: str, issue_number: int, **_: Any) -> dict[str, Any]:
        return text(self._rest("GET", f"/repos/{owner}/{repo}/issues/{issue_number}"))

    def _issue_write(self, method: str, owner: str, repo: str, issue_number: int | None = None, **fields: Any) -> dict[str, Any]:
        body = {k: v for k, v in fields.items() if k in ("title", "body", "state", "labels")}
        if method == "create":
            return self._minimal(self._rest("POST", f"/repos/{owner}/{repo}/issues", json=body))
        return self._minimal(self._rest("PATCH", f"/repos/{owner}/{repo}/issues/{issue_number}", json=body))

    def _add_issue_comment(self, owner: str, repo: str, issue_number: int, body: str, **_: Any) -> dict[str, Any]:
        self._rest("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", json={"body": body})
        return text({"id": "1", "url": f"https://github.com/{owner}/{repo}/issues/{issue_number}#issuecomment-1"})

    def _search_issues(self, query: str, owner: str, repo: str, **_: Any) -> dict[str, Any]:
        return text(self._rest("GET", "/search/issues", params={"q": f"repo:{owner}/{repo} {query}"}))

    def _search_repositories(self, query: str, **_: Any) -> dict[str, Any]:
        full = query.split("repo:", 1)[-1].strip()
        info = None if full in self.unindexed else self.fake.repos.get(full)
        return text({"total_count": 1 if info else 0, "items": [info] if info else []})

    def _list_branches(self, owner: str, repo: str, **_: Any) -> dict[str, Any]:
        self._rest("GET", f"/repos/{owner}/{repo}")  # a 404 when it isn't there
        return text([])  # a new, empty repo has no branch yet

    def _create_repository(self, name: str, description: str | None = None, private: bool = False, autoInit: bool = False,  # noqa: N803
                           **_: Any) -> dict[str, Any]:
        made = self._rest("POST", "/user/repos", json={"name": name, "description": description or "", "private": private, "auto_init": autoInit})
        self.unindexed.add(made["full_name"])
        return text({"id": "77", "url": made["html_url"]})

    def _get_file_contents(self, owner: str, repo: str, path: str, ref: str | None = None, sha: str | None = None,
                           **_: Any) -> dict[str, Any]:
        branch = sha or (ref or "refs/heads/main").removeprefix("refs/heads/").removeprefix("refs/tags/")
        data = self._rest("GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": branch})
        content = base64.b64decode(data["content"]).decode()
        return {"content": [{"type": "text", "text": "successfully downloaded text file"},
                            {"type": "resource", "resource": {"uri": f"repo://{owner}/{repo}/{path}", "mimeType": "text/markdown", "text": content}}]}

    def _create_pull_request(self, owner: str, repo: str, title: str, head: str, base: str, body: str = "", **_: Any) -> dict[str, Any]:
        return self._minimal(self._rest("POST", f"/repos/{owner}/{repo}/pulls", json={"title": title, "head": head, "base": base, "body": body}))

    def _rest_pull(self, pull: dict[str, Any]) -> dict[str, Any]:
        return {**pull, "head": {"ref": pull["head"]}, "base": {"ref": pull["base"]}, "state": "closed" if pull["merged"] else "open"}

    def _list_pull_requests(self, owner: str, repo: str, head: str | None = None, **_: Any) -> dict[str, Any]:
        return text([self._rest_pull(p) for p in self._rest("GET", f"/repos/{owner}/{repo}/pulls", params={"head": head or ""})])

    def _pull_request_read(self, method: str, owner: str, repo: str, pullNumber: int, **_: Any) -> dict[str, Any]:  # noqa: N803
        pull = next(p for p in self.fake.pulls[f"{owner}/{repo}"] if p["number"] == int(pullNumber))
        if method == "get":
            return text(self._rest_pull(pull))
        bare = str(self.fake.remotes / f"{owner}/{repo}.git")
        base = pull["merge_commit_sha"] + "^" if pull["merged"] else pull["base"]
        diff = subprocess.run(["git", "--git-dir", bare, "diff", base, pull["merge_commit_sha"] if pull["merged"] else pull["head"]],
                              capture_output=True, text=True, check=True).stdout
        return text(diff)

    def _merge_pull_request(self, owner: str, repo: str, pullNumber: int, merge_method: str = "merge", **_: Any) -> dict[str, Any]:  # noqa: N803
        merged = self._rest("PUT", f"/repos/{owner}/{repo}/pulls/{pullNumber}/merge", json={"merge_method": merge_method})
        return text({**merged, "message": "Pull Request successfully merged"})


class FakeSlackConnector:
    """Slack's MCP server in memory: messages kept as Slack's API has them, answered in the server's text formats,
    as scripts/check_next.py saw them."""

    def __init__(self) -> None:
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.channels = {"C0LECHONK1": ("le-chonk", "The demo."), "C0LECHONK2": ("le-chonk-old", "Moved to #le-chonk.")}
        self.people = {ME: "Denis Example", "U2": "Ana Example"}
        self._clock = 1_800_000_000

    def _ts(self) -> str:
        self._clock += 1
        return f"{self._clock}.000100"

    def say(self, channel: str, words: str, thread: str | None = None, user: str = ME, broadcast: bool = False) -> str:
        ts = self._ts()
        message = {"ts": ts, "user": user, "text": words, **({"thread_ts": thread} if thread else {})}
        if broadcast:  # a reply also sent to the channel
            message["subtype"] = "thread_broadcast"
        self.messages.setdefault(channel, []).append(message)
        return ts

    def thread(self, channel: str, thread: str) -> list[dict[str, Any]]:
        return [m for m in self.messages.get(channel, []) if m.get("thread_ts") == thread]

    def _who(self, user: str) -> str:
        name = self.people.get(user, user)
        return f"{name} <{name.split()[0].lower()}@example.com> ({user})"

    @staticmethod
    def _when(ts: str) -> str:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _block(self, message: dict[str, Any], channel: str) -> str:
        block = f"=== Message from {self._who(message['user'])} at {self._when(message['ts'])} === \nMessage TS: {message['ts']}\n{message['text']}"
        if replies := self.thread(channel, message["ts"]):
            block += f"\nThread: {len(replies)} {'reply' if len(replies) == 1 else 'replies'} (latest: {self._when(replies[-1]['ts'])})"
        return block

    def _reply(self, message: dict[str, Any], n: int, total: int) -> str:
        return (f"--- Reply {n} of {total} ---\nFrom: {self._who(message['user'])}\nTime: {self._when(message['ts'])}\n"
                f"Message TS: {message['ts']}\n{message['text']}")

    def _read_channel(self, channel: str, oldest: float, limit: int) -> dict[str, Any]:
        top = [m for m in self.messages.get(channel, []) if m.get("thread_ts") in (None, m["ts"]) or m.get("subtype") == "thread_broadcast"]
        newest_first = sorted((m for m in top if float(m["ts"]) > oldest), key=lambda m: -float(m["ts"]))[:limit]
        name = self.channels.get(channel, (channel.lower(), ""))[0]
        listing = f"Channel: #{name} ({channel})\n\n" + "\n\n".join(self._block(m, channel) for m in newest_first)
        return text({"messages": listing, "pagination_info": "There are no more messages available.\n"})

    def _read_thread(self, channel: str, parent_ts: str, oldest: float) -> dict[str, Any]:
        parent = next(m for m in self.messages.get(channel, []) if m["ts"] == parent_ts)
        replies = [m for m in self.thread(channel, parent_ts) if float(m["ts"]) > oldest]
        listing = (f"=== THREAD PARENT MESSAGE ===\nFrom: {self._who(parent['user'])}\nTime: {self._when(parent['ts'])}\n"
                   f"Message TS: {parent['ts']}\n{parent['text']}\n\n")
        if replies:
            listing += f"=== THREAD REPLIES ({len(replies)} total) ===\n\n" + "\n\n".join(
                self._reply(m, n, len(replies)) for n, m in enumerate(replies, 1)) + "\n"
        else:
            listing += "No thread messsages\n"
        return text({"messages": listing, "pagination_info": "There are no more messages in this thread.\n"})

    def _search_channels(self, keywords: list[str]) -> dict[str, Any]:
        found = [(cid, name, purpose) for cid, (name, purpose) in self.channels.items() if all(k.lower() in name for k in keywords)]
        listing = f"# Search Results for: \n\n## Channels ({len(found)} results)\n"
        for i, (cid, name, purpose) in enumerate(found, 1):
            listing += (f"### Result {i} of {len(found)}\nName: #{name}\nCreator: Denis Example (<@{ME})\nCreated: 2026-10-21 14:12:23 UTC\n"
                        f"Purpose: {purpose}\nPermalink: [link](https://example.slack.com/archives/{cid})\nIs Archived: false\n"
                        "Channel Type: public_channel\n\n---\n\n")
        return text({"results": listing, "pagination_info": "End of results - No more pages available.\\n"})

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        self.calls.append((tool, args))
        channel = args.get("channel_id")
        oldest = float(args.get("oldest") or 0)
        if tool == "slack_read_channel":
            return self._read_channel(channel, oldest, int(args.get("limit") or 100))
        if tool == "slack_read_thread":
            return self._read_thread(channel, args["message_ts"], oldest)
        if tool == "slack_send_message":  # what the real tool returns isn't read
            ts = self.say(channel, args["message"], args.get("thread_ts"))
            return text(f"Message sent: https://example.slack.com/archives/{channel}/p{ts.replace('.', '')}")
        if tool == "slack_search_channels":
            return self._search_channels(args.get("keywords") or [])
        raise RuntimeError(f"no fake for the tool {tool}")


class FakeMistral(FakeClient):
    """The scripted conversation client, plus the Workflows API: a run started on a deployment runs in this process."""

    def __init__(self, script: list[list[Any]] | None = None, workflows_by_name: dict[str, Any] | None = None) -> None:
        super().__init__(script or [])
        self.workflows_by_name = workflows_by_name or {}
        self.started: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.runs: dict[str, asyncio.Task] = {}
        self.status_override: str | None = None
        self.status_errors = 0  # the next status checks that fail
        self.running: set[str] = set()  # executions started here without code to run: the watchers
        self.workflows = SimpleNamespace(
            execute_workflow_async=self._execute,
            executions=SimpleNamespace(get_workflow_execution_async=self._get, cancel_workflow_execution_async=self._cancel),
        )

    async def _execute(self, **kwargs: Any) -> Any:
        self.started.append(kwargs)
        workflow = self.workflows_by_name.get(kwargs["workflow_identifier"])
        if workflow is not None:
            import mistralai.workflows as workflows

            cls, model = workflow
            self.runs[kwargs["execution_id"]] = asyncio.create_task(workflows.execute_workflow(cls, model(**kwargs["input"])))
        else:
            self.running.add(kwargs["execution_id"])
        return SimpleNamespace(execution_id=kwargs["execution_id"])

    async def _get(self, execution_id: str) -> Any:
        if self.status_override:
            return SimpleNamespace(status=self.status_override, result=None)
        if execution_id in self.running:
            return SimpleNamespace(status="WorkflowExecutionStatus.RUNNING", result=None)
        run = self.runs.get(execution_id)
        if run is None:
            raise RuntimeError(f"404 execution {execution_id} not found")
        if self.status_errors:
            self.status_errors -= 1
            raise RuntimeError("503 Service Unavailable")
        # As the SDK stores a workflow's dict result: {"result": ...}
        return SimpleNamespace(status="WorkflowExecutionStatus.COMPLETED", result={"result": await run})

    async def _cancel(self, execution_id: str) -> None:
        self.cancelled.append(execution_id)
        self.running.discard(execution_id)
