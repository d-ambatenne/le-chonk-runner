"""The next phase's runs, in Mistral's cloud and on your Mac: what they did and where they failed. Read-only.

    uv run python scripts/status_next.py                    # the latest runs of the four workflows
    uv run python scripts/status_next.py --execution <id>   # one run

For each run: its status, its steps (activities) and their failures, and its warnings and errors. It reads
MISTRAL_API_KEY from .env, like the rest of the project, and prints no secrets. Each run's history and logs are
saved to runs/status/<execution>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mistralai.client import Mistral

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ["le-chonk-slack", "le-chonk-tasks", "le-chonk-vibe-run", "le-chonk-vibe-ab"]


def dump(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value


def failure(error: Exception) -> str:
    status = getattr(error, "status_code", None)
    body = " ".join(str(getattr(error, "body", None) or error).split())
    return f"{type(error).__name__}{f' {status}' if status else ''}: {body[:300]}"


def key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def find(data: Any, *names: str) -> Any:
    """A field by name, whatever its case: eventType, event_type."""
    if isinstance(data, dict):
        wanted = {key(n) for n in names}
        for k, v in data.items():
            if key(k) in wanted:
                return v
    return None


def events_of(history: Any) -> list[dict[str, Any]]:
    if isinstance(history, list):
        return [e for e in history if isinstance(e, dict)]
    for name in ("events", "history", "data"):
        inner = find(history, name)
        if isinstance(inner, (list, dict)) and inner is not history:
            found = events_of(inner)
            if found:
                return found
    return []


def attributes(event: dict[str, Any]) -> dict[str, Any]:
    return next((v for k, v in event.items() if key(k).endswith("attributes") and isinstance(v, dict)), {})


def message_of(failure_info: Any) -> str:
    """A Temporal failure's message, with its causes."""
    parts = []
    while isinstance(failure_info, dict) and len(parts) < 4:
        text = find(failure_info, "message")
        if text:
            parts.append(str(text))
        failure_info = find(failure_info, "cause")
    return " <- ".join(parts)[:600]


def summarize(history: Any) -> dict[str, Any]:
    names: dict[str, str] = {}
    steps: Counter[str] = Counter()
    failures: list[str] = []
    last: list[str] = []
    for event in events_of(history):
        kind = str(find(event, "eventType", "event_type") or "?").removeprefix("EVENT_TYPE_")
        attrs = attributes(event)
        event_id = str(find(event, "eventId", "event_id") or "")
        name = ""
        activity_type = find(attrs, "activityType", "activity_type")
        if activity_type:
            name = str(find(activity_type, "name") or activity_type)
            names[event_id] = name
        scheduled = find(attrs, "scheduledEventId", "scheduled_event_id")
        if scheduled is not None:
            name = names.get(str(scheduled), name)
        if "ACTIVITY" in kind.upper() or "Activity" in kind:
            steps[f"{name or '?'} {kind.split('_')[-1].lower() if '_' in kind else kind}"] += 1
        problem = find(attrs, "failure")
        if problem or "FAILED" in kind.upper() or "TIMED_OUT" in kind.upper():
            failures.append(f"{find(event, 'eventTime', 'event_time') or ''} {kind} {name}: {message_of(problem) or json.dumps(attrs)[:300]}")
        last.append(f"{find(event, 'eventTime', 'event_time') or ''} {kind} {name}".strip())
    return {"events": len(events_of(history)), "steps": dict(steps), "failures": failures, "last": last[-12:]}


def report(client: Mistral, execution: str, out: Path) -> None:
    saved: dict[str, Any] = {"execution": execution}
    try:
        run = dump(client.workflows.executions.get_workflow_execution(execution_id=execution))
        saved["run"] = {k: v for k, v in run.items() if k != "result"}
        status = str(run.get("status")).rsplit(".", 1)[-1]
        print(f"\n{execution}: {status}, started {run.get('start_time')}, ended {run.get('end_time') or '-'}")
        if run.get("result") is not None:
            saved["result"] = run["result"]
    except Exception as error:  # noqa: BLE001 - every failure is part of the report
        print(f"\n{execution}: can't read it: {failure(error)}")
    try:
        history = dump(client.workflows.executions.get_workflow_execution_history(execution_id=execution))
        saved["history"] = history
        summary = summarize(history)
        print(f"  {summary['events']} events; steps: " + ", ".join(f"{k} x{v}" for k, v in sorted(summary["steps"].items())))
        for line in summary["failures"][-10:]:
            print(f"  FAILED {line}")
        print("  last: " + " | ".join(summary["last"][-6:]))
    except Exception as error:  # noqa: BLE001
        print(f"  history: {failure(error)}")
    try:
        logs = dump(client.workflows.executions.get_workflow_execution_logs(execution_id=execution, order="desc", limit=100))
        records = logs.get("results") or []
        saved["logs"] = records
        notable = [r for r in records if str(r.get("severity_text", "")).upper() in ("WARN", "WARNING", "ERROR", "CRITICAL", "FATAL")]
        for record in notable[:10]:
            print(f"  {record.get('severity_text')} {record.get('timestamp')}: {' '.join(str(record.get('body')).split())[:400]}")
        if not notable:
            print(f"  logs: {len(records)} lines, no warnings or errors")
    except Exception as error:  # noqa: BLE001
        print(f"  logs: {failure(error)}")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{execution}.json").write_text(json.dumps(saved, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execution", help="one run's execution ID")
    parser.add_argument("--runs", type=int, default=2, help="how many of each workflow's latest runs (default 2)")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not os.environ.get("MISTRAL_API_KEY"):
        print("No MISTRAL_API_KEY in .env")
        return 1
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    out = ROOT / "runs" / "status"
    executions = [args.execution] if args.execution else []
    if not executions:
        for workflow in WORKFLOWS:
            try:
                page = client.workflows.runs.list_runs(workflow_identifier=workflow, order="desc", page_size=args.runs)
                found = list(page.result.executions)[:args.runs] if page is not None else []
            except Exception as error:  # noqa: BLE001
                print(f"{workflow}: can't list its runs: {failure(error)}")
                continue
            print(f"{workflow}: " + (", ".join(f"{e.execution_id} ({str(e.status).rsplit('.', 1)[-1]})" for e in found) or "no runs"))
            executions += [e.execution_id for e in found]
    for execution in executions:
        report(client, execution, out)
    print(f"\nSaved to runs/status/: {len(executions)} file{'' if len(executions) == 1 else 's'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
