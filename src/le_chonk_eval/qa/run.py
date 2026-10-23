"""One QA run: a release's APK on the emulator, checked against its issue's acceptance criteria.

    1. The issue's criteria, and the release's APK (a debug build: the one the verifier makes, with the test hooks).
    2. lechonk-qa boots from its clean snapshot, or the device you name; the APK is installed and started.
    3. Per criterion: a kept Maestro flow is replayed; if there's none, or it fails, the agent explores and reports.
       A flow the agent wrote for a passing tree check is replayed, and kept when it passes. When the replay
       fails, the agent gets the reason and may fix the flow, twice; the verdict stands either way.
    4. runs/qa/<issue>-<release>/: verdicts.json, report.md, screenshots/, apk/, flows/.

The run only reports. Filing issues and holding the merge come with the PR loop.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager, nullcontext
from pathlib import Path
from typing import Any

from ..utils.paths import runs_dir
from .agent import QaTools, Verdict, check_criterion, ensure_qa_agent, verdict
from .device import AdbError, Device
from .emulator import Emulator, apk_package, tool
from .flows import Maestro, flow_path, keep, normalize
from .inputs import QaInput

ICONS = {"pass": "pass", "fail": "FAIL", "cant_check": "n/a "}


def out_dir(number: int, release: str) -> Path:
    return runs_dir() / "qa" / f"{number}-{release}"


def pick_apk(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The release's APK: a debug build when it has one, else the first."""
    apks = [a for a in assets if str(a.get("name", "")).lower().endswith(".apk")]
    return next((a for a in apks if "debug" in a["name"].lower()), apks[0] if apks else None)


async def criteria_and_apk(github: Any, repo: str, number: int, release: str, out: Path) -> tuple[str, list[str], Path]:
    from ..demo.issues import Task

    issue = await github.issue(repo, number)
    task = Task.from_issue(issue["title"], issue.get("body"))
    if not task.criteria:
        raise ValueError(f"Issue #{number} has no acceptance criteria to check")
    found = await github.release_by_tag(repo, release)
    if found is None:
        raise ValueError(f"{repo} has no release {release}")
    asset = pick_apk(found.get("assets") or [])
    if asset is None:
        raise ValueError(f"Release {release} has no APK")
    apk = await github.download_asset(repo, asset, out / "apk" / asset["name"])
    return task.title, task.criteria, apk


async def install(device: Device, apk: Path, package_of: Any = apk_package) -> tuple[str, str | None]:
    """Install the APK. Its package comes from aapt2; without it, it's the one the install adds (the snapshot has no other app)."""
    package = await package_of(apk)
    before = await device.packages()
    await device.install(str(apk))
    if package is None:
        after = await device.packages()
        added = sorted(after - before) or sorted(after)
        if len(added) != 1:
            raise RuntimeError(f"Can't tell which app {apk.name} is: installed apps are {', '.join(sorted(after)) or 'none'}")
        package = added[0]
    activity = await device.launch_activity(package)
    await device.restart_app(package, activity)
    return package, activity


async def device_problem(device: Device, package: str, activity: str | None) -> str:
    """What's wrong with the device: "" when its UI tree answers, at once or after the app restarts."""
    problem = ""
    for attempt in range(2):
        try:
            if attempt:
                await device.restart_app(package, activity)
            await device.ui_tree()
            return ""
        except AdbError as exc:
            problem = f"its UI tree didn't answer: {str(exc)[:300]}"
    return problem


def not_checked(k: int, criterion: str, broken: str, notes: list[str] | None = None) -> Verdict:
    return verdict(k, criterion, "cant_check", "screenshot", evidence=f"Not checked: the device broke ({broken}).", environment=broken,
                   notes=notes or [])


