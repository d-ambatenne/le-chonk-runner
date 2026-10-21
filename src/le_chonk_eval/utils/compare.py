"""Compare the two arms' Harbor jobs: read every trial, then write the summary and the dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .results import read_job
from .paths import results_dir
from .report import write_dashboard
from .stats import summarize


def compare(job_dirs: list[Path], ab_id: str | None = None, **labels: str) -> dict[str, Any]:
    """Writes results/<ab id>/{summary.json, runs.jsonl, dashboard.html} and returns the summary.

    labels: the dashboard's title, agent_label and reward_note, for A/Bs other than the Le Chonk Runner task."""
    ab_id = ab_id or _common_prefix([d.name for d in job_dirs]) or job_dirs[0].name
    out = results_dir() / ab_id
    out.mkdir(parents=True, exist_ok=True)
    runs = [run for job_dir in job_dirs for run in read_job(job_dir)]
    summary = summarize(ab_id, runs)
    summary.update({k: v for k, v in labels.items() if v})
    summary["dashboard"] = str(out / "dashboard.html")
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (out / "runs.jsonl").open("w", encoding="utf-8") as handle:
        for run in runs:
            handle.write(json.dumps(run, default=str) + "\n")
    write_dashboard(summary, runs, out / "dashboard.html")
    return summary


def _common_prefix(names: list[str]) -> str:
    if len(names) < 2:
        return ""
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix):
            prefix = prefix[:-1]
    return prefix.rstrip("-_")


def compact(summary: dict[str, Any]) -> dict[str, Any]:
    """What a workflow returns: the headline numbers, not every reward."""
    return {
        "ab_id": summary["ab_id"],
        "model": summary["model"],
        "dashboard": summary.get("dashboard"),
        "arms": {
            arm: {k: s.get(k) for k in ("n", "set_aside", "reward_mean", "reward_ci95", "judge_score_mean", "cost_mean", "cost_total")}
            for arm, s in summary["arms"].items()
        },
        "comparison": summary["comparison"],
    }
