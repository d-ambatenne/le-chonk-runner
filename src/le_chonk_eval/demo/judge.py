"""The judge: a Mistral model reads the issue and the change, and says which acceptance criteria are met.

Building and testing can't tell whether the feature is there: the old app builds and passes its tests too.
The judge reads the diff between the issue's base commit and the project Vibe left, with the verifier's
results, and writes judge.json next to the trial's result. A trial that changed nothing is not_done, with no model call.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .github import commit_all, git, sync_tree
from .issues import Task

MAX_DIFF_CHARS = 120_000
VERDICTS = ("done", "partial", "not_done")

# Named for the judge but not shown: a new project's diff is mostly these, and they came before its code, so the code was cut
SKIPPED_NAMES = {"gradlew", "gradlew.bat", "kotlin", "kotlin.bat", "amper", "amper.bat", "mvnw", "mvnw.cmd", "package-lock.json",
                 "yarn.lock", "pnpm-lock.yaml", "gradle.lockfile", "Podfile.lock", "Cargo.lock"}
SKIPPED_PARTS = ("/gradle/wrapper/", ".xcodeproj/", ".xcworkspace/", ".xcassets/", "/.idea/", "/mipmap-", "/ic_launcher")
BINARY = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".ttf", ".otf", ".jar", ".aar", ".so", ".apk", ".mp3", ".wav", ".ogg")
CODE = (".kt", ".java", ".swift", ".m", ".mm", ".c", ".cc", ".cpp", ".h", ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".dart")
TESTS = ("/test/", "/tests/", "/test@", "/commonTest/", "/androidTest/", "/androidUnitTest/", "/jvmTest/", "/iosTest/")
HEADER = re.compile(r'^diff --git "?a/(.*?)"? "?b/(.*?)"?$')
MAX_NAMES = 60
MAX_STAT_FILES = 300

SYSTEM = """You review a coding agent's change to a Kotlin Multiplatform project against the task it was given.
Judge only from the change and the verifier's results; don't assume code you can't see. A criterion is met only when the change clearly implements it.
The diff shows the code first, then the tests, then the other files. Generated, IDE and binary files, and files that didn't fit, are only named under it: when a criterion's code could be in one of them, say so in its evidence.
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
        diff = git(["diff", base, "HEAD"], cwd=work).stdout
    return judge_diff(diff)


def split_diff(diff: str) -> list[tuple[str, str]]:
    """A unified diff's files, in order: (the file's new path, its part of the diff)."""
    chunks: list[tuple[str, str]] = []
    path: str | None = None
    lines: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path is not None:
                chunks.append((path, "".join(lines)))
            found = HEADER.match(line.rstrip("\r\n"))
            path, lines = (found.group(2) if found else line[len("diff --git "):].strip()), [line]
        elif path is not None:
            lines.append(line)
    if path is not None:
        chunks.append((path, "".join(lines)))
    if not chunks and diff.strip():  # not a git diff: shown as it is, rather than read as no change
        chunks.append(("(the diff)", diff))
    return chunks


def skipped(path: str, text: str) -> bool:
    """A wrapper, a lock file, an IDE project, an icon or a binary file: the judge gets its name only."""
    where = "/" + path
    return (path.rsplit("/", 1)[-1] in SKIPPED_NAMES or any(part in where for part in SKIPPED_PARTS)
            or path.lower().endswith(BINARY) or "\nBinary files " in text or "\nGIT binary patch" in text)


def counts(text: str) -> tuple[int, int]:
    """Lines added and removed in one file's part of a diff: only lines after its first hunk header count."""
    added = removed = 0
    in_hunks = False
    for line in text.splitlines():
        if line.startswith("@@"):
            in_hunks = True
        elif in_hunks and line.startswith("+"):
            added += 1
        elif in_hunks and line.startswith("-"):
            removed += 1
    return added, removed


def _names(paths: list[str]) -> str:
    more = f", and {len(paths) - MAX_NAMES} more" if len(paths) > MAX_NAMES else ""
    return ", ".join(paths[:MAX_NAMES]) + more


def judge_diff(diff: str, max_chars: int = MAX_DIFF_CHARS) -> dict[str, Any]:
    """A unified diff as the judge reads it: the code, then the tests, then the other files, smallest first, as much as fits.

    Wrappers, lock files, IDE projects, icons and binaries are only named, in left_out; files that didn't fit, in not_shown."""
    chunks = split_diff(diff)
    code: list[tuple[str, str]] = []
    tests: list[tuple[str, str]] = []
    rest: list[tuple[str, str]] = []
    left_out: list[str] = []
    for path, text in chunks:
        if skipped(path, text):
            left_out.append(path)
        elif path.endswith(CODE):
            (tests if any(part in "/" + path for part in TESTS) else code).append((path, text))
        else:
            rest.append((path, text))
    shown: list[str] = []
    not_shown: list[str] = []
    room = max_chars
    for path, text in [*code, *tests, *sorted(rest, key=lambda chunk: len(chunk[1]))]:
        if len(text) <= room:
            shown.append(text)
            room -= len(text)
        else:
            not_shown.append(path)
    lines, total_added, total_removed = [], 0, 0
    for path, text in chunks:
        added, removed = counts(text)
        total_added, total_removed = total_added + added, total_removed + removed
        lines.append(f" {path} | " + ("binary" if path in left_out and not added and not removed else f"+{added} -{removed}"))
    if len(lines) > MAX_STAT_FILES:
        lines = lines[:MAX_STAT_FILES] + [f" ... and {len(lines) - MAX_STAT_FILES} more files"]
    stat = "\n".join([*lines, f" {len(chunks)} files changed, {total_added} insertions(+), {total_removed} deletions(-)"]) if chunks else ""
    return {"files": [path for path, _ in chunks], "stat": stat, "diff": "".join(shown), "left_out": left_out, "not_shown": not_shown,
            "truncated": bool(not_shown)}


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


def unseen(diff: dict[str, Any]) -> str:
    """What the judge didn't see of the change, under the diff."""
    notes = ""
    if diff.get("left_out"):
        notes += f"\nNamed only, as generated, IDE or binary files: {_names(diff['left_out'])}."
    if diff.get("not_shown"):
        notes += f"\nNot shown, to fit: {_names(diff['not_shown'])}."
    elif diff.get("truncated"):
        notes += "\n(The diff was cut to fit.)"
    return notes


async def judge(client: Any, model: str, task: Task, details: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    if not diff["files"]:
        result = normalize({"verdict": "not_done", "summary": "The agent changed nothing."}, task)
        return {**result, "model": None, "files_changed": 0}
    criteria = "\n".join(f"- {c}" for c in task.criteria) or f"- {task.goal or task.title}"
    user = (f"# Task: {task.title}\n\n{task.goal}\n\nAcceptance criteria:\n{criteria}\n\n# Verifier\n{_verifier_facts(details)}\n\n"
            f"# Change ({len(diff['files'])} files)\n{diff['stat']}\n```diff\n{diff['diff']}\n```" + unseen(diff))
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
