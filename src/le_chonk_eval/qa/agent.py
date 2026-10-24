"""The QA agent: a Studio agent that checks one acceptance criterion at a time on the running app.

One conversation per criterion. The agent's tools are function tools, run here, on the machine with the emulator.
A function result is text only (FRICTIONS.md, row 35), so a screenshot goes in as an image in a user message,
in the same append as the results: tool_inputs() is the one place that does it, so a fallback can replace it.
The agent sees the criterion and the running app, never the code.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import mistralai.client.models as models
from pydantic import BaseModel, Field

from ..agents.studio import ensure_studio_agent
from ..agents.studio_agent import _arguments, _message_text
from .device import AdbError, Device, Element, describe, find, tree_text
from .inputs import DEFAULT_MODEL

AGENT_NAME = "le-chonk-qa"
MAX_ROUNDS = 25
FLOW_FIXES = 2  # how often the agent may fix a flow that fails its replay
DEVICE_FAILURES = 3  # a tool failing this often in one criterion means the device is broken, not the app
FIX_FLOW = "Your verdict is recorded, but your flow didn't work: {problem}\nFix the flow and call report_verdict again, with the same verdict and evidence."
VERDICTS = ("pass", "fail", "cant_check")
CHECKS = ("tree", "screenshot")

INSTRUCTIONS = """You test an Android app on an emulator against one acceptance criterion of a GitHub issue. You see the running app through your tools, never its code.

## What you know
The prompt has the issue's title and description, so you know what the app and its parts are; check only the criterion it names. When the description points to another issue, such as "Part of #1", read_issue gives you that issue's title and description. You never see the code, the pull requests or the comments.

## How to check
1. Start with ui_tree: the screen's texts, buttons and descriptions, each with a key such as e4 and its bounds.
2. Do what the criterion describes: tap elements by key, wait, read the tree again. restart_app starts over.
3. Prefer evidence from the tree, and read it again while things move: a debug build may describe what it draws, such as a character's state or where the obstacles are. An event too brief for a tree read, such as a jump, may be counted there, such as "jumps: 3": compare the count before and after, and that's a tree check. When the tree doesn't have it, use screenshots.
4. Things on screen move. A jump or an animation is over in under a second, and your next turn takes longer, so use tap_then_capture: it taps and takes the screenshot in one call. A screenshot arrives as an image in the next user message.
5. When an app crashes or freezes, read logcat.

## The verdict
Call report_verdict once, then stop:
- verdict: pass, fail, or cant_check when you couldn't observe it, with the reason.
- check: tree when your evidence is text in the UI tree; screenshot when you judged pixels. A failed tree check blocks the merge, a screenshot judgment doesn't, so don't call a judgment a tree check.
- evidence: what you did and saw, in two to five plain sentences, quoting the tree's texts and naming the screenshots.
- flow: for a tree check, a Maestro flow that repeats it, else leave it empty. Match texts loosely, with regular expressions such as "(?i).*game over.*", so the flow survives small changes. Use only launchApp, tapOn, assertVisible, assertNotVisible, extendedWaitUntil, waitForAnimationToEnd, takeScreenshot, back and stopApp. There's no wait step: to wait, use extendedWaitUntil with what you wait for, and a timeout.

Flows on a game: the screen never stops moving, so each step can take a few seconds. Don't assert a text that shows only briefly, such as a hint that fades after a few seconds; assert what lasts: a screen that appears or goes away, a value in a range. For a value that grows, wait for a small value, then for a larger one the app reaches well before the run can end.

Your flow is replayed once before it's kept. If the replay fails, report_verdict answers with the reason: fix the flow, and call report_verdict again. For example:

appId: <the app's package>
---
- launchApp:
    clearState: true
- extendedWaitUntil:
    visible: "(?i).*game over.*"
    timeout: 30000
- tapOn: "(?i)restart"
- assertNotVisible: "(?i).*game over.*"

Be strict and literal: a criterion passes only when you saw it hold. Never pass something you couldn't see."""


def _tool(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or []}}}


