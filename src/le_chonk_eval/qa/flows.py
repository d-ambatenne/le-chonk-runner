"""The Maestro flows the QA agent writes: one per criterion it checked in the UI tree, kept per issue and replayed.

A kept flow that passes is the criterion's verdict, with no model call. When it fails, or there's none, the
agent explores. Flows live on this Mac, under runs/qa/flows/<owner>-<repo>/issue-<n>/criterion-<k>.yaml.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..utils.paths import runs_dir

COMMANDS = {"launchApp", "tapOn", "assertVisible", "assertNotVisible", "extendedWaitUntil", "waitForAnimationToEnd", "takeScreenshot",
            "back", "stopApp"}


@dataclass
class FlowResult:
    passed: bool
    message: str
    report: Path | None = None


def flows_dir(repo: str, number: int) -> Path:
    return runs_dir() / "qa" / "flows" / repo.replace("/", "-") / f"issue-{number}"


def flow_path(repo: str, number: int, criterion: int) -> Path:
    return flows_dir(repo, number) / f"criterion-{criterion}.yaml"


def normalize(text: str, package: str) -> tuple[str | None, list[str]]:
    """The flow with the app's package as its appId, and what's wrong with it: an empty list when it's fine."""
    try:
        documents = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as exc:
        return None, [f"not YAML: {str(exc)[:200]}"]
    if len(documents) != 2 or not isinstance(documents[0], dict) or not isinstance(documents[1], list):
        return None, ["a flow is a header with appId, then ---, then a list of steps"]
    header, steps = documents
    problems = []
    for step in steps:
        name = step if isinstance(step, str) else next(iter(step), None) if isinstance(step, dict) and len(step) == 1 else None
        if name not in COMMANDS:
            problems.append(f"step {step!r} isn't one of {', '.join(sorted(COMMANDS))}")
    if not steps:
        problems.append("the flow has no steps")
    if problems:
        return None, problems
    header = {**header, "appId": package}
    return yaml.safe_dump(header, sort_keys=False) + "---\n" + yaml.safe_dump(steps, sort_keys=False), []


def parse_junit(xml: str) -> tuple[bool, str]:
    """Passed, and why not: from Maestro's JUnit report."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return False, f"unreadable report: {exc}"
    cases = list(root.iter("testcase"))
    failures = [element for case in cases for element in case if element.tag in ("failure", "error")]
    if not cases:
        return False, "the report has no test"
    if failures:
        return False, " ".join(((f.get("message") or "") + " " + (f.text or "")).strip() for f in failures)[:600] or "failed"
    return True, "passed"


async def run_status(args: list[str], timeout: float) -> tuple[int, str]:
    """A command's exit code and output; a failure isn't an error here: a flow is allowed to fail."""
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"{shlex.join(args)} timed out after {timeout:g} s"
    return process.returncode or 0, out.decode(errors="replace")


def maestro_bin() -> str:
    """LECHONK_MAESTRO, else maestro on the PATH, else where its installer puts it."""
    installed = Path.home() / ".maestro" / "bin" / "maestro"
    return os.environ.get("LECHONK_MAESTRO") or shutil.which("maestro") or str(installed)


class Maestro:
    """The Maestro CLI, on one device."""

    def __init__(self, serial: str, binary: str | None = None, run: Any = run_status, timeout: float = 300) -> None:
        self.serial, self.binary, self.run, self.timeout = serial, binary or maestro_bin(), run, timeout

    def command(self, flow: Path, report: Path) -> list[str]:
        return [self.binary, "--device", self.serial, "test", "--format", "JUNIT", "--output", str(report),
                "--test-output-dir", str(report.parent / "maestro"), str(flow)]

    async def replay(self, flow: Path, out: Path) -> FlowResult:
        out.mkdir(parents=True, exist_ok=True)
        report = out / f"{flow.stem}.xml"
        report.unlink(missing_ok=True)
        code, output = await self.run(self.command(flow, report), self.timeout)
        if report.is_file():
            passed, message = parse_junit(report.read_text(encoding="utf-8", errors="replace"))
            if passed and code != 0:
                return FlowResult(False, f"maestro exited with {code}: {output.strip()[-300:]}", report)
            return FlowResult(passed, message, report)
        return FlowResult(False, f"maestro exited with {code} and wrote no report: {output.strip()[-400:]}")


def keep(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
