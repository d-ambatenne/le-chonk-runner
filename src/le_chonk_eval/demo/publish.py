"""What a run leaves behind: on GitHub, a merged PR, a release with the APK and a comment; in Slack, a message.

A run is merged when its APK builds and its tests pass. The judge decides what happens to the issue: closed
when the task is done, open with vibe:not-done otherwise. An A/B merges nothing: its pre-release holds the report
and one APK per arm, so both can be tried on a phone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .github import GitHub, commit_all, push, reset_to, sync_tree
from .issues import AB_DONE, AB_FAILED, AB_REQUESTED, AB_RUNNING, DONE, FAILED, NOT_DONE, READY, RUNNING, Task
from .notify import Notifier

APK_TYPE = "application/vnd.android.package-archive"


def _facts(run: dict[str, Any]) -> str:
    parts = []
    if run.get("cost_usd") is not None:
        parts.append(f"${run['cost_usd']:.2f}")
    if run.get("agent_seconds") is not None:
        parts.append(f"{run['agent_seconds'] / 60:.0f} min of agent time")
    tokens = (run.get("input_tokens") or 0) + (run.get("output_tokens") or 0)
    if tokens:
        parts.append(f"{tokens:,} tokens")
    return ", ".join(parts)


def _judge_md(judge: dict[str, Any] | None) -> str:
    if not judge:
        return "The judge didn't run."
    rows = "\n".join(f"| {'met' if c['met'] else 'not met'} | {c['criterion']} | {c.get('evidence') or ''} |" for c in judge["criteria"])
    return (f"**Judge: {judge['verdict'].replace('_', ' ')}** ({sum(c['met'] for c in judge['criteria'])} of {len(judge['criteria'])} "
            f"criteria). {judge.get('summary') or ''}\n\n| | Criterion | Evidence |\n|---|---|---|\n{rows}")


def _apk(run: dict[str, Any]) -> Path | None:
    if not run.get("apk") or not run.get("trial_dir"):
        return None
    path = Path(run["trial_dir"]) / "verifier" / run["apk"]
    return path if path.is_file() else None


def _why_not_merged(run: dict[str, Any]) -> str:
    a = run.get("assertions") or {}
    if run.get("reward") is None:
        return f"the run stopped before the checks ({run.get('error') or 'no verifier result'})"
    if not a.get("android_apk"):
        return "the Android APK didn't build"
    if not a.get("shared_tests_pass"):
        return "the shared tests failed"
    return "Vibe's project wasn't saved"


async def publish_run(github: GitHub, notifier: Notifier, repo: str, number: int, task: Task, run_id: str, run: dict[str, Any],
                      clone_dir: Path, branch: str, token: str | None, base_commit: str) -> dict[str, Any]:
    """After one run of Vibe on an issue: merge it if it builds and passes its tests, release the APK, report.

    Safe to retry: the clone goes back to the base commit first, and an existing PR or release is reused."""
    judge = run.get("judge")
    a = run.get("assertions") or {}
    project = Path(run["project_dir"]) if run.get("project_dir") else None
    facts = _facts(run)
    if not (a.get("android_apk") and a.get("shared_tests_pass") and project and project.is_dir()):
        why = _why_not_merged(run)
        await github.comment(repo, number, f"Vibe's run `{run_id}`: {why}, so nothing was merged. {facts}.\n\n{_judge_md(judge)}\n\n"
                                           "Ask the triage agent in Slack to queue it again.")
        await github.relabel(repo, number, add=[FAILED], remove=[RUNNING])
        await notifier.post(task.slack_channel, task.slack_thread, f"#{number} {task.title}: {why}, nothing merged. {facts}.")
        return {"merged": False, "reason": why}

    await asyncio.to_thread(reset_to, clone_dir, base_commit)
    await asyncio.to_thread(sync_tree, project, clone_dir)
    commit = await asyncio.to_thread(commit_all, clone_dir, f"#{number}: {task.title}\n\nImplemented by Vibe, run {run_id}.")
    pr = await github.find_pull(repo, branch)
    if commit is None and pr is None:
        await github.comment(repo, number, f"Vibe's run `{run_id}` changed nothing. {facts}.")
        await github.relabel(repo, number, add=[NOT_DONE], remove=[RUNNING])
        await notifier.post(task.slack_channel, task.slack_thread, f"#{number} {task.title}: Vibe changed nothing. {facts}.")
        return {"merged": False, "reason": "no change"}

    info = await github.repo(repo)
    base = info.get("default_branch") or "main"
    done = bool(judge and judge.get("verdict") == "done")
    merged_already = bool(pr and (pr.get("merged") or pr.get("merged_at")))
    if not merged_already:
        await asyncio.to_thread(push, clone_dir, branch, token)
    if pr is None:
        pr = await github.create_pull(repo, f"#{number}: {task.title}", branch, base,
                                      f"Vibe's change for #{number}, run `{run_id}`. The APK builds and the shared tests pass. "
                                      f"{facts}.\n\n{_judge_md(judge)}")
    if merged_already:
        sha = pr.get("merge_commit_sha")
    else:
        sha = (await github.merge_pull(repo, pr["number"], f"#{number}: {task.title} (#{pr['number']})")).get("sha")
    release = await github.ensure_release(
        repo, run_id, sha or base, f"#{number}: {task.title}",
        f"Built by Vibe for #{number} and merged in #{pr['number']}. {facts}.\n\n{_judge_md(judge)}",
    )
    apk_url = None
    apk = _apk(run)
    if apk:
        asset = await github.upload_asset(repo, release, apk, f"le-chonk-issue-{number}.apk", APK_TYPE)
        apk_url = asset.get("browser_download_url")
    links = f"PR #{pr['number']} ({pr.get('html_url')}), release {release.get('html_url')}" + (f", APK {apk_url}" if apk_url else "")
    await github.comment(repo, number, f"Merged Vibe's change: {links}. {facts}.\n\n{_judge_md(judge)}")
    await github.relabel(repo, number, add=[DONE if done else NOT_DONE], remove=[RUNNING])
    if done:
        await github.update_issue(repo, number, state="closed", state_reason="completed")
    verdict = f"judge: {judge['verdict'].replace('_', ' ')}, {judge['score']:.0%} of the criteria" if judge else "no judge"
    await notifier.post(task.slack_channel, task.slack_thread,
                        f"#{number} {task.title}: merged in <{pr.get('html_url')}|PR #{pr['number']}>"
                        + (f", APK: <{apk_url}|download>" if apk_url else "") + f". {verdict}. {facts}."
                        + ("" if done else " The issue stays open: tell me what to change, or queue it again."))
    return {"merged": True, "pull": pr["number"], "release": release.get("html_url"), "apk": apk_url, "done": done}


def _ab_row(arm: str, s: dict[str, Any]) -> str:
    reward = "n/a" if s.get("reward_mean") is None else f"{s['reward_mean']:.2f}"
    judged = "n/a" if s.get("judge_score_mean") is None else f"{s['judge_score_mean']:.0%}"
    return f"| {arm} | {s.get('n', 0)} | {reward} | {judged} | ${s.get('cost_mean') or 0:.2f} |"


def _best(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    built = [r for r in runs if _apk(r) is not None]
    return max(built, key=lambda r: r.get("reward") or 0) if built else None


def ab_text(summary: dict[str, Any], errors: list[str] | None = None) -> tuple[str, str]:
    """The A/B's headline and its table of arms, in Markdown."""
    arms = summary.get("arms") or {}
    comparison = summary.get("comparison") or {}
    rows = "\n".join(_ab_row(arm, stats) for arm, stats in arms.items())
    delta = comparison.get("reward_delta")
    headline = (f"With the skill, mean reward {arms['skill']['reward_mean']} against {arms['no-skill']['reward_mean']} without "
                f"(Δ {delta:+.2f}, p {comparison.get('reward_p_value')})." if delta is not None else "One arm has no scored run yet.")
    table = f"| Arm | Runs | Mean reward | Criteria met | Cost per run |\n|---|---|---|---|---|\n{rows}"
    if errors:
        table += "\n\nJobs that failed: " + "; ".join(e[:300] for e in errors)
    return headline, table


