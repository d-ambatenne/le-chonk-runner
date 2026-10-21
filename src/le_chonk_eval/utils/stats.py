"""A/B statistics: the no-skill and skill arms of one model."""

from __future__ import annotations

import itertools
import math
import random
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Any

from .results import ARMS, ASSERTIONS, LABELS, TASK_DONE


def _mean(values: list[float]) -> float | None:
    return round(mean(values), 4) if values else None


def _ci95(values: list[float]) -> list[float] | None:
    if len(values) < 2:
        return None
    half = 1.96 * stdev(values) / math.sqrt(len(values))
    m = mean(values)
    return [round(max(0.0, m - half), 4), round(min(1.0, m + half), 4)]


def permutation_p_value(a: list[float], b: list[float], iterations: int = 20_000) -> float | None:
    """Two-sided permutation test on the difference of means; exact when small enough."""
    if not a or not b:
        return None
    observed = abs(mean(b) - mean(a))
    pooled = a + b
    n_a = len(a)
    total = sum(pooled)
    extreme = count = 0
    if math.comb(len(pooled), n_a) <= 50_000:
        for idx in itertools.combinations(range(len(pooled)), n_a):
            sum_a = sum(pooled[i] for i in idx)
            diff = abs((total - sum_a) / len(b) - sum_a / n_a)
            extreme += diff >= observed - 1e-12
            count += 1
    else:
        rng = random.Random(7)
        for _ in range(iterations):
            rng.shuffle(pooled)
            diff = abs(mean(pooled[n_a:]) - mean(pooled[:n_a]))
            extreme += diff >= observed - 1e-12
            count += 1
    return round(extreme / count, 4)


def scored(run: dict[str, Any]) -> bool:
    """A trial counts when the verifier ran on a healthy machine. Agent failures and timeouts still count."""
    return run.get("reward") is not None and not run.get("environment_errors")


def _rate(runs: list[dict[str, Any]], key: str) -> float | None:
    known = [bool(r["assertions"][key]) for r in runs if r["assertions"].get(key) is not None]
    return round(sum(known) / len(known), 4) if known else None


def _avg(runs: list[dict[str, Any]], key: str, process: bool = False) -> float | None:
    values = [(r.get("process") or {}).get(key) if process else r.get(key) for r in runs]
    return _mean([float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)])


def _share(runs: list[dict[str, Any]], key: str) -> float | None:
    values = [(r.get("process") or {}).get(key) for r in runs if r.get("process")]
    return _mean([1.0 if v else 0.0 for v in values])


def arm_stats(runs: list[dict[str, Any]], keys: list[str] = ASSERTIONS) -> dict[str, Any]:
    counted = [r for r in runs if scored(r)]
    rewards = [float(r["reward"]) for r in counted]
    judged = [float(r["judge"]["score"]) for r in counted if r.get("judge")]
    return {
        "n": len(counted),
        "trials": len(runs),
        "set_aside": len(runs) - len(counted),
        "agent_errors": sum(1 for r in counted if r.get("error")),
        "stopped_at_limit": sum(1 for r in counted if r.get("stop_reason") in ("max_cost", "max_turns")),
        "reward_mean": _mean(rewards),
        "reward_sd": round(stdev(rewards), 4) if len(rewards) > 1 else None,
        "reward_ci95": _ci95(rewards),
        "rewards": rewards,
        "assertion_rates": {key: _rate(counted, key) for key in keys},
        "judge_score_mean": _mean(judged),
        "cost_mean": _avg(counted, "cost_usd"),
        "cost_total": round(sum(float(r.get("cost_usd") or 0) for r in runs), 4),
        "agent_seconds_mean": _avg(counted, "agent_seconds"),
        "input_tokens_mean": _avg(counted, "input_tokens"),
        "output_tokens_mean": _avg(counted, "output_tokens"),
        "model_calls_mean": _avg(counted, "model_calls", process=True),
        "tool_calls_mean": _avg(counted, "tool_calls", process=True),
        "shell_commands_mean": _avg(counted, "shell_commands", process=True),
        "gradle_calls_mean": _avg(counted, "gradle_calls", process=True),
        "kotlin_calls_mean": _avg(counted, "kotlin_calls", process=True),
        "kotlin_scaffold_rate": _share(counted, "kotlin_scaffold_used"),
        "skill_md_read_rate": _share(counted, "skill_md_read"),
    }


def summarize(ab_id: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    models = sorted({r["model"] for r in runs})
    if len(models) > 1:
        raise ValueError(f"The jobs use different models ({', '.join(models)}): compare one model at a time.")
    keys = ([TASK_DONE] if any(r.get("judge") for r in runs) else []) + ASSERTIONS
    arms = {arm: arm_stats([r for r in runs if r["arm"] == arm], keys) for arm in ARMS if any(r["arm"] == arm for r in runs)}
    comparison = None
    if len(arms) == 2:
        a, b = arms["no-skill"], arms["skill"]

        def delta(key: str) -> float | None:
            return round(b[key] - a[key], 4) if a[key] is not None and b[key] is not None else None

        comparison = {
            "reward_delta": delta("reward_mean"),
            "reward_p_value": permutation_p_value(a["rewards"], b["rewards"]),
            "cost_delta": delta("cost_mean"),
            "agent_seconds_delta": delta("agent_seconds_mean"),
            "input_tokens_delta": delta("input_tokens_mean"),
        }
    return {
        "ab_id": ab_id,
        "model": models[0] if models else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "jobs": sorted({r["job"] for r in runs}),
        "runs": len(runs),
        "arms": arms,
        "comparison": comparison,
        "assertions": keys,
        "labels": LABELS,
        "environment": next((r["environment"] for r in runs if r.get("environment")), {}),
    }