_WHERE = {
    "element": {"type": "string", "description": "An element's key from the last ui_tree, such as e4"},
    "x": {"type": "integer", "description": "Screen x in pixels, when there's no element"},
    "y": {"type": "integer", "description": "Screen y in pixels, when there's no element"},
}
TOOLS = [
    _tool("ui_tree", "The screen's UI tree: texts, buttons and descriptions, each with a key and its bounds."),
    _tool("screenshot", "A screenshot of the screen, sent as an image in the next message."),
    _tool("tap", "Tap an element by its key, or a point. Without either, the screen's center.", _WHERE),
    _tool("tap_then_capture", "Tap, wait ms milliseconds, take a screenshot: in one call, for what lasts under a second, such as a jump.",
          {**_WHERE, "ms": {"type": "integer", "description": "Milliseconds between the tap and the screenshot, 0 to 2000"}}),
    _tool("wait", "Wait, with the app running: for a timer, or for a game to end.",
          {"seconds": {"type": "number", "description": "0 to 60"}}, ["seconds"]),
    _tool("logcat", "The app's latest log lines and any crash."),
    _tool("restart_app", "Stop the app and start it again from its launcher."),
    _tool("read_issue", "Another issue of this repository: its title and description. For an issue this one points to, such as 'Part of #1'.",
          {"number": {"type": "integer", "description": "The issue's number"}}, ["number"]),
    _tool("report_verdict", "Report the criterion's verdict, once, at the end.", {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "check": {"type": "string", "enum": list(CHECKS), "description": "tree: the evidence is text in the UI tree; screenshot: you judged pixels"},
        "evidence": {"type": "string", "description": "What you did and saw, two to five sentences"},
        "flow": {"type": "string", "description": "For a tree check: a Maestro flow (YAML) that repeats it; else empty"},
    }, ["verdict", "check", "evidence"]),
]


class Verdict(BaseModel):
    number: int  # the criterion's place in the issue, from 1
    criterion: str
    verdict: Literal["pass", "fail", "cant_check"]
    check: Literal["tree", "screenshot"]
    blocking: bool = False  # a tree check: when it fails, the merge waits
    evidence: str = ""
    screenshots: list[str] = Field(default_factory=list)  # file names in the run's screenshots/ folder
    tree: str = ""  # the last UI tree the agent read
    log: str = ""  # crash lines, if any
    flow: str | None = None  # the flow the agent wrote
    flow_file: str | None = None  # where the flow is kept, once a replay passed
    source: Literal["agent", "flow"] = "agent"  # explored by the agent, or a kept flow replayed
    environment: str = ""  # a device problem that stopped the check: then the verdict says nothing about the app
    notes: list[str] = Field(default_factory=list)
    conversation_id: str | None = None
    rounds: int = 0


def verdict(number: int, criterion: str, value: str, check: str, **fields: Any) -> Verdict:
    return Verdict(number=number, criterion=criterion, verdict=value, check=check, blocking=check == "tree", **fields)  # type: ignore[arg-type]


@dataclass
class ToolResult:
    text: str
    image: bytes | None = None