async def check_all(client: Any, device: Device, maestro: Maestro, repo: str, number: int, criteria: list[str], package: str,
                    activity: str | None, out: Path, model: str | None = None) -> list[Verdict]:
    """Every criterion, in order. A broken device stops the checks: what's left isn't checked, rather than judged on it."""
    tools = QaTools(device, package, activity, out / "screenshots")
    agent_id: str | None = None
    verdicts: list[Verdict] = []
    broken = ""
    for k, criterion in enumerate(criteria, 1):
        if broken:
            verdicts.append(not_checked(k, criterion, broken))
            continue
        kept = flow_path(repo, number, k)
        notes = []
        if kept.is_file():
            replayed = await maestro.replay(kept, out / "flows")
            if replayed.passed:
                verdicts.append(verdict(k, criterion, "pass", "tree", source="flow", flow_file=str(kept),
                                        evidence=f"The kept flow {kept.name} passed again."))
                continue
            notes.append(f"The kept flow {kept.name} failed: {replayed.message}")
        broken = await device_problem(device, package, activity)
        if broken:
            verdicts.append(not_checked(k, criterion, broken, notes))
            continue
        await device.restart_app(package, activity)
        agent_id = agent_id or await ensure_qa_agent(client, model)
        kept_file: dict[int, str] = {}

        async def check_flow(flow: str, k: int = k) -> str | None:
            problem, saved = await try_flow(flow, maestro, repo, number, k, package, out)
            if saved:
                kept_file[k] = saved
            return problem

        found = await check_criterion(client, agent_id, tools, k, criterion, check_flow=check_flow)
        found.notes = notes + found.notes
        found.flow_file = kept_file.get(k)
        broken = found.environment
        verdicts.append(found)
    return verdicts


async def try_flow(flow: str, maestro: Maestro, repo: str, number: int, criterion: int, package: str, out: Path) -> tuple[str | None, str | None]:
    """Replay the agent's flow once: (None, where it's kept) when it passes, else (what went wrong, None)."""
    text, problems = normalize(flow, package)
    if text is None:
        return "; ".join(problems), None
    draft = keep(out / "flows" / f"criterion-{criterion}.yaml", text)
    replayed = await maestro.replay(draft, out / "flows")
    if replayed.passed:
        return None, str(keep(flow_path(repo, number, criterion), text))
    return f"it failed on replay: {replayed.message}", None


def summary(repo: str, number: int, release: str, title: str, verdicts: list[Verdict], out: Path, package: str) -> dict[str, Any]:
    counts = {value: sum(1 for v in verdicts if v.verdict == value) for value in ICONS}
    return {
        "ok": True, "repo": repo, "number": number, "release": release, "title": title, "package": package, "out": str(out),
        "passed": counts["pass"], "failed": counts["fail"], "cant_check": counts["cant_check"],
        "blocking": [v.number for v in verdicts if v.verdict == "fail" and v.blocking],
        "environment_errors": list(dict.fromkeys(v.environment for v in verdicts if v.environment)),
        "verdicts": [v.model_dump() for v in verdicts],
    }


def table(result: dict[str, Any]) -> str:
    lines = [f"QA of {result['repo']}#{result['number']}, release {result['release']}: "
             f"{result['passed']} passed, {result['failed']} failed, {result['cant_check']} not checked"]
    for v in result["verdicts"]:
        kind = ("tree" if v["check"] == "tree" else "screenshot") + (", replayed flow" if v["source"] == "flow" else "")
        block = "  blocks the merge" if v["verdict"] == "fail" and v["blocking"] else ""
        lines.append(f"  {ICONS[v['verdict']]}  {v['number']}. {v['criterion'][:70]}  ({kind}){block}")
    for problem in result.get("environment_errors") or []:
        lines.append(f"The device broke, so the run stopped: {problem}\nRun again on a fresh emulator: without --serial, QA boots lechonk-qa from its clean snapshot.")
    if result.get("issue"):
        lines.append(result["issue"])
    return "\n".join(lines)


def report_markdown(result: dict[str, Any]) -> str:
    rows = ["| # | Criterion | Verdict | Check | Evidence |", "|---|---|---|---|---|"]
    for v in result["verdicts"]:
        shots = v["screenshots"]
        if len(shots) > 8:  # a long hunt: the first and last few, and the count
            shots = shots[:3] + ["…"] + shots[-3:] + [f"{len(v['screenshots'])} in all"]
        evidence = (v["evidence"] + (" Screenshots: " + ", ".join(shots) + "." if shots else "")).replace("|", "\\|")
        evidence = evidence.replace("\n", " ")
        rows.append(f"| {v['number']} | {v['criterion'].replace('|', '/')} | {v['verdict']} | {v['check']} | {evidence} |")
    head = f"# QA of #{result['number']}: {result['title']}\n\nRelease `{result['release']}`, app `{result['package']}`.\n\n"
    return head + "\n".join(rows) + "\n"


