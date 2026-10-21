"""What the cloud does with a run's result from your Mac: the PR, the judge, the merge, the labels and the replies.

The Mac pushed the branch and released the APK. Here, with the GitHub and Slack connectors: a PR from the
branch, the judge on the PR's diff, a squash merge, a comment and labels on the issue, a message in the thread.
The rule is the demo's: merged when the APK builds and the tests pass; the judge decides whether the issue is done.
"""

from __future__ import annotations

from typing import Any

from ..demo.issues import AB_DONE, AB_RUNNING, DONE, FAILED, NOT_DONE, RUNNING, Task
from ..demo.judge import MAX_DIFF_CHARS, judge
from ..demo.publish import _judge_md, failure_line, failure_markdown
from .connectors import ConnectorGitHub, ConnectorNotifier, diff_summary


def _links(pr: dict[str, Any], result: dict[str, Any]) -> tuple[str, str]:
    """The PR, release and APK links, in a GitHub comment and in a Slack message."""
    comment = f"PR #{pr['number']} ({pr.get('html_url')})" + (f", release {result['release']}" if result.get("release") else "") + (
        f", APK {result['apk']}" if result.get("apk") else "")
    slack = f"[PR #{pr['number']}]({pr.get('html_url')})" + (f", [APK]({result['apk']})" if result.get("apk") else "")
    return comment, slack


async def merge_run(github: ConnectorGitHub, notifier: ConnectorNotifier, judge_client: Any, judge_model: str, repo: str, number: int,
                    task: Task, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Merge Vibe's change when it passed its checks, with the judge's verdict on the issue. Safe to retry."""
    facts = result.get("facts") or ""
    note = f" {result['note']}" if result.get("note") else ""
    if not result.get("passed"):
        why, failure = result.get("reason") or "the checks failed", result.get("failure")
        await github.comment(repo, number, f"Vibe's run `{run_id}`: {why}, so nothing was merged. {facts}.{note}\n\n"
                                           f"{failure_markdown(failure)}\n\nAsk the triage agent in Slack to queue it again.")
        await github.relabel(repo, number, add=[FAILED], remove=[RUNNING])
        await notifier.post(task.slack_channel, task.slack_thread, f"#{number} {task.title}: {why}, nothing merged. {facts}. "
                                                                   f"{failure_line(failure)}".strip())
        return {"merged": False, "reason": why}
    if not result.get("changed"):
        await github.comment(repo, number, f"Vibe's run `{run_id}` changed nothing. {facts}.")
        await github.relabel(repo, number, add=[NOT_DONE], remove=[RUNNING])
        await notifier.post(task.slack_channel, task.slack_thread, f"#{number} {task.title}: Vibe changed nothing. {facts}.")
        return {"merged": False, "reason": "no change"}

    branch = result["branch"]
    pr = await github.find_pull(repo, branch)
    if pr is None:
        pr = await github.create_pull(repo, f"#{number}: {task.title}", branch, result.get("base_branch") or "main",
                                      f"Vibe's change for #{number}, run `{run_id}`. The APK builds and the shared tests pass. {facts}."
                                      + (f"\n\nRelease with the APK: {result['release']}" if result.get("release") else ""))
    verdict = await judge(judge_client, judge_model, task, {"assertions": result.get("assertions") or {}},
                          diff_summary(await github.pull_diff(repo, pr["number"]), MAX_DIFF_CHARS))
    done = verdict["verdict"] == "done"
    if not pr.get("merged"):
        await github.merge_pull(repo, pr["number"], f"#{number}: {task.title} (#{pr['number']})")
    comment_links, slack_links = _links(pr, result)
    await github.comment(repo, number, f"Merged Vibe's change: {comment_links}. {facts}.{note}\n\n{_judge_md(verdict)}")
    await github.relabel(repo, number, add=[DONE if done else NOT_DONE], remove=[RUNNING])
    if done:
        await github.update_issue(repo, number, state="closed", state_reason="completed")
    await notifier.post(task.slack_channel, task.slack_thread,
                        f"#{number} {task.title}: merged in {slack_links}. Judge: {verdict['verdict'].replace('_', ' ')}, "
                        f"{verdict['score']:.0%} of the criteria. {facts}."
                        + ("" if done else " The issue stays open: tell me what to change, or queue it again."))
    return {"merged": True, "pull": pr["number"], "release": result.get("release"), "apk": result.get("apk"), "done": done,
            "verdict": verdict["verdict"]}


async def report_ab(github: ConnectorGitHub, notifier: ConnectorNotifier, repo: str, number: int, task: Task, run_id: str,
                    result: dict[str, Any]) -> dict[str, Any]:
    """The A/B's result from your Mac, on the issue and in the thread."""
    errors = result.get("errors") or []
    apks = "".join(f"\n- {arm}: {url}" for arm, url in (result.get("apks") or {}).items())
    await github.comment(repo, number, f"Skill A/B `{run_id}`: {result.get('headline')}\n\n{result.get('table')}\n\n"
                                       f"Report and APKs: {result.get('release')}{apks}")
    await github.relabel(repo, number, add=[AB_DONE], remove=[AB_RUNNING])
    await notifier.post(task.slack_channel, task.slack_thread,
                        f"A/B for #{number} {task.title}: {result.get('headline')} [Report and APKs]({result.get('release')})."
                        + (f" {len(errors)} job(s) failed: see the issue." if errors else ""))
    return {"release": result.get("release"), "headline": result.get("headline"), "errors": errors}