@dataclass
class QaTools:
    """The agent's tools on one device and app. Screenshots are saved as c<criterion>-<n>.png."""

    device: Device
    package: str
    activity: str | None
    shots: Path
    criterion: int = 1
    last_tree: list[Element] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    reported: dict[str, Any] | None = None
    read_issue: Callable[[int], Awaitable[str]] | None = None  # another issue's title and description, by its number
    errors: dict[str, int] = field(default_factory=dict)  # the device's failures per tool, in this criterion
    broken: str = ""  # set when a tool keeps failing: the device is broken, so the check stops

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = getattr(self, f"_{name}", None)
        if name.startswith("_") or handler is None:
            return ToolResult(f"There's no tool named {name}.")
        try:
            return await handler(**args)
        except TypeError as exc:
            return ToolResult(f"Wrong arguments for {name}: {exc}")
        except AdbError as exc:
            self.errors[name] = self.errors.get(name, 0) + 1
            if self.errors[name] >= DEVICE_FAILURES:
                self.broken = f"{name} failed {self.errors[name]} times, last: {str(exc)[:300]}"
            return ToolResult(f"The device didn't answer: {str(exc)[:400]}")

    def save(self, png: bytes) -> str:
        self.shots.mkdir(parents=True, exist_ok=True)
        name = f"c{self.criterion}-{len(self.saved) + 1}.png"
        (self.shots / name).write_bytes(png)
        self.saved.append(name)
        return name

    async def point(self, element: Any = None, x: Any = None, y: Any = None) -> tuple[int, int, Element | None]:
        found = find(self.last_tree, element) if element else None
        if element and found is None:
            raise TypeError(f"no element {element} in the last ui_tree: call ui_tree again")
        if found:
            return (*found.center, found)
        if x is not None and y is not None:
            return int(x), int(y), None
        width, height = await self.device.screen_size()
        return width // 2, height // 2, None

    async def _ui_tree(self) -> ToolResult:
        self.last_tree = await self.device.ui_tree()
        return ToolResult(tree_text(self.last_tree))

    async def _screenshot(self) -> ToolResult:
        png = await self.device.screenshot()
        return ToolResult(f"Screenshot {self.save(png)}, attached in the next message.", png)

    async def _tap(self, element: Any = None, x: Any = None, y: Any = None) -> ToolResult:
        px, py, found = await self.point(element, x, y)
        await self.device.tap(px, py)
        return ToolResult(f"Tapped {describe(found, px, py)}.")

    async def _tap_then_capture(self, element: Any = None, x: Any = None, y: Any = None, ms: Any = 300) -> ToolResult:
        px, py, found = await self.point(element, x, y)
        delay = min(max(int(ms or 0), 0), 2000)
        png = await self.device.tap_then_capture(px, py, delay)
        return ToolResult(f"Tapped {describe(found, px, py)}; screenshot {self.save(png)} {delay} ms later, attached in the next message.", png)

    async def _wait(self, seconds: Any = 1) -> ToolResult:
        value = min(max(float(seconds), 0.0), 60.0)
        await self.device.wait(value)
        return ToolResult(f"Waited {value:g} s.")

    async def _logcat(self) -> ToolResult:
        return ToolResult((await self.device.logcat(self.package))[-4000:] or "(no log lines)")

    async def _restart_app(self) -> ToolResult:
        await self.device.restart_app(self.package, self.activity)
        self.last_tree = []
        return ToolResult("The app started again.")

    async def _read_issue(self, number: Any = None) -> ToolResult:
        if self.read_issue is None:
            return ToolResult("Other issues can't be read in this run.")
        try:
            wanted = int(number)
        except (TypeError, ValueError):
            raise TypeError("number must be an issue's number, such as 1") from None
        return ToolResult(await self.read_issue(wanted))

    async def _report_verdict(self, verdict: Any = "", check: Any = "", evidence: Any = "", flow: Any = "") -> ToolResult:
        if verdict not in VERDICTS or check not in CHECKS:
            return ToolResult(f"verdict must be one of {', '.join(VERDICTS)}, and check one of {', '.join(CHECKS)}: call it again.")
        self.reported = {"verdict": verdict, "check": check, "evidence": str(evidence or ""), "flow": str(flow or "").strip() or None}
        return ToolResult("Recorded.")


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def tool_inputs(results: list[tuple[str, ToolResult]]) -> list[Any]:
    """The append after a round of tool calls: each call's result, then one user message with the screenshots.

    The API takes only text as a function result (FRICTIONS.md, row 35). If the model can't see images sent this
    way (scripts/check_screenshot.py checks it), change this function only."""
    entries: list[Any] = [models.FunctionResultEntry(tool_call_id=call_id, result=result.text) for call_id, result in results]
    images = [result for _, result in results if result.image]
    if images:
        content: list[Any] = [models.TextChunk(text="The screenshots from the tool calls above, in order.")]
        content += [models.ImageURLChunk(image_url=data_uri(result.image)) for result in images if result.image]
        entries.append(models.MessageInputEntry(role="user", content=content))
    return entries


def criterion_prompt(number: int, criterion: str, package: str, issue: str = "") -> str:
    about = f"The issue, so you know what the app is about:\n\n{issue.strip()}\n\n---\n\n" if issue.strip() else ""
    return (f"{about}The app {package} is running on the emulator: use {package} as a flow's appId. "
            f"Check acceptance criterion {number}, and only this one:\n\n{criterion}\n\nUse your tools, then call report_verdict.")


def qa_model(model: str | None = None) -> str:
    return (model or os.environ.get("LECHONK_QA_MODEL") or DEFAULT_MODEL).split("/", 1)[-1]


