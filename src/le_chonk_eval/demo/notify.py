"""Messages to the Slack thread a task came from, sent by the worker as the task moves on.

Best effort: without SLACK_BOT_TOKEN, or for an issue made on GitHub rather than from Slack, nothing is sent,
and a failed message never fails the run.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, client: Any | None = None, token: str | None = None) -> None:
        if client is None:
            token = token or os.environ.get("SLACK_BOT_TOKEN")
            if token:
                from slack_sdk.web.async_client import AsyncWebClient

                client = AsyncWebClient(token=token)
        self.client = client

    async def post(self, channel: str | None, thread: str | None, text: str) -> None:
        if not (self.client and channel):
            return
        try:
            await self.client.chat_postMessage(channel=channel, thread_ts=thread, text=text, unfurl_links=False, unfurl_media=False)
        except Exception as exc:
            log.warning(f"Slack message to {channel} failed: {exc}")

    async def upload(self, channel: str | None, thread: str | None, path: Path, title: str, comment: str) -> None:
        if not (self.client and channel and path.is_file()):
            return
        try:
            await self.client.files_upload_v2(channel=channel, thread_ts=thread, file=str(path), filename=path.name, title=title,
                                              initial_comment=comment)
        except Exception as exc:
            log.warning(f"Slack upload to {channel} failed: {exc}")
            await self.post(channel, thread, comment)


def make_notifier() -> Notifier:
    """The worker's notifier; tests replace it."""
    return Notifier()
