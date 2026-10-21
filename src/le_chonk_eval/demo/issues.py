"""A demo task as a GitHub issue: its sections, its hidden metadata, and the labels that track it.

The body is plain Markdown, so people can read and edit the issue on GitHub:

    <goal>

    ### Acceptance criteria
    - <criterion>

    ### Notes
    <notes>

    ### Skills
    - <GitHub URL of a skill folder>

    <!-- le-chonk {"ab_attempts": 3, "slack": {"channel": "C1", "thread": "1712.5"}} -->

The visible sections are the source of truth, so a skill added on GitHub counts too. The comment keeps what
only the pipeline needs: the Slack thread to report to, the A/B size, a model for this task. Other sections,
such as those of a GitHub issue form, are kept as they are, and Vibe and the judge read them too.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

READY = "vibe:ready"
RUNNING = "vibe:running"
DONE = "vibe:done"
NOT_DONE = "vibe:not-done"
FAILED = "vibe:failed"
AB_REQUESTED = "ab:requested"
AB_RUNNING = "ab:running"
AB_DONE = "ab:done"
AB_FAILED = "ab:failed"

# name: (color, description), created in the repo when the project is set
LABELS = {
    READY: ("0e8a16", "Checked by the triage agent: Vibe implements it next"),
    RUNNING: ("fbca04", "Vibe is working on it"),
    DONE: ("5319e7", "Implemented by Vibe and merged; the judge agrees"),
    NOT_DONE: ("d93f0b", "Merged because it builds, but the judge says it isn't done"),
    FAILED: ("b60205", "Vibe's change didn't build or its tests failed: nothing merged"),
    AB_REQUESTED: ("1d76db", "Run the skill A/B for this task"),
    AB_RUNNING: ("fbca04", "The skill A/B is running"),
    AB_DONE: ("0e8a16", "The skill A/B finished: the report is in the comments"),
    AB_FAILED: ("b60205", "The skill A/B failed"),
}
BUSY = {RUNNING, AB_RUNNING}

_META = re.compile(r"<!--\s*le-chonk\s+(\{.*?\})\s*-->", re.S)
_HEADING = re.compile(r"(?m)^#{2,4}\s+(.+?)\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")


class Task(BaseModel):
    title: str
    goal: str = ""
    criteria: list[str] = Field(default_factory=list)
    notes: str = ""
    skills: list[str] = Field(default_factory=list)
    ab_attempts: int | None = None
    model: str | None = None
    slack_channel: str | None = None
    slack_thread: str | None = None
    requested_by: str | None = None
    other: list[tuple[str, str]] = Field(default_factory=list)  # (heading, text) of the sections not listed above

    def body(self) -> str:
        parts = [self.goal.strip()]
        parts += [f"### {heading}\n{text}".rstrip() for heading, text in self.other]
        if self.criteria:
            parts.append("### Acceptance criteria\n" + "\n".join(f"- {c.strip()}" for c in self.criteria))
        if self.notes.strip():
            parts.append("### Notes\n" + self.notes.strip())
        if self.skills:
            parts.append("### Skills\n" + "\n".join(f"- {s.strip()}" for s in self.skills))
        meta: dict[str, Any] = {}
        if self.ab_attempts:
            meta["ab_attempts"] = self.ab_attempts
        if self.model:
            meta["model"] = self.model
        if self.slack_channel:
            meta["slack"] = {"channel": self.slack_channel, "thread": self.slack_thread}
        if self.requested_by:
            meta["requested_by"] = self.requested_by
        if meta:
            parts.append(f"<!-- le-chonk {json.dumps(meta, sort_keys=True)} -->")
        return "\n\n".join(p for p in parts if p) + "\n"

    def brief(self) -> str:
        """The task as Vibe and the judge read it: no Skills section, no metadata."""
        parts = [self.goal.strip()]
        parts += [f"{heading}:\n{text}".rstrip() for heading, text in self.other if text.strip()]
        if self.criteria:
            parts.append("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in self.criteria))
        if self.notes.strip():
            parts.append("Notes:\n" + self.notes.strip())
        return "\n\n".join(p for p in parts if p)

    @classmethod
    def from_issue(cls, title: str, body: str | None) -> Task:
        body = body or ""
        meta: dict[str, Any] = {}
        match = _META.search(body)
        if match:
            try:
                meta = json.loads(match.group(1))
            except json.JSONDecodeError:
                meta = {}
            body = body[: match.start()] + body[match.end():]
        headings = list(_HEADING.finditer(body))
        goal = body[: headings[0].start()] if headings else body
        criteria: list[str] = []
        skills: list[str] = []
        notes = ""
        other: list[tuple[str, str]] = []
        for i, heading in enumerate(headings):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
            name, text = heading.group(1).strip(), body[heading.end():end].strip()
            items = [m.group(1) for line in text.splitlines() if (m := _ITEM.match(line))]
            key = name.lower()
            if key.startswith("acceptance criteria") and items:
                criteria += items
            elif key.startswith("skills") and any(item.startswith("http") for item in items):
                skills += [item for item in items if item.startswith("http")]
            elif key.startswith("notes") and not notes:
                notes = text
            else:  # a section of an issue form, or criteria written as prose: kept as written
                other.append((name, text))

        slack = meta.get("slack") or {}
        return cls(
            title=title,
            goal=goal.strip(),
            criteria=criteria,
            notes=notes,
            skills=skills,
            other=other,
            ab_attempts=meta.get("ab_attempts"),
            model=meta.get("model"),
            slack_channel=slack.get("channel"),
            slack_thread=slack.get("thread"),
            requested_by=meta.get("requested_by"),
        )


def label_names(issue: dict[str, Any]) -> set[str]:
    return {label["name"] if isinstance(label, dict) else str(label) for label in issue.get("labels") or []}


def work_kind(issue: dict[str, Any]) -> str | None:
    """What the watcher should do with an open issue: "ab", "run", or nothing while it's busy or not queued."""
    labels = label_names(issue)
    if "pull_request" in issue or issue.get("state", "open") != "open" or labels & BUSY:
        return None
    if AB_REQUESTED in labels:
        return "ab"
    if READY in labels:
        return "run"
    return None