async def ensure_qa_agent(client: Any, model: str | None = None, store: dict[str, Any] | None = None) -> str:
    return await ensure_studio_agent(
        client, AGENT_NAME, model=qa_model(model), instructions=INSTRUCTIONS, tools=TOOLS,
        description="Checks an Android app on an emulator against an issue's acceptance criteria (le-chonk QA).",
        completion_args={"temperature": 0.2}, store=store,
    )


async def flow_problem(check_flow: Callable[[str], Awaitable[str | None]] | None, reported: dict[str, Any]) -> str | None:
    """What's wrong with a passing tree check's flow, by check_flow's replay; None when it worked or there's nothing to check."""
    if check_flow is None or reported["verdict"] != "pass" or reported["check"] != "tree" or not reported["flow"]:
        return None
    return await check_flow(reported["flow"])


async def check_criterion(client: Any, agent_id: str, tools: QaTools, number: int, criterion: str, max_rounds: int = MAX_ROUNDS,
                          check_flow: Callable[[str], Awaitable[str | None]] | None = None, flow_fixes: int = FLOW_FIXES,
                          issue: str = "") -> Verdict:
    """One conversation: the agent explores until it reports a verdict, or runs out of rounds.

    check_flow replays a passing tree check's flow and says what went wrong. Then the agent gets the reason as
    report_verdict's answer and may fix the flow, flow_fixes times; the verdict stands either way."""
    tools.criterion, tools.reported, tools.saved, tools.last_tree, tools.errors, tools.broken = number, None, [], [], {}, ""
    response = await client.beta.conversations.start_async(agent_id=agent_id, inputs=criterion_prompt(number, criterion, tools.package, issue),
                                                           store=True)
    conversation = response.conversation_id
    reminded = False
    rounds = fixes = 0
    said = ""
    earlier: dict[str, Any] | None = None  # a verdict sent back only for its flow
    notes: list[str] = []
    while rounds < max_rounds and tools.reported is None:
        rounds += 1
        calls = [o for o in response.outputs if isinstance(o, models.FunctionCallEntry)]
        said = "\n".join(_message_text(o.content) for o in response.outputs if isinstance(o, models.MessageOutputEntry)).strip() or said
        if not calls:
            if reminded:
                break
            reminded = True
            response = await client.beta.conversations.append_async(conversation_id=conversation, inputs="Call report_verdict now.")
            continue
        results = [(call.tool_call_id, await tools.call(call.name, _arguments(call.arguments))) for call in calls]
        if tools.broken:  # no verdict on a broken device, not even from screenshots
            break
        if tools.reported is not None:
            problem = await flow_problem(check_flow, tools.reported)
            if problem is None:
                break
            if fixes >= flow_fixes:
                notes.append(f"Its flow wasn't kept: {problem}")
                break
            fixes += 1
            notes.append(f"Its flow didn't work, so the agent was asked to fix it: {problem}")
            earlier, tools.reported = tools.reported, None
            names = {call.tool_call_id: call.name for call in calls}
            results = [(call_id, ToolResult(FIX_FLOW.format(problem=problem)) if names[call_id] == "report_verdict" else result)
                       for call_id, result in results]
        response = await client.beta.conversations.append_async(conversation_id=conversation, inputs=tool_inputs(results))
    crashes = ""
    try:
        crashes = await tools.device.crashes()
    except AdbError:
        pass
    common = {"screenshots": list(tools.saved), "tree": tree_text(tools.last_tree)[:3000] if tools.last_tree else "", "log": crashes[-3000:],
              "conversation_id": conversation, "rounds": rounds}
    if tools.broken:
        return verdict(number, criterion, "cant_check", "screenshot", evidence=f"The device broke: {tools.broken}.", environment=tools.broken,
                       notes=notes, **common)
    reported = tools.reported or earlier
    if reported is None:
        why = f"No verdict after {rounds} rounds" + (f"; the agent last said: {said[:300]}" if said else "")
        return verdict(number, criterion, "cant_check", "screenshot", evidence=why, notes=notes, **common)
    if tools.reported is None:  # it ran out of rounds while fixing its flow: the verdict stands, the flow isn't kept
        notes.append("It ran out of rounds before its flow worked.")
    return verdict(number, criterion, reported["verdict"], reported["check"], evidence=reported["evidence"], flow=reported["flow"],
                   notes=notes, **common)
