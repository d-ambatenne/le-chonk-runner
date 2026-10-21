"""GitHub and Slack through Studio's connectors: what the cloud workflows use instead of tokens.

A workflow activity calls a connector's tool, and Mistral runs it with the sign-in of the person who started the
workflow (an on-behalf-of workflow). The tools are those of GitHub's and Slack's MCP servers as Studio lists
them: `uv run python scripts/check_next.py` saves their definitions. Results come back as MCP content blocks:
GitHub's are JSON text, Slack's are text.

ConnectorGitHub has the methods of the demo's REST client (demo/github.py) that the triage tools and the
publishing steps call, so the same code runs with either.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..demo.github import GitHubError, parse_repo
from .names import GITHUB, SLACK

log = logging.getLogger(__name__)

__all__ = ["GITHUB", "SLACK", "ConnectorError", "ConnectorGitHub", "ConnectorNotifier", "ConnectorSlack", "is_person", "parse_channels",
           "tool_error"]
# The agent's Slack messages start with this. They post as you, so the watcher skips its own messages by it.
MARK = "🤖"
MARKS = (MARK, ":robot_face:")


def make_connector(name: str) -> Any:
    """A client for one connector, for calls from a workflow activity. Tests replace it."""
    from mistralai.workflows.plugins.mistralai.connectors import ToolCallClient

    return ToolCallClient(name)


# --- Results ----------------------------------------------------------------------------------------------------

def _blocks(result: Any) -> list[dict[str, Any]]:
    data = result.model_dump() if hasattr(result, "model_dump") else result
    if isinstance(data, str):
        return [{"type": "text", "text": data}]
    blocks = data.get("content") if isinstance(data, dict) else None
    return [b for b in blocks or [] if isinstance(b, dict)]


def content_text(result: Any) -> str:
    """The text blocks of a tool's result, joined."""
    return "\n".join(b["text"] for b in _blocks(result) if isinstance(b.get("text"), str))


def resource_text(result: Any) -> str | None:
    """The text of an embedded resource, such as a file's content."""
    for block in _blocks(result):
        resource = block.get("resource")
        if isinstance(resource, dict) and isinstance(resource.get("text"), str):
            return resource["text"]
    return None


