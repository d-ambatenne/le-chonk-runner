"""A self-contained HTML dashboard for one comparison: open results/<ab id>/dashboard.html in a browser."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

ARM_CLASS = {"no-skill": "a0", "skill": "a1"}


def _e(value: Any) -> str:
    return html.escape(str(value))


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _int(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _minutes(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds / 60:.1f}"


def _cls(arm: str) -> str:
    return ARM_CLASS.get(arm, "a2")


def _lead(summary: dict[str, Any]) -> str:
    arms, comp = summary["arms"], summary["comparison"]
    if not arms:
        return "No finished trials yet."
    if comp and comp["reward_delta"] is not None:
        a, b = arms["no-skill"], arms["skill"]
        p = comp["reward_p_value"]
        p_text = "n/a" if p is None else ("< 0.001" if p < 0.001 else f"= {p:.3f}")
        return (
            f"Mean reward {_num(b['reward_mean'])} with the skill and {_num(a['reward_mean'])} without it "
            f"(Δ {comp['reward_delta']:+.2f}, permutation p {p_text}, n = {b['n']} and {a['n']})."
        )
    return "So far: " + "; ".join(f"{arm}: mean reward {_num(s['reward_mean'])} (n = {s['n']})" for arm, s in arms.items()) + "."


def _rubric_chart(summary: dict[str, Any]) -> str:
    keys = summary["assertions"]
    arms = list(summary["arms"].items())
    label_w, bar_w, bar_h = 250, 360, 14
    row_h = 18 * max(1, len(arms)) + 14
    width, height = label_w + bar_w + 60, row_h * len(keys) + 30
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Pass rate per assertion and arm">']
    for tick in (0, 0.5, 1):
        x = label_w + bar_w * tick
        parts.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{height - 22}" class="grid"/>')
        parts.append(f'<text x="{x}" y="{height - 6}" class="tick" text-anchor="middle">{int(tick * 100)}%</text>')
    for row, key in enumerate(keys):
        y0 = row * row_h + 6
        parts.append(f'<text x="{label_w - 10}" y="{y0 + row_h / 2}" class="label" text-anchor="end" dominant-baseline="middle">{_e(summary["labels"].get(key, key))}</text>')
        for i, (label, stats) in enumerate(arms):
            rate = stats["assertion_rates"].get(key)
            y = y0 + i * 18
            if rate is None:
                parts.append(f'<text x="{label_w + 4}" y="{y + bar_h - 3}" class="na">n/a</text>')
                continue
            w = max(1.5, bar_w * rate)
            parts.append(f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="3" class="{_cls(label)}"><title>{_e(label)}: {_pct(rate)}</title></rect>')
            parts.append(f'<text x="{label_w + w + 6:.1f}" y="{y + bar_h - 3}" class="value">{_pct(rate)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _reward_strip(summary: dict[str, Any]) -> str:
    arms = list(summary["arms"].items())
    label_w, plot_w, row_h = 150, 480, 54
    width, height = label_w + plot_w + 30, row_h * max(1, len(arms)) + 34
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Reward of every trial, per arm">']
    for tick in (0, 0.25, 0.5, 0.75, 1):
        x = label_w + plot_w * tick
        parts.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{height - 24}" class="grid"/>')
        parts.append(f'<text x="{x}" y="{height - 8}" class="tick" text-anchor="middle">{tick:.2f}</text>')
    for row, (label, stats) in enumerate(arms):
        y = row * row_h + row_h / 2
        parts.append(f'<text x="{label_w - 12}" y="{y}" class="label" text-anchor="end" dominant-baseline="middle">{_e(label)}</text>')
        for i, reward in enumerate(stats["rewards"]):
            jitter = ((i * 37) % 21 - 10) * 1.3
            parts.append(f'<circle cx="{label_w + plot_w * reward:.1f}" cy="{y + jitter:.1f}" r="5" class="{_cls(label)} dot"><title>{reward:.2f}</title></circle>')
        if stats["reward_mean"] is not None:
            x = label_w + plot_w * stats["reward_mean"]
            parts.append(f'<line x1="{x:.1f}" y1="{y - 20}" x2="{x:.1f}" y2="{y + 20}" class="mean"/>')
    parts.append("</svg>")
    return "".join(parts)


def _kpis(summary: dict[str, Any]) -> str:
    cards = []
    for label, s in summary["arms"].items():
        ci = s.get("reward_ci95")
        ci_text = f"95% CI {ci[0]:.2f}–{ci[1]:.2f}" if ci else f"n = {s['n']}"
        tokens = None
        judged = (f"<dt>Judge: criteria met</dt><dd>{_pct(s['judge_score_mean'])}</dd>"
                  if s.get("judge_score_mean") is not None else "")
        if s["input_tokens_mean"] is not None or s["output_tokens_mean"] is not None:
            tokens = (s["input_tokens_mean"] or 0) + (s["output_tokens_mean"] or 0)
        cards.append(f"""
        <div class="card"><div class="arm {_cls(label)}t">{_e(label)}</div>
          <div class="big">{_num(s['reward_mean'])}</div><div class="muted">mean reward · {ci_text}</div>
          <dl>{judged}
            <dt>Cost per trial</dt><dd>${_num(s['cost_mean'])}</dd>
            <dt>Tokens per trial</dt><dd>{_int(tokens)}</dd>
            <dt>Agent time per trial</dt><dd>{_minutes(s['agent_seconds_mean'])} min</dd>
            <dt>Stopped at the cost or turn limit</dt><dd>{s['stopped_at_limit']} of {s['n']}</dd>
            <dt>Trials with agent errors</dt><dd>{s['agent_errors']} of {s['n']}</dd>
            <dt>Set aside (machine problems)</dt><dd>{s['set_aside']}</dd>
          </dl></div>""")
    return "".join(cards)


def _process_table(summary: dict[str, Any]) -> str:
    rows = [
        ("Model calls", "model_calls_mean", _num),
        ("Tool calls", "tool_calls_mean", _num),
        ("Shell commands", "shell_commands_mean", _num),
        ("gradle calls", "gradle_calls_mean", _num),
        ("kotlin calls", "kotlin_calls_mean", _num),
        ("Scaffolded with kotlin init/new", "kotlin_scaffold_rate", _pct),
        ("Read SKILL.md", "skill_md_read_rate", _pct),
    ]
    head = "".join(f"<th>{_e(label)}</th>" for label in summary["arms"])
    body = "".join(
        "<tr><td>" + _e(name) + "</td>" + "".join(f"<td>{fmt(s.get(key))}</td>" for s in summary["arms"].values()) + "</tr>"
        for name, key, fmt in rows
    )
    return f'<div class="scroll"><table class="compact"><thead><tr><th>Mean per trial</th>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _runs_table(runs: list[dict[str, Any]]) -> str:
    body = []
    for r in sorted(runs, key=lambda r: (r["arm"], r["trial"])):
        p = r.get("process") or {}
        tokens = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) if r.get("input_tokens") is not None else None
        stopped = r.get("stop_reason") if r.get("stop_reason") not in (None, "done") else ""
        note = ", ".join(r.get("environment_errors") or []) or r.get("error") or stopped
        body.append(
            f"<tr><td>{_e(r['trial'])}</td><td><span class='pill {_cls(r['arm'])}'>{_e(r['arm'])}</span></td>"
            f"<td>{_num(r.get('reward'))}</td><td>{_int(tokens)}</td><td>${_num(r.get('cost_usd'))}</td><td>{_minutes(r.get('agent_seconds'))}</td>"
            f"<td>{p.get('gradle_calls', 'n/a')}</td><td>{p.get('kotlin_calls', 'n/a')}</td>"
            f"<td>{'yes' if p.get('kotlin_scaffold_used') else 'no'}</td><td class='err'>{_e(note[:140])}</td></tr>"
        )
    return (
        "<div class='scroll'><table><thead><tr><th>Trial</th><th>Arm</th><th>Reward</th><th>Tokens</th>"
        "<th>Cost</th><th>Min</th><th>gradle</th><th>kotlin</th><th>init</th><th>Note</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table></div>"
    )


CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--card:#ffffff;--a0:#7b8794;--a1:#e8632b;--a2:#3d7fbf}
@media (prefers-color-scheme: dark){:root{--bg:#141416;--fg:#ececee;--muted:#9a9aa2;--line:#2c2c31;--card:#1c1c20;--a0:#9aa5b1;--a1:#f07a45;--a2:#6aa4dc}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
main{max-width:1000px;margin:0 auto;padding:28px 16px 48px}h1{font-size:26px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub,.muted{color:var(--muted)}.lead{font-size:17px;margin:14px 0 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}.big{font-size:34px;font-weight:650;line-height:1.1;margin-top:6px}
.arm{font-weight:600}.a0t{color:var(--a0)}.a1t{color:var(--a1)}.a2t{color:var(--a2)}
dl{display:grid;grid-template-columns:1fr auto;gap:4px 12px;margin:14px 0 0}dt{color:var(--muted)}dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
svg{width:100%;height:auto;display:block}.chart{overflow-x:auto}.chart svg{min-width:620px}svg text{fill:var(--fg);font-size:12px}svg .tick,svg .na{fill:var(--muted);font-size:11px}
svg .value{font-size:11px;fill:var(--muted)}.grid{stroke:var(--line)}.mean{stroke:var(--fg);stroke-width:2}
rect.a0,circle.a0{fill:var(--a0)}rect.a1,circle.a1{fill:var(--a1)}rect.a2,circle.a2{fill:var(--a2)}.dot{fill-opacity:.85}
.legend{display:flex;flex-wrap:wrap;gap:16px;color:var(--muted);font-size:13px;margin:0 0 8px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--muted);font-weight:600}
table.compact{max-width:640px}.pill{padding:1px 8px;border-radius:99px;color:#fff;font-size:12px}.pill.a0{background:var(--a0)}.pill.a1{background:var(--a1)}.pill.a2{background:var(--a2)}
.err{color:var(--muted);white-space:normal;min-width:160px}footer{margin-top:36px;color:var(--muted);font-size:13px}
"""


