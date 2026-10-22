"""scripts/check_screenshot.py: the tool call, its result and the image in one append, and the answer's check."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import mistralai.client.models as models

from fake_device import SHOT
from fakes import FakeClient, call

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_screenshot", ROOT / "scripts" / "check_screenshot.py")
check_screenshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_screenshot)

ANSWER = '{"hearts_filled": 4, "hearts_total": 5, "in_the_air": true, "seen": "Le Chonk mid-jump over a cactus."}'


def image(tmp_path: Path) -> Path:
    path = tmp_path / "shot.png"
    path.write_bytes(SHOT)
    return path


async def test_the_screenshot_goes_in_beside_the_result_and_the_answer_is_checked(tmp_path: Path) -> None:
    client = FakeClient([[call("t1", "screenshot")], [models.MessageOutputEntry(content=ANSWER)]])
    args = check_screenshot.parse_args(["--image", str(image(tmp_path)), "--expect-hearts", "4", "--expect-air", "yes"])
    assert await check_screenshot.main_async(args, client) == 0
    started, appended = client.inputs
    assert started["model"] == "mistral-large-4" and started["tools"][0]["function"]["name"] == "screenshot" and "agent_id" not in started
    assert started["completion_args"] == {"temperature": 0, "top_p": 1}  # greedy needs top_p 1, or the API refuses
    result, message = appended["inputs"]
    assert isinstance(result, models.FunctionResultEntry) and result.tool_call_id == "t1"
    assert isinstance(message, models.MessageInputEntry) and any(isinstance(c, models.ImageURLChunk) for c in message.content)


async def test_a_wrong_count_fails_and_a_refused_append_says_so(tmp_path: Path, capsys) -> None:
    client = FakeClient([[call("t1", "screenshot")], [models.MessageOutputEntry(content=ANSWER)]])
    args = check_screenshot.parse_args(["--image", str(image(tmp_path)), "--expect-hearts", "5"])
    assert await check_screenshot.main_async(args, client) == 1
    refusing = FakeClient([[call("t1", "screenshot")]], fail_after=1)
    assert await check_screenshot.main_async(args, refusing) == 2
    assert "refused the screenshot, sent beside the tool's result" in capsys.readouterr().out


async def test_a_refused_first_request_isnt_blamed_on_the_screenshot(tmp_path: Path, capsys) -> None:
    client = FakeClient([])

    async def refuse(**kwargs: object) -> None:
        raise RuntimeError("top_p must be 1 when using greedy sampling.")

    client.beta.conversations.start_async = refuse
    args = check_screenshot.parse_args(["--image", str(image(tmp_path))])
    assert await check_screenshot.main_async(args, client) == 2
    out = capsys.readouterr().out
    assert "the first request, before any screenshot was sent" in out and "greedy sampling" in out


def test_answers_in_json_are_read_from_the_text() -> None:
    assert check_screenshot.read_answer("Here you go:\n```json\n" + ANSWER + "\n```")["hearts_filled"] == 4
    assert check_screenshot.read_answer("I can't see an image.") is None
    assert check_screenshot.verdict(None, 5, None) == (False, ["the model didn't answer in JSON"])