def content_json(result: Any) -> Any:
    text = content_text(result).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class ConnectorError(RuntimeError):
    """A tool's error, with its text. Unlike the SDK's ConnectorToolCallError, an activity retries it."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def tool_error(connector: str, tool: str, exc: Exception) -> ConnectorError:
    """The SDK's error says only "connector tool call failed": the tool's own text is in its details."""
    details = [d for d in getattr(exc, "details", None) or () if d is not None]
    text = " ".join(content_text(d) for d in details).strip() or str(exc)
    status = re.search(r"\b([45]\d\d)\b(?=\s+[A-Z]|\s*[:\]])", text)
    code = int(status.group(1)) if status else (404 if re.search(r"not[ _]found", text, re.I) else 502)
    error = GitHubError(code, f"{tool}: {text[:500]}") if connector == GITHUB else ConnectorError(code, f"Slack {tool}: {text[:500]}")
    return error


# --- GitHub -----------------------------------------------------------------------------------------------------

def _html_url(item: dict[str, Any], repo: str, kind: str, number: int | None) -> str | None:
    url = item.get("html_url") or item.get("url")
    if isinstance(url, str) and url.startswith("https://api.github.com/repos/"):
        url = url.replace("https://api.github.com/repos/", "https://github.com/").replace("/pulls/", "/pull/")
    if not url and number:
        url = f"https://github.com/{repo}/{kind}/{number}"
    return url


def _number(data: Any) -> int | None:
    """The number of an issue or PR from a full or a minimal result ({"id", "url"})."""
    if isinstance(data, dict):
        if data.get("number"):
            return int(data["number"])
        match = re.search(r"/(?:issues|pull|pulls)/(\d+)", str(data.get("html_url") or data.get("url") or ""))
        if match:
            return int(match.group(1))
    return None


def label_list(labels: Any) -> list[str]:
    if isinstance(labels, dict):
        labels = labels.get("nodes") or labels.get("labels") or []
    return [str(label.get("name") if isinstance(label, dict) else label) for label in labels or [] if label]


def normalize_issue(item: dict[str, Any], repo: str) -> dict[str, Any]:
    """An issue in the REST shape the demo's code reads, from the MCP server's REST or GraphQL shape."""
    number = _number(item) or 0
    issue = {
        "number": number,
        "title": item.get("title") or "",
        "body": item.get("body") or "",
        "state": str(item.get("state") or "open").lower(),
        "labels": [{"name": name} for name in label_list(item.get("labels"))],
        "html_url": _html_url(item, repo, "issues", number),
    }
    if item.get("pull_request"):
        issue["pull_request"] = item["pull_request"]
    return issue


def normalize_pull(item: dict[str, Any], repo: str) -> dict[str, Any]:
    number = _number(item) or 0
    head = item.get("head")
    return {
        "number": number,
        "html_url": _html_url(item, repo, "pull", number),
        "head": head.get("ref") if isinstance(head, dict) else head,
        "merged": bool(item.get("merged") or item.get("merged_at")),
        "merge_commit_sha": item.get("merge_commit_sha"),
        "state": item.get("state"),
    }


def _items(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return [i for i in data[key] if isinstance(i, dict)]
    return []


class ConnectorGitHub:
    """The demo's GitHub client, made with the GitHub connector instead of a token."""

    token = None  # git can't use a connector: pushing stays on the Mac

    def __init__(self, connector: Any | None = None) -> None:
        self.connector = connector if connector is not None else make_connector(GITHUB)

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> ConnectorGitHub:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def call(self, tool: str, **arguments: Any) -> Any:
        try:
            return await self.connector.call_tool(tool, {k: v for k, v in arguments.items() if v is not None})
        except Exception as exc:
            raise tool_error(GITHUB, tool, exc) from exc

    async def call_json(self, tool: str, **arguments: Any) -> Any:
        result = await self.call(tool, **arguments)
        data = content_json(result)
        if data is None:
            text = content_text(result)
            if re.search(r"\b404\b|not[ _]found", text, re.I):
                raise GitHubError(404, f"{tool}: {text[:300]}")
            return {"text": text}
        return data

    @staticmethod
    def _split(repo: str) -> tuple[str, str]:
        owner, name = parse_repo(repo).split("/", 1)
        return owner, name

    # Repos and files

    async def repo(self, repo: str) -> dict[str, Any]:
        """There's no tool to read a repository. A search by its full name finds it, a private one too; a new one
        isn't in the search index yet, so listing its branches then says it's there."""
        full = parse_repo(repo)
        owner, name = full.split("/", 1)
        data = await self.call_json("search_repositories", query=f"repo:{full}")
        found = next((i for i in _items(data, "items", "repositories") if str(i.get("full_name", "")).lower() == full.lower()), None)
        if found is None:
            try:
                await self.call_json("list_branches", owner=owner, repo=name, perPage=1)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError(404, f"{full} isn't there, or your GitHub connector can't see it") from exc
                raise
            found = {}
        return {"full_name": full, "name": name, "default_branch": found.get("default_branch") or "main",
                "html_url": found.get("html_url") or f"https://github.com/{full}"}

    async def create_repo(self, name: str, description: str = "") -> dict[str, Any]:
        """An empty private repository in the account the connector signs in with."""
        data = await self.call_json("create_repository", name=name, description=description or None, private=True, autoInit=False)
        url = str(data.get("html_url") or data.get("url") or "") if isinstance(data, dict) else ""
        match = re.search(r"github\.com/([^/]+/[^/?#]+)", url)
        full = str(data.get("full_name") or "") if isinstance(data, dict) else ""
        full = full or (match.group(1) if match else "")
        if not full:
            raise GitHubError(502, f"create_repository didn't say which repository it made: {str(data)[:300]}")
        return {"full_name": full, "html_url": f"https://github.com/{full}"}

    async def file_text(self, repo: str, path: str, ref: str | None = None) -> str | None:
        """A file's text at a branch, a tag or a commit; None when it isn't there."""
        owner, name = self._split(repo)
        if ref and re.fullmatch(r"[0-9a-f]{40}", ref):
            tries = [{"sha": ref}]
        elif ref and not ref.startswith("refs/"):
            tries = [{"ref": f"refs/heads/{ref}"}, {"ref": f"refs/tags/{ref}"}]
        else:
            tries = [{"ref": ref}]
        for where in tries:
            try:
                result = await self.call("get_file_contents", owner=owner, repo=name, path=path, **where)
            except GitHubError as exc:
                if exc.status == 404:
                    continue
                raise
            text = resource_text(result)
            if text is not None:
                return text
        return None

    async def ensure_labels(self, repo: str) -> None:
        """No tool makes labels: GitHub makes one the first time it's set. `le-chonk next start` colors them."""

    # Issues

    async def issues(self, repo: str, state: str = "open", labels: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        owner, name = self._split(repo)
        found: list[dict[str, Any]] = []
        after = None
        while len(found) < limit:
            data = await self.call_json("list_issues", owner=owner, repo=name, state=None if state == "all" else state.upper(),
                                        labels=[labels] if labels else None, orderBy="CREATED_AT", direction="ASC", perPage=100, after=after)
            found += [normalize_issue(i, repo) for i in _items(data, "issues", "items")]
            page = data.get("pageInfo") if isinstance(data, dict) else None
            if not (isinstance(page, dict) and page.get("hasNextPage") and page.get("endCursor")):
                break
            after = page["endCursor"]
        return found[:limit]

    async def issue(self, repo: str, number: int) -> dict[str, Any]:
        owner, name = self._split(repo)
        return normalize_issue(await self.call_json("issue_read", method="get", owner=owner, repo=name, issue_number=int(number)), repo)

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        owner, name = self._split(repo)
        data = await self.call_json("issue_write", method="create", owner=owner, repo=name, title=title, body=body, labels=labels or None)
        number = _number(data)
        if number is None:
            raise GitHubError(502, f"issue_write didn't say which issue it made: {str(data)[:300]}")
        return {"number": number, "title": title, "html_url": _html_url(data, repo, "issues", number), "labels": [{"name": n} for n in labels]}

    async def update_issue(self, repo: str, number: int, **fields: Any) -> dict[str, Any]:
        owner, name = self._split(repo)
        known = {k: v for k, v in fields.items() if k in ("title", "body", "state", "state_reason", "labels")}
        return await self.call_json("issue_write", method="update", owner=owner, repo=name, issue_number=int(number), **known)

    async def relabel(self, repo: str, number: int, add: list[str] | None = None, remove: list[str] | None = None) -> None:
        """issue_write replaces the labels: read them, then write the new set. Each step adds one, so it's never empty."""
        current = [label["name"] for label in (await self.issue(repo, number))["labels"]]
        wanted = [n for n in current if n not in (remove or [])] + [n for n in add or [] if n not in current]
        if wanted != current:
            await self.update_issue(repo, number, labels=wanted)

    async def comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        owner, name = self._split(repo)
        return await self.call_json("add_issue_comment", owner=owner, repo=name, issue_number=int(number), body=body)

    async def search_issues(self, repo: str, words: str, limit: int = 20) -> list[dict[str, Any]]:
        owner, name = self._split(repo)
        data = await self.call_json("search_issues", query=words, owner=owner, repo=name, perPage=limit)
        return [normalize_issue(i, repo) for i in _items(data, "items", "issues")]

    # Pull requests

    async def create_pull(self, repo: str, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        owner, name = self._split(repo)
        data = await self.call_json("create_pull_request", owner=owner, repo=name, title=title, head=head, base=base, body=body)
        number = _number(data)
        if number is None:
            raise GitHubError(502, f"create_pull_request didn't say which PR it made: {str(data)[:300]}")
        return {"number": number, "html_url": _html_url(data, repo, "pull", number), "head": head, "merged": False}

    async def find_pull(self, repo: str, head: str) -> dict[str, Any] | None:
        owner, name = self._split(repo)
        data = await self.call_json("list_pull_requests", owner=owner, repo=name, head=f"{owner}:{head}", state="all", perPage=20)
        for item in _items(data, "pull_requests", "items"):
            pull = normalize_pull(item, repo)
            if pull["head"] in (head, f"{owner}:{head}"):
                return pull
        return None

    async def pull(self, repo: str, number: int) -> dict[str, Any]:
        owner, name = self._split(repo)
        return normalize_pull(await self.call_json("pull_request_read", method="get", owner=owner, repo=name, pullNumber=int(number)), repo)

    async def pull_diff(self, repo: str, number: int) -> str:
        owner, name = self._split(repo)
        return content_text(await self.call("pull_request_read", method="get_diff", owner=owner, repo=name, pullNumber=int(number)))

    async def merge_pull(self, repo: str, number: int, title: str) -> dict[str, Any]:
        owner, name = self._split(repo)
        data = await self.call_json("merge_pull_request", owner=owner, repo=name, pullNumber=int(number), merge_method="squash",
                                    commit_title=title)
        return data if isinstance(data, dict) else {}


def diff_summary(diff: str, max_chars: int) -> dict[str, Any]:
    """A PR's diff in the shape the judge reads: the files, a stat line and the diff, cut to size."""
    files = re.findall(r"(?m)^diff --git a/(\S+) b/", diff)
    added = len(re.findall(r"(?m)^\+(?!\+\+)", diff))
    removed = len(re.findall(r"(?m)^-(?!--)", diff))
    stat = f" {len(files)} files changed, {added} insertions(+), {removed} deletions(-)" if files else ""
    return {"files": files, "stat": stat, "diff": diff[:max_chars], "truncated": len(diff) > max_chars}


# --- Slack ------------------------------------------------------------------------------------------------------

def _message(item: dict[str, Any]) -> dict[str, Any] | None:
    ts = item.get("ts") or item.get("message_ts")
    if not ts:
        return None
    return {"ts": str(ts), "user": item.get("user") or item.get("user_id") or item.get("username"), "text": item.get("text") or "",
            "thread_ts": item.get("thread_ts"), "reply_count": item.get("reply_count"), "latest_reply": item.get("latest_reply"),
            "subtype": item.get("subtype"), "bot": bool(item.get("bot_id"))}


# Slack's MCP server answers with text inside JSON: {"messages": "<text>", "pagination_info": "…"}. In the detailed
# format, its default, a channel's messages are blocks, newest first. A message with replies gets a line about them:
#     === Message from Denis <denis@example.com> (U0EXAMPLE1) at 2026-10-21 16:48:22 CEST ===
#     Message TS: 1792594102.216779
#     <the message's text>
#     Thread: 2 replies (latest: 2026-10-21 16:48:42 CEST)
# A thread is its parent, then its replies:
#     === THREAD PARENT MESSAGE ===
#     From: Denis <denis@example.com> (U0EXAMPLE1)
#     Time: 2026-10-21 16:48:22 CEST
#     Message TS: 1792594102.216779
#     <the message's text>
#
#     === THREAD REPLIES (2 total) ===
#
#     --- Reply 1 of 2 ---
#     From: … (U0EXAMPLE1)
#     Time: …
#     Message TS: 1792594111.294259
#     <the reply's text>
# or "No thread messsages" (sic) after the parent. A reply also sent to the channel is in the channel's blocks too,
# with nothing to tell it from a new message. A message is found by its "Message TS:" line: its sender is in the
# header lines above it, its text runs to the next message's header.
_STAMP = re.compile(r"^\s*Message TS:\s*(\d+\.\d+)\s*$")
_HEADER = re.compile(r"^\s*(?:===.*===|---.*---|(?:From|Time):.*)?\s*$")
_SENDER = re.compile(r"\(([UWB][A-Z0-9]{6,})\)")
# Slack's lines about a message, not its text: "includes reactions + thread info", says the tool. Reactions
# haven't been seen yet.
_DETAIL = re.compile(r"^\s*(?:Thread:\s*\d+\s+repl(?:y|ies)\b|Reactions?:)", re.I)
_NO_REPLIES = re.compile(r"^\s*No thread mes+ages\s*$", re.I)
# Joins, renames and the like: Slack's text gives them no subtype, only their words, at the start
_WHO = r"(?:<@[UW][A-Z0-9]+(?:\|[^>]*)?>\s+)?"
SYSTEM_TEXT = re.compile(_WHO + r"(?:has (?:joined|left) the (?:channel|group|conversation)|was (?:added to|removed from) (?:this|the) channel"
                         r"|has renamed the channel|(?:set|cleared) the channel(?:'s)? (?:topic|purpose|description)"
                         r"|(?:has )?(?:un)?archived the channel|(?:un)?pinned a message)", re.I)


def _parse_blocks(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    stamps = [i for i, line in enumerate(lines) if _STAMP.match(line)]
    if not stamps:  # the usual answer: nothing new in the channel
        return []
    starts = []  # where each message's header starts
    for k, i in enumerate(stamps):
        j, floor = i, stamps[k - 1] + 1 if k else 0
        while j > floor and _HEADER.match(lines[j - 1]):
            j -= 1
        starts.append(j)
    ends = starts[1:] + [len(lines)]
    messages = []
    for i, start, end in zip(stamps, starts, ends, strict=True):
        sender = next((m.group(1) for line in lines[start:i] if (m := _SENDER.search(line))), None)
        words = "\n".join(line for line in lines[i + 1:end] if not (_DETAIL.match(line) or _NO_REPLIES.match(line))).strip()
        messages.append({"ts": _STAMP.match(lines[i]).group(1), "user": sender, "text": words, "thread_ts": None,
                         "reply_count": None, "latest_reply": None, "subtype": "system" if SYSTEM_TEXT.match(words) else None,
                         "bot": bool(sender and sender.startswith("B"))})
    return messages


def parse_messages(text: str) -> list[dict[str, Any]]:
    """Slack messages from a read tool's result: Slack's text, or messages as JSON."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("messages"), str):
        return _parse_blocks(data["messages"])
    if data is not None:
        return [m for m in map(_message, _items(data, "messages", "replies", "results")) if m]
    return _parse_blocks(text)


def parse_channels(text: str) -> list[tuple[str, str]]:
    """(name, ID) of the channels a search found. Slack's text has a block per channel:
        ### Result 1 of 1
        Name: #le-chonk-team
        Purpose: …
        Permalink: [link](https://….slack.com/archives/C0EXAMPLE1)
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("results"), str):
        text, data = data["results"], None
    if data is not None:
        return [(str(i.get("name") or "").lstrip("#"), str(i["id"])) for i in _items(data, "channels", "results", "items") if i.get("id")]
    found = []
    blocks = re.split(r"(?m)^\s*#+\s*Result\b.*$", text)[1:] or text.splitlines()
    for block in blocks:
        name = re.search(r"(?mi)^\s*Name:\s*#?(\S+)", block)
        channel = re.search(r"/archives/([CG][A-Z0-9]{8,})", block) or re.search(r"\b([CG][A-Z0-9]{8,})\b", block)
        if channel:
            found.append((name.group(1) if name else "", channel.group(1)))
    return found


def is_mine(message: dict[str, Any]) -> bool:
    """A message the agent posted: it starts with the mark."""
    return str(message.get("text") or "").lstrip().startswith(MARKS)


# Slack's system messages, by subtype as Slack's API has them, or "system" when read from Slack's text: joins,
# topic changes, edits. Others with a subtype, such as thread_broadcast (a reply also sent to the channel) or
# file_share (a message with a file), are people's.
SYSTEM_SUBTYPES = {"bot_message", "channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
                   "channel_archive", "channel_unarchive", "channel_convert_to_private", "group_join", "group_leave",
                   "message_changed", "message_deleted", "message_replied", "pinned_item", "unpinned_item", "reminder_add",
                   "tombstone", "huddle_thread", "ekm_access_denied", "system"}


def is_person(message: dict[str, Any]) -> bool:
    """A message from a person, to answer: not the agent's, not a bot's, not a system message."""
    return not (is_mine(message) or message.get("bot") or message.get("subtype") in SYSTEM_SUBTYPES)


class ConnectorSlack:
    """Read a channel and its threads, and reply in a thread, with the Slack connector."""

    def __init__(self, connector: Any | None = None) -> None:
        self.connector = connector if connector is not None else make_connector(SLACK)

    async def call(self, tool: str, **arguments: Any) -> Any:
        try:
            return await self.connector.call_tool(tool, {k: v for k, v in arguments.items() if v is not None})
        except Exception as exc:
            raise tool_error(SLACK, tool, exc) from exc

    async def read_channel(self, channel: str, oldest: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """The channel's messages after `oldest`, oldest first."""
        result = await self.call("slack_read_channel", channel_id=channel, oldest=oldest, limit=limit, response_format="detailed")
        messages = [m for m in parse_messages(content_text(result)) if not oldest or float(m["ts"]) > float(oldest)]
        return sorted(messages, key=lambda m: float(m["ts"]))

    async def read_thread(self, channel: str, thread: str, oldest: str | None = None) -> list[dict[str, Any]]:
        """The replies of a thread after `oldest`, oldest first, without the parent message."""
        result = await self.call("slack_read_thread", channel_id=channel, message_ts=thread, oldest=oldest, response_format="detailed")
        messages = [m for m in parse_messages(content_text(result)) if m["ts"] != thread and (not oldest or float(m["ts"]) > float(oldest))]
        return sorted(messages, key=lambda m: float(m["ts"]))

    async def post(self, channel: str, thread: str | None, text: str) -> None:
        await self.call("slack_send_message", channel_id=channel, thread_ts=thread, message=f"{MARK} {text}"[:4900])


class ConnectorNotifier:
    """The demo's Notifier, with the Slack connector: best effort, a failed message never fails a run."""

    def __init__(self, slack: ConnectorSlack | None = None) -> None:
        self._slack = slack

    @property
    def slack(self) -> ConnectorSlack:
        if self._slack is None:
            self._slack = ConnectorSlack()
        return self._slack

    async def post(self, channel: str | None, thread: str | None, text: str) -> None:
        if not channel:
            return
        try:
            await self.slack.post(channel, thread, text)
        except Exception as exc:
            log.warning(f"Slack message to {channel} failed: {exc}")

    async def upload(self, channel: str | None, thread: str | None, path: Path, title: str, comment: str) -> None:
        await self.post(channel, thread, comment)  # the report is on the release: the message links to it
