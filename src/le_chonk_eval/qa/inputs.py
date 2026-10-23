"""What a QA run takes. Light on purpose: the workflow imports it in Temporal's sandbox."""

from __future__ import annotations

from pydantic import BaseModel

DEFAULT_MODEL = "mistral-large-4"  # the model the triage agent runs on; it reads images too


class QaInput(BaseModel):
    repo: str  # owner/name or its URL
    number: int  # the issue
    release: str  # the release's tag, such as issue-1-20261021-202046
    model: str | None = None  # the QA agent's model: LECHONK_QA_MODEL, else DEFAULT_MODEL
    serial: str | None = None  # an emulator or phone already running; by default lechonk-qa is booted and stopped
    label: bool = True  # mark the issue qa:tested or qa:bugs, with a comment of the verdicts
