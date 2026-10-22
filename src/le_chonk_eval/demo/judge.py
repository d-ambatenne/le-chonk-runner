"""The judge: a Mistral model reads the issue and the change, and says which acceptance criteria are met.

Building and testing can't tell whether the feature is there: the old app builds and passes its tests too.
The judge reads the diff between the issue's base commit and the project Vibe left, with the verifier's
results, and writes judge.json next to the trial's result. A trial that changed nothing is not_done, with no model call.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .github import commit_all, git, sync_tree
from .issues import Task

MAX_DIFF_CHARS = 60_000
VERDICTS = ("done", "partial", "not_done")

SYSTEM = """You review a coding agent's change to a Kotlin Multiplatform project against the task it was given.
Judge only from the change and the verifier's results; don't assume code you can't see. A criterion is met only when the change clearly implements it.
Answer with JSON only:
{"criteria": [{"criterion": "<as given>", "met": true or false, "evidence": "<file and what it does, one sentence>"}],
 "verdict": "done" | "partial" | "not_done",
 "summary": "<one or two sentences>"}
"done" means every criterion is met; "not_done" means none is, or the change doesn't address the task."""


def project_diff(base_clone: Path, project: Path) -> dict[str, Any]:
    """The change from the base commit to Vibe's project: changed files, a stat and the diff, cut to size."""
    with tempfile.TemporaryDirectory(prefix="le-chonk-diff-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(base_clone, work, symlinks=True)
        sync_tree(project, work)
        if commit_all(work, "Vibe's change") is None:
            return {"files": [], "stat": "", "diff": "", "truncated": False}
        has_parent = git(["rev-parse", "--verify", "--quiet", "HEAD~1"], cwd=work, check=False).returncode == 0
        base = "HEAD~1" if has_parent else git(["hash-object", "-t", "tree", "/dev/null"], cwd=work).stdout.strip()
        files = git(["diff", "--name-only", base, "HEAD"], cwd=work).stdout.split()
        stat = git(["diff", "--stat", base, "HEAD"], cwd=work).stdout
        diff = git(["diff", base, "HEAD"], cwd=work).stdout
    return {"files": files, "stat": stat, "diff": diff[:MAX_DIFF_CHARS], "truncated": len(diff) > MAX_DIFF_CHARS}


def _verifier_facts(details: dict[str, Any]) -> str:
    assertions = details.get("assertions") or {}

    def mark(key: str) -> str:
        value = assertions.get(key)
        return "not checked" if value is None else ("yes" if value else "no")

    return f"Android APK built: {mark('android_apk')}. Shared tests pass: {mark('shared_tests_pass')}."


def normalize(data: dict[str, Any], task: Task) -> dict[str, Any]:
    wanted = task.criteria or [task.goal or task.title]
    given = data.get("criteria") if isinstance(data.get("criteria"), list) else []
    criteria = []
    for i, criterion in enumerate(wanted):
        found = given[i] if i < len(given) and isinstance(given[i], dict) else {}
        criteria.append({"criterion": criterion, "met": found.get("met") is True, "evidence": str(found.get("evidence") or "")[:300]})
    met = sum(c["met"] for c in criteria)
    verdict = data.get("verdict") if data.get("verdict") in VERDICTS else ("done" if met == len(criteria) else "partial" if met else "not_done")
    if verdict == "done" and met < len(criteria):  # the score wins over a generous verdict
        verdict = "partial" if met else "not_done"
    return {"verdict": verdict, "score": round(met / len(criteria), 4), "criteria": criteria, "summary": str(data.get("summary") or "")[:600]}


async def judge(client: Any, model: str, task: Task, details: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    if not diff["files"]:
        result = normalize({"verdict": "not_done", "summary": "The agent changed nothing."}, task)
        return {**result, "model": None, "files_changed": 0}
    criteria = "\n".join(f"- {c}" for c in task.criteria) or f"- {task.goal or task.title}"
    user = (f"# Task: {task.title}\n\n{task.goal}\n\nAcceptance criteria:\n{criteria}\n\n# Verifier\n{_verifier_facts(details)}\n\n"
            f"# Change ({len(diff['files'])} files)\n{diff['stat']}\n```diff\n{diff['diff']}\n```"
            + ("\n(The diff was cut to fit.)" if diff["truncated"] else ""))
    response = await client.chat.complete_async(
        # Greedy, so a verdict doesn't change between runs; Mistral refuses temperature 0 unless top_p is 1 (FRICTIONS.md, row 38).
        model=model.split("/", 1)[-1], temperature=0, top_p=1, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    )
    content = response.choices[0].message.content
    text = content if isinstance(content, str) else "".join(getattr(part, "text", "") for part in content or [])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"summary": f"The judge's answer wasn't JSON: {text[:200]}"}
    usage = getattr(response, "usage", None)
    return {**normalize(data if isinstance(data, dict) else {}, task), "model": model, "files_changed": len(diff["files"]),
            "prompt_tokens": getattr(usage, "prompt_tokens", None), "completion_tokens": getattr(usage, "completion_tokens", None)}


async def judge_trial(client: Any, model: str, task: Task, trial_dir: Path, base_clone: Path) -> dict[str, Any] | None:
    """Judge one trial and write its judge.json; None for a trial without a project or a verifier result."""
    project = trial_dir / "artifacts" / "project"
    details_path = trial_dir / "verifier" / "details.json"
    if not project.is_dir() or not details_path.is_file():
        return None
    details = json.loads(details_path.read_text())
    diff = await asyncio.to_thread(project_diff, base_clone, project)
    result = await judge(client, model, task, details, diff)
    (trial_dir / "judge.json").write_text(json.dumps(result, indent=2))
    return result
