"""What the next phase needs from your Mistral account: a read-only check.

    uv run python scripts/check_next.py
    uv run python scripts/check_next.py --channel le-chonk --repo <you>/le-chonk-game

It only reads: it creates, starts, sends and changes nothing. It reads MISTRAL_API_KEY from .env, like the rest
of the project, and prints no secrets. It writes three files to runs/:

- check-next.txt         this report
- connector-tools.json   the full definitions of the GitHub and Slack tools the next phase calls
- connector-samples.json with --channel and --repo: what those tools return, read from that channel and repo.
                         Use a channel made for the demo: its last 10 messages, and a thread with replies, are saved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mistralai.client import Mistral

ROOT = Path(__file__).resolve().parents[1]
MODEL = "mistral-large-4"
# connector: (label, the tools the next phase calls)
CONNECTORS = {
    "github_app": ("GitHub", ["list_issues", "search_issues", "issue_read", "issue_write", "add_issue_comment",
                              "create_pull_request", "list_pull_requests", "pull_request_read", "merge_pull_request",
                              "search_repositories", "get_file_contents", "list_branches", "create_repository"]),
    "slack": ("Slack", ["slack_search_channels", "slack_read_channel", "slack_read_thread", "slack_send_message"]),
}
CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{6,}$")

lines: list[str] = []


def say(text: str = "") -> None:
    print(text)
    lines.append(text)


def failure(error: Exception) -> str:
    status = getattr(error, "status_code", None)
    body = " ".join(str(getattr(error, "body", None) or error).split())
    return f"{type(error).__name__}{f' {status}' if status else ''}: {body[:200]}"


def attempt(title: str, step: Callable[[], None]) -> None:
    try:
        step()
    except Exception as error:  # noqa: BLE001 - every failure is part of the report
        say(f"  ✗ {title}: {failure(error)}")


def dump(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value


def schema_of(tool: dict[str, Any]) -> dict[str, Any]:
    return tool.get("inputSchema") or tool.get("input_schema") or {}


def signature(name: str, tool: dict[str, Any]) -> str:
    """name(required, optional?) from the tool's input schema."""
    schema = schema_of(tool)
    required = set(schema.get("required") or [])
    params = [p if p in required else f"{p}?" for p in (schema.get("properties") or {})]
    return f"{name}({', '.join(params)})"


def param(tool: dict[str, Any] | None, *candidates: str) -> str | None:
    """The first of the candidate names that the tool takes."""
    properties = schema_of(tool or {}).get("properties") or {}
    return next((c for c in candidates if c in properties), None)


def connected_tools(client: Mistral, name: str, label: str, wanted: list[str]) -> dict[str, dict[str, Any]] | None:
    """The connector's tools when you're signed in, else None. Listing its tools needs your credentials."""
    try:
        tools = {str(t["name"]): t for t in map(dump, client.beta.connectors.list_tools(connector_id_or_name=name))}
    except Exception as error:  # noqa: BLE001
        status = getattr(error, "status_code", None)
        say(f"  ✗ {label} '{name}': " + ("not connected yet, see Studio > Connectors" if status == 401 else failure(error)))
        return None
    kinds = ""
    try:
        credentials = client.beta.connectors.list_user_credentials(connector_id_or_name=name).credentials
        kinds = ", ".join(sorted({str(getattr(c.authentication_type, "value", c.authentication_type)) for c in credentials}))
    except Exception:  # noqa: BLE001 - the tool list already shows you're connected
        pass
    say(f"  ✓ {label} '{name}': connected" + (f" with {kinds}" if kinds else ""))
    missing = [t for t in wanted if t not in tools]
    say("    has every tool the next phase uses" if not missing else f"    missing: {', '.join(missing)}")
    for tool in wanted:
        if tool in tools:
            say(f"    {signature(tool, tools[tool])}")
    return tools


