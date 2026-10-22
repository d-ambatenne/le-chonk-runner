"""Can the model see a screenshot sent beside a tool's result? The check QA depends on (docs/next.md, QA).

    uv run python scripts/check_screenshot.py --image shot.png --expect-hearts 5
    uv run python scripts/check_screenshot.py --serial emulator-5580 --expect-hearts 5

A function result is text only, so the QA agent's screenshots go in as an image in a user message, in the same
append as the results. This script does exactly that, through the QA agent's own code: it asks the model to call a
screenshot tool, answers with the result and the image, and asks how many red hearts are filled and whether Le
Chonk is in the air. It compares the count with --expect-hearts and says pass or fail.

It reads MISTRAL_API_KEY from .env and makes two or three model calls. With --serial it takes the screenshot from a
running emulator with adb; the screenshot is saved to runs/qa/spike/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
MODEL = "mistral-large-4"
INSTRUCTIONS = "You look at screenshots of an Android game. Use the screenshot tool to see the screen, then answer in JSON only."
QUESTION = """Take a screenshot with the screenshot tool. The game shows hearts at the top left: red hearts are the ones left, gray ones are lost. Then answer with this JSON and nothing else:
{"hearts_filled": <the number of red hearts>, "hearts_total": <all the hearts>, "in_the_air": <true if Le Chonk, the round cat, is above the ground>, "seen": "<one sentence on what you see>"}"""
SCREENSHOT_TOOL = {"type": "function", "function": {"name": "screenshot", "description": "A screenshot of the game's screen.",
                                                    "parameters": {"type": "object", "properties": {}, "required": []}}}
_JSON = re.compile(r"\{.*\}", re.S)
# Greedy sampling: Mistral refuses temperature 0 unless top_p is 1.
SAMPLING = {"temperature": 0, "top_p": 1}


class Refused(Exception):
    """The API refused a call. stage says which: "start", before any screenshot, or "screenshot", the append with it."""

    def __init__(self, stage: str, error: Exception) -> None:
        super().__init__(f"{type(error).__name__}: {str(error)[:500]}")
        self.stage = stage


def read_answer(text: str) -> dict[str, Any] | None:
    match = _JSON.search(text or "")
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


async def capture(serial: str) -> bytes:
    from le_chonk_eval.qa.device import Device
    from le_chonk_eval.qa.emulator import tool

    return await Device(serial, adb=tool("adb")).screenshot()


async def ask(client: Any, png: bytes, model: str = MODEL) -> tuple[str, dict[str, Any] | None]:
    """The conversation: the tool call, its result with the image in one append, the model's answer."""
    import mistralai.client.models as models

    from le_chonk_eval.agents.studio_agent import _message_text
    from le_chonk_eval.qa.agent import ToolResult, tool_inputs

    try:
        response = await client.beta.conversations.start_async(model=model, instructions=INSTRUCTIONS, tools=[SCREENSHOT_TOOL], inputs=QUESTION,
                                                               store=True, completion_args=SAMPLING)
    except Exception as exc:
        raise Refused("start", exc) from exc
    for _ in range(3):
        calls = [o for o in response.outputs if isinstance(o, models.FunctionCallEntry)]
        if not calls:
            break
        results = [(call.tool_call_id, ToolResult("The screenshot is attached in the next message.", png if call.name == "screenshot" else None))
                   for call in calls]
        try:
            response = await client.beta.conversations.append_async(conversation_id=response.conversation_id, inputs=tool_inputs(results))
        except Exception as exc:  # the API may refuse an image message right after a function result
            raise Refused("screenshot", exc) from exc
    text = "\n".join(_message_text(o.content) for o in response.outputs if isinstance(o, models.MessageOutputEntry)).strip()
    return text, read_answer(text)


def verdict(answer: dict[str, Any] | None, expect_hearts: int | None, expect_air: bool | None) -> tuple[bool, list[str]]:
    if answer is None:
        return False, ["the model didn't answer in JSON"]
    problems = []
    if expect_hearts is not None and answer.get("hearts_filled") != expect_hearts:
        problems.append(f"it counted {answer.get('hearts_filled')} red hearts, not {expect_hearts}")
    if expect_air is not None and bool(answer.get("in_the_air")) != expect_air:
        problems.append(f"it says Le Chonk is {'in the air' if answer.get('in_the_air') else 'on the ground'}")
    return not problems, problems


async def main_async(args: argparse.Namespace, client: Any = None) -> int:
    if args.image:
        png = Path(args.image).read_bytes()
    else:
        png = await capture(args.serial)
        saved = ROOT / "runs" / "qa" / "spike" / "shot.png"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(png)
        print(f"Screenshot: {saved}")
    if client is None:
        from mistralai.client import Mistral

        client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    try:
        text, answer = await ask(client, png, args.model)
    except Refused as exc:
        where = "the screenshot, sent beside the tool's result" if exc.stage == "screenshot" else "the first request, before any screenshot was sent"
        print(f"The API refused {where}: {exc}")
        return 2
    print(f"The model's answer:\n{text or '(nothing)'}\n")
    passed, problems = verdict(answer, args.expect_hearts, args.expect_air)
    print("PASS: the model read the screenshot." if passed else "FAIL: " + "; ".join(problems) + ".")
    return 0 if passed else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="a PNG screenshot of the game")
    source.add_argument("--serial", help="take the screenshot from this running emulator, such as emulator-5580")
    parser.add_argument("--expect-hearts", type=int, default=None, help="the red hearts on the screenshot")
    parser.add_argument("--expect-air", choices=["yes", "no"], default=None, help="whether Le Chonk is in the air on it")
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args(argv)
    args.expect_air = None if args.expect_air is None else args.expect_air == "yes"
    return args


def main() -> None:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    if not os.environ.get("MISTRAL_API_KEY"):
        sys.exit("MISTRAL_API_KEY is not set. Put it in .env (see .env.example).")
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
