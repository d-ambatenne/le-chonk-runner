"""The Slack app: Socket Mode, so it runs on this Mac without a public URL.

It passes the triage agent every message that mentions the app, every reply in a thread the agent is part of,
and every direct message, and posts the agent's reply in the thread. Tokens: SLACK_BOT_TOKEN (xoxb-...) and
SLACK_APP_TOKEN (xapp-..., with connections:write), from the app made with demo/slack-app-manifest.yaml.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from .triage import SlackContext, Triage

log = logging.getLogger(__name__)
MENTION = re.compile(r"<@[A-Z0-9]+>")
IGNORED_SUBTYPES = {"bot_message", "message_changed", "message_deleted", "channel_join", "channel_leave", "thread_broadcast"}


def should_answer(event: dict[str, Any], kind: str, bot_user_id: str | None) -> bool:
    if event.get("bot_id") or event.get("subtype") in IGNORED_SUBTYPES or not (event.get("text") or "").strip():
        return False
    if bot_user_id and event.get("user") == bot_user_id:
        return False
    if kind == "app_mention" or event.get("channel_type") == "im":  # Slack sends no app_mention in a DM
        return True
    if bot_user_id and f"<@{bot_user_id}>" in event.get("text", ""):
        return False  # the app_mention event of the same message answers it
    thread = event.get("thread_ts")
    return bool(thread) and Triage.knows(SlackContext(event["channel"], thread))


async def answer(event: dict[str, Any], slack: Any, triage: Triage) -> str:
    channel = event["channel"]
    thread = event.get("thread_ts") or event["ts"]
    text = MENTION.sub("", event.get("text", "")).strip() or "Hello"
    await _react(slack, "reactions_add", channel, event["ts"])
    try:
        reply = await triage.handle(SlackContext(channel, thread, event.get("user")), text)
    finally:
        await _react(slack, "reactions_remove", channel, event["ts"])
    await slack.chat_postMessage(channel=channel, thread_ts=thread, text=reply, unfurl_links=False, unfurl_media=False)
    return reply


async def _react(slack: Any, method: str, channel: str, ts: str) -> None:
    try:
        await getattr(slack, method)(channel=channel, timestamp=ts, name="eyes")
    except Exception:  # without reactions:write, or already removed
        pass


def build_app(triage: Triage, bot_token: str | None = None) -> Any:
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=bot_token or os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    async def on_mention(event: dict[str, Any], client: Any, context: Any) -> None:
        if should_answer(event, "app_mention", context.get("bot_user_id")):
            await answer(event, client, triage)

    @app.event("message")
    async def on_message(event: dict[str, Any], client: Any, context: Any) -> None:
        if should_answer(event, "message", context.get("bot_user_id")):
            await answer(event, client, triage)

    return app


async def run_slack(triage: Triage) -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    missing = [name for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"{' and '.join(missing)} not set: see docs/demo.md for the Slack app.")
    handler = AsyncSocketModeHandler(build_app(triage), os.environ["SLACK_APP_TOKEN"])
    log.info("Slack app connected over Socket Mode")
    await handler.start_async()