def call(client: Mistral, connector: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One read-only tool call, as it comes back: MCP content blocks, texts cut to 12,000 characters."""
    try:
        result = dump(client.beta.connectors.call_tool(connector_id_or_name=connector, tool_name=tool, arguments=arguments))
    except Exception as error:  # noqa: BLE001
        return {"arguments": arguments, "error": failure(error)}
    for block in result.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            block["text"] = block["text"][:12000]
    return {"arguments": arguments, "result": result}


def text_of(sample: dict[str, Any]) -> str:
    return "\n".join(b.get("text", "") for b in (sample.get("result") or {}).get("content") or [] if isinstance(b, dict))


def slack_text(sample: dict[str, Any]) -> str:
    """Slack's tools answer with text inside JSON, such as {"messages": "<text>"}: that text."""
    text = text_of(sample)
    try:
        data = json.loads(text)
    except ValueError:
        return text
    return "\n".join(v for v in data.values() if isinstance(v, str)) if isinstance(data, dict) else text


def sample_slack(client: Mistral, tools: dict[str, dict[str, Any]], channel: str) -> dict[str, Any]:
    samples: dict[str, Any] = {}
    channel_id = channel.lstrip("#")
    if not CHANNEL_ID.match(channel_id):
        search = tools.get("slack_search_channels")
        keywords, query = param(search, "keywords"), param(search, "query", "search", "name")
        if not (keywords or query):
            say("    can't look the channel up by name: give its ID (Slack > the channel > channel details, at the bottom)")
            return samples
        arguments = {keywords: [channel_id]} if keywords else {query: channel_id}
        samples["slack_search_channels"] = found = call(client, "slack", "slack_search_channels", arguments)
        results = slack_text(found)
        pairs = []  # (name, ID): Slack writes a block per channel, "### Result 1 of 2", "Name: #…", a link with the ID
        for block in re.split(r"(?m)^\s*#+\s*Result\b.*$", results)[1:] or results.splitlines():
            if ids := re.findall(r"\b[CG][A-Z0-9]{8,}\b", block):
                name = re.search(r"Name:\s*#?([\w.-]+)", block)
                pairs.append((name.group(1) if name else "", ids[0]))
        if not pairs:
            say(f"    no channel ID found for '{channel}': give its ID instead")
            return samples
        exact = [cid for name, cid in pairs if name == channel_id]
        channel_id = (exact or [pairs[0][1]])[0]
        say(f"    '{channel}' found: {', '.join(f'#{n} ({c})' if n else c for n, c in pairs)}; reading {channel_id}")
        if not exact:
            say(f"    no channel is named exactly '{channel}': `next start` needs a channel's exact name, or its ID")
    read = tools.get("slack_read_channel")
    where = param(read, "channel_id", "channel", "channelId")
    limit = param(read, "limit", "count", "max_results")
    if not where:
        return samples
    samples["slack_read_channel"] = history = call(client, "slack", "slack_read_channel", {where: channel_id, **({limit: 10} if limit else {})})
    say("    read the channel: " + ("error, see the samples" if "error" in history else "ok"))
    if param(read, "response_format"):
        samples["slack_read_channel_concise"] = call(client, "slack", "slack_read_channel",
                                                     {where: channel_id, "response_format": "concise", **({limit: 3} if limit else {})})
    thread = tools.get("slack_read_thread")
    thread_where, thread_ts = param(thread, "channel_id", "channel", "channelId"), param(thread, "message_ts", "thread_ts", "ts")
    stamps = re.findall(r"Message TS:\s*(\d+\.\d+)", slack_text(history)) or re.findall(r"\b\d{10}\.\d{6}\b", slack_text(history))
    if not (stamps and thread_where and thread_ts):
        return samples
    # A thread with replies shows how Slack writes replies: the last format the next phase hasn't seen
    for stamp in stamps[:10]:
        sample = call(client, "slack", "slack_read_thread", {thread_where: channel_id, thread_ts: stamp})
        samples.setdefault("slack_read_thread", sample)
        if "error" not in sample and len(re.findall(r"TS:\s*\d+\.\d+", slack_text(sample))) > 1:
            samples["slack_read_thread_with_replies"] = sample
            break
    say("    read a thread: " + ("error, see the samples" if "error" in samples["slack_read_thread"] else "ok"))
    say("    a thread with replies: ok" if "slack_read_thread_with_replies" in samples else
        "    no thread with replies in the last 10 messages: reply to a message in its thread, and run this again")
    return samples


def sample_github(client: Mistral, tools: dict[str, dict[str, Any]], repo: str) -> dict[str, Any]:
    owner, _, name = repo.removeprefix("https://github.com/").strip("/").partition("/")
    samples: dict[str, Any] = {}
    listing = tools.get("list_issues")
    per_page = param(listing, "perPage", "per_page")
    samples["list_issues"] = call(client, "github_app", "list_issues", {"owner": owner, "repo": name, **({per_page: 3} if per_page else {})})
    query = param(tools.get("search_issues"), "query", "q")
    if query:
        samples["search_issues"] = call(client, "github_app", "search_issues", {query: "jump", "owner": owner, "repo": name})
    samples["list_pull_requests"] = call(client, "github_app", "list_pull_requests", {"owner": owner, "repo": name, **({per_page: 3} if per_page else {})})
    numbers = re.findall(r'"number":\s*(\d+)', text_of(samples["list_issues"]))
    if numbers:
        samples["issue_read"] = call(client, "github_app", "issue_read", {"method": "get", "owner": owner, "repo": name, "issue_number": int(numbers[0])})
    say("    read the repo: " + ", ".join(f"{k} {'error' if 'error' in v else 'ok'}" for k, v in samples.items()))
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel", help="the Slack channel made for the demo: its name or ID")
    parser.add_argument("--repo", help="the demo repo: <owner>/<name> or its URL")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not os.environ.get("MISTRAL_API_KEY"):
        print("No MISTRAL_API_KEY in .env")
        return 1
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    out = ROOT / "runs"
    out.mkdir(exist_ok=True)

    say("Models")

    def models() -> None:
        ids = sorted(m.id for m in client.models.list().data)
        found = MODEL in ids
        say(f"  {'✓' if found else '✗'} {MODEL}{'' if found else ' is not in your model list'}")
        say("    large and Devstral models: " + ", ".join(i for i in ids if "large" in i or "devstral" in i))
    attempt("models", models)

    say("Workflows deployments")

    def deployments() -> None:
        found = client.workflows.deployments.list_deployments(active_only=False).deployments
        say(f"  ✓ the Workflows API answers: {len(found)} deployment{'' if len(found) == 1 else 's'}")
        for d in found:
            kind = "managed" if d.managed else "self-hosted"
            say(f"    {d.name}: {kind}, {'active' if d.is_active else 'inactive'}, {'hardened' if d.is_hardened else 'not hardened'}")
    attempt("deployments", deployments)

    say("Connectors")
    definitions: dict[str, Any] = {}
    samples: dict[str, Any] = {}
    for name, (label, wanted) in CONNECTORS.items():
        tools = connected_tools(client, name, label, wanted)
        if tools is None:
            continue
        definitions[name] = {tool: tools[tool] for tool in wanted if tool in tools}
        if name == "slack" and args.channel:
            samples["slack"] = sample_slack(client, tools, args.channel)
        if name == "github_app" and args.repo:
            samples["github_app"] = sample_github(client, tools, args.repo)
    (out / "connector-tools.json").write_text(json.dumps(definitions, indent=2, ensure_ascii=False))
    if samples:
        (out / "connector-samples.json").write_text(json.dumps(samples, indent=2, ensure_ascii=False))

    say()
    say("Check these in the console; the API doesn't show them:")
    say("  - Admin > Billing: pay-as-you-go is on. Managed deployments need it; Pro without it gets none.")
    say("  - github.com/apps/mistralai is installed on the repo a managed deployment would build from.")

    (out / "check-next.txt").write_text("\n".join(lines) + "\n")
    print("\nSaved to runs/: check-next.txt, connector-tools.json" + (", connector-samples.json" if samples else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