async def release_ab(github: GitHub, repo: str, number: int, task: Task, ab_id: str, summary: dict[str, Any],
                     runs: list[dict[str, Any]], target: str, errors: list[str] | None = None) -> dict[str, Any]:
    """The A/B's pre-release: the report and the best APK of each arm. Safe to retry: the release and its files are reused."""
    headline, table = ab_text(summary, errors)
    release = await github.ensure_release(repo, ab_id, target, f"A/B for #{number}: {task.title}",
                                          f"{headline}\n\n{table}\n\nReward = mean of: APK builds, tests pass, judge's share of criteria met.",
                                          prerelease=True)
    report = Path(summary["dashboard"]) if summary.get("dashboard") else None
    if report and report.is_file():
        await github.upload_asset(repo, release, report, "ab-report.html", "text/html")
    apks = {}
    for arm in summary.get("arms") or {}:
        best = _best([r for r in runs if r["arm"] == arm])
        if best:
            asset = await github.upload_asset(repo, release, _apk(best), f"le-chonk-issue-{number}-{arm}.apk", APK_TYPE)
            apks[arm] = asset.get("browser_download_url")
    return {"release": release.get("html_url"), "apks": apks, "headline": headline, "table": table,
            "report": str(report) if report and report.is_file() else None}


async def publish_ab(github: GitHub, notifier: Notifier, repo: str, number: int, task: Task, ab_id: str, summary: dict[str, Any],
                     runs: list[dict[str, Any]], target: str, errors: list[str] | None = None) -> dict[str, Any]:
    """After the A/B of an issue: a pre-release with the report and the best APK of each arm, a comment, a Slack message.
    Safe to retry: the release and its files are reused."""
    released = await release_ab(github, repo, number, task, ab_id, summary, runs, target, errors)
    headline, table, apks = released["headline"], released["table"], released["apks"]
    release = {"html_url": released["release"]}
    report = Path(released["report"]) if released["report"] else None
    apk_md = "".join(f"\n- {arm}: {url}" for arm, url in apks.items())
    await github.comment(repo, number, f"Skill A/B `{ab_id}`: {headline}\n\n{table}\n\nReport and APKs: {release.get('html_url')}{apk_md}")
    await github.relabel(repo, number, add=[AB_DONE], remove=[AB_RUNNING])
    message = f"A/B for #{number} {task.title}: {headline} <{release.get('html_url')}|Report and APKs>." + (
        f" {len(errors)} job(s) failed: see the issue." if errors else "")
    if report and report.is_file():
        await notifier.upload(task.slack_channel, task.slack_thread, report, f"A/B #{number}", message)
    else:
        await notifier.post(task.slack_channel, task.slack_thread, message)
    return {"release": release.get("html_url"), "apks": apks, "headline": headline}


async def report_failure(github: GitHub, notifier: Notifier, repo: str, number: int, task: Task | None, kind: str, run_id: str,
                         error: str) -> None:
    """A run that broke before it could publish: say so where people look, and free the issue."""
    await github.comment(repo, number, f"Run `{run_id}` failed: {error[:1500]}\n\nAsk the triage agent in Slack to queue it again.")
    if kind == "ab":
        await github.relabel(repo, number, add=[AB_FAILED], remove=[AB_RUNNING, AB_REQUESTED])
    else:
        await github.relabel(repo, number, add=[FAILED], remove=[RUNNING, READY])
    if task:
        await notifier.post(task.slack_channel, task.slack_thread, f"#{number} {task.title}: the {'A/B' if kind == 'ab' else 'run'} failed: {error[:300]}")
