"""le-chonk-qa: a QA run as a workflow on your Mac's deployment, le-chonk-vibe, where the emulator is.

Your Mac's worker registers it (`uv run le-chonk next worker`). Nothing starts it yet: start it from Studio >
Workflows with {"repo": "<you>/le-chonk-game", "number": 1, "release": "<tag>"}, or run the same code with
`uv run le-chonk qa run`. Later, le-chonk-tasks starts it after a release, and the PR waits for its verdicts.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import mistralai.workflows as workflows

from ..demo.workflows import _error
from .inputs import QaInput


@workflows.activity(start_to_close_timeout=timedelta(minutes=120), retry_policy_max_attempts=1)
async def run_qa_activity(params: QaInput) -> dict[str, Any]:
    """Boot the emulator, install the release's APK and check every criterion. Not retried: a run takes long."""
    from .run import run_qa

    return await run_qa(params)


@workflows.workflow.define(
    name="le-chonk-qa",
    workflow_display_name="Le Chonk: QA of a release on your Mac's emulator",
    workflow_description="Installs a release's APK on an emulator and checks it against the issue's acceptance criteria with the QA agent.",
    execution_timeout=timedelta(hours=3),
    search_keys=["repo", "number"],
)
class QaRun:
    @workflows.workflow.entrypoint
    async def run(self, params: QaInput) -> dict[str, Any]:
        try:
            return await run_qa_activity(params)
        except Exception as exc:
            return {"ok": False, "error": _error(exc)}


WORKFLOWS = [QaRun]