def write_results(result: dict[str, Any], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "verdicts.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out / "report.md").write_text(report_markdown(result), encoding="utf-8")


def issue_comment(result: dict[str, Any]) -> str:
    """QA's comment on the issue: the count, a row per criterion, and why the failures failed."""
    total = len(result["verdicts"])
    head = (f"QA ran release `{result['release']}` on an emulator: {result['passed']} of {total} criteria passed"
            + (f", {result['failed']} failed" if result["failed"] else "")
            + (f", {result['cant_check']} couldn't be checked" if result["cant_check"] else "") + ".")
    rows = ["| # | Criterion | Verdict | Checked by |", "|---|---|---|---|"]
    for v in result["verdicts"]:
        how = "the screen's text" if v["check"] == "tree" else "a screenshot, judged by the agent"
        how += ", a saved flow" if v["source"] == "flow" else ""
        rows.append(f"| {v['number']} | {v['criterion'].replace('|', '/')} | {v['verdict']} | {how} |")
    why = [f"- **{v['number']}.** {v['evidence'][:400]}" for v in result["verdicts"] if v["verdict"] == "fail"]
    note = "The QA agent sees the issue and the running app, never the code. A failed check of the screen's text blocks a merge; a screenshot judgment doesn't."
    return "\n\n".join([head, "\n".join(rows)] + (["Why it failed:\n" + "\n".join(why)] if why else []) + [note])


async def mark_issue(github: Any, result: dict[str, Any]) -> str:
    """Mark the issue with QA's verdict: qa:tested when every criterion passed, qa:bugs when one failed, and comment.

    A run on a broken device marks nothing: its verdicts say nothing about the app. Returns what was done."""
    from ..demo.issues import QA_BUGS, QA_TESTED

    if result.get("environment_errors"):
        return "Issue not marked: the device broke, so the verdicts don't count."
    repo, number = result["repo"], result["number"]
    verdicts = [v["verdict"] for v in result["verdicts"]]
    label = QA_BUGS if "fail" in verdicts else QA_TESTED if verdicts and all(v == "pass" for v in verdicts) else None
    if label:
        await github.ensure_labels(repo)
        await github.relabel(repo, number, add=[label], remove=[QA_TESTED if label == QA_BUGS else QA_BUGS])
    await github.comment(repo, number, issue_comment(result))
    return f"Issue #{number} marked {label}, with a comment." if label else f"Issue #{number}: commented; not marked, since some criteria weren't checked."


async def mark_saved(repo: str, number: int, release: str, github: Any = None) -> str:
    """Mark an issue from a QA run that's already done, from its verdicts.json."""
    from ..demo.github import GitHub, parse_repo

    saved = out_dir(number, release) / "verdicts.json"
    if not saved.is_file():
        raise ValueError(f"No QA run of #{number} on {release}: {saved} doesn't exist")
    result = json.loads(saved.read_text(encoding="utf-8"))
    if result.get("repo") != parse_repo(repo):
        raise ValueError(f"{saved} is a run on {result.get('repo')}, not {parse_repo(repo)}")
    owned = github is None
    github = github or GitHub()
    try:
        return await mark_issue(github, result)
    finally:
        if owned:
            await github.close()


def _device(serial: str) -> Device:
    return Device(serial, adb=tool("adb"))


async def run_qa(params: QaInput, *, github: Any = None, client: Any = None, emulator: Any = None,
                 make_device: Any = _device, make_maestro: Any = Maestro) -> dict[str, Any]:
    """The whole run. The GitHub client, the Mistral client and the emulator are made here unless given (the tests give fakes)."""
    from ..demo.github import GitHub, parse_repo

    repo = parse_repo(params.repo)
    out = out_dir(params.number, params.release)
    owned_github = github is None
    github = github or GitHub()
    try:
        title, criteria, apk = await criteria_and_apk(github, repo, params.number, params.release, out)
    finally:
        if owned_github:
            await github.close()
    if client is None:
        from ..demo.runner import _mistral

        client = _mistral()
    booted: AbstractAsyncContextManager[str]
    if params.serial:
        booted = nullcontext(params.serial)
    else:
        booted = (emulator or Emulator(log=out / "emulator.log")).running()
    async with booted as serial:
        device = make_device(serial)
        await device.clear_logs()
        package, activity = await install(device, apk)
        verdicts = await check_all(client, device, make_maestro(serial), repo, params.number, criteria, package, activity, out, params.model)
    result = summary(repo, params.number, params.release, title, verdicts, out, package)
    if params.label:
        result["issue"] = await _mark(github if not owned_github else None, result)
    write_results(result, out)
    return result


async def _mark(github: Any, result: dict[str, Any]) -> str:
    """mark_issue, with a GitHub client of its own unless one is given; a GitHub error is reported, not raised."""
    from ..demo.github import GitHub, GitHubError

    owned = github is None
    github = github or GitHub()
    try:
        return await mark_issue(github, result)
    except GitHubError as exc:
        return f"Issue not marked: GitHub answered {exc}"
    finally:
        if owned:
            await github.close()