def render(summary: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    legend = "".join(f"<span><i style='background:var(--{_cls(arm)})'></i>{_e(arm)}</span>" for arm in summary["arms"])
    env = summary.get("environment") or {}
    env_text = " · ".join(
        f"{name}: {value}" for name, value in (("Kotlin Toolchain", env.get("kotlin_toolchain")), ("image warm-up", env.get("warmup"))) if value
    )
    meta = f"{summary['ab_id']} · {summary.get('agent_label', 'Studio agent')} on {summary['model']} · {summary['runs']} trials · {summary['generated_at']}"
    title = summary.get("title") or "Le Chonk Runner: Kotlin Toolchain skill A/B"
    note = summary.get("reward_note") or (
        "Reward = share of the eight assertions of the skill's own eval that a trial passes. Trials the verifier "
        "flagged with machine problems (Android SDK licences, memory, network) are set aside.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Le Chonk A/B</title><style>{CSS}</style></head>
<body><main>
<header><h1>{_e(title)}</h1><div class="sub">{_e(meta)}</div><p class="lead">{_e(_lead(summary))}</p></header>
<section class="kpis">{_kpis(summary)}</section>
<h2>Pass rate per assertion</h2><div class="legend">{legend}</div><div class="chart">{_rubric_chart(summary)}</div>
<p class="muted">{_e(note)}</p>
<h2>Reward per trial</h2><div class="chart">{_reward_strip(summary)}</div>
<h2>How the agent worked</h2>{_process_table(summary)}
<h2>Trials</h2>{_runs_table(runs)}
<footer>{_e(env_text)}<br>Per trial: jobs/&lt;job&gt;/&lt;trial&gt;/ with agent/trajectory.json, verifier/details.json,
verifier/build.log and artifacts/project/. Generated by le-chonk.</footer>
</main></body></html>"""


def write_dashboard(summary: dict[str, Any], runs: list[dict[str, Any]], path: Path) -> Path:
    path.write_text(render(summary, runs), encoding="utf-8")
    return path
