"""le-chonk: the Kotlin Toolchain skill A/B as two Harbor jobs, run here or on Mistral Workflows.

  le-chonk ab [options]          run both arms on this Mac, then compare
  le-chonk ab --cloud [options]  the same as the Workflows pipeline le-chonk-harbor-ab (needs le-chonk-worker)
  le-chonk ab --dry-run          print the two harbor commands and stop
  le-chonk ab --smoke            the whole pipeline with a fake harbor: no Docker, no model calls
  le-chonk compare JOB [JOB...]  compare finished Harbor jobs and write a dashboard
  le-chonk chat [--skill]        chat with the Studio agent; its tools run in the task's container
  le-chonk demo                  the demo: this Mac's worker and the Slack app (see docs/demo.md)
  le-chonk watch REPO            start watching a repo's issues on Mistral Workflows
  le-chonk next worker|start|stop  the next phase: Studio in the cloud, Vibe on this Mac (see docs/next.md)
  le-chonk qa run --repo URL --issue N --release TAG  check a release's APK on an emulator against the issue
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def _ab_input(args: argparse.Namespace):
    from .workflow.pipeline import HarborAbInput

    ab_id = args.ab_id or ("smoke-" if args.smoke else "ab-") + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return HarborAbInput(
        ab_id=ab_id,
        model=args.model,
        attempts=args.attempts,
        concurrency=args.concurrency,
        max_cost_usd=args.max_cost or None,
        agent_options=args.ak,
        smoke=args.smoke,
    )


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="le-chonk", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    ab = sub.add_parser("ab", help="run both arms as Harbor jobs, then compare")
    ab.add_argument("--cloud", action="store_true", help="start le-chonk-harbor-ab on Mistral Workflows; keep `uv run le-chonk-worker` running")
    ab.add_argument("--model", default="mistral/mistral-large-4")
    ab.add_argument("-k", "--attempts", type=int, default=8, help="trials per arm (default 8)")
    ab.add_argument("-n", "--concurrency", type=int, default=1, help="trials at a time per arm (default 1, so two containers in all)")
    ab.add_argument("--max-cost", type=float, default=4.0, help="USD limit per trial (default 4; 0 for none)")
    ab.add_argument("--ak", action="append", default=[], metavar="KEY=VALUE", help="Studio agent option, e.g. max_turns=60; repeatable")
    ab.add_argument("--ab-id", default="", help="name of the comparison; the jobs are <ab id>-no-skill and <ab id>-skill")
    ab.add_argument("--dry-run", action="store_true", help="print the harbor commands and stop")
    ab.add_argument("--smoke", action="store_true", help="use a fake harbor: free and offline, to test the pipeline")

    cmp = sub.add_parser("compare", help="compare finished Harbor jobs")
    cmp.add_argument("jobs", nargs="+", type=Path, help="job folders, e.g. jobs/ab-20261020-1200-no-skill")
    cmp.add_argument("--ab-id", default=None)

    chat = sub.add_parser("chat", help="chat with the Studio agent; its tools run in the task's container")
    chat.add_argument("--skill", action="store_true", help="give the agent the kotlin-tooling-kotlin-toolchain skill")
    chat.add_argument("--model", default="mistral/mistral-large-4")
    chat.add_argument("--project", type=Path, default=None, help="the folder mounted at /app; default: a new chat/<session>/app")
    chat.add_argument("--max-cost", type=float, default=3.0, help="stop the chat at this many USD (default 3; 0 for none)")
    chat.add_argument("--keep", action="store_true", help="leave the container running when the chat ends")

    def demo_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--local", action="store_true", help="run the workflows in this process, without Mistral Workflows")
        command.add_argument("--smoke", action="store_true", help="a fake harbor and judge: free, to try the flow")
        command.add_argument("--model", default="mistral/mistral-large-4", help="Vibe's model, unless an issue sets one")
        command.add_argument("--max-cost", type=float, default=3.0, help="USD limit per Vibe run (default 3)")
        command.add_argument("--ab-attempts", type=int, default=3, help="runs per arm of an A/B, unless the issue sets it (default 3)")
        command.add_argument("--poll", type=int, default=60, help="seconds between checks of the repo's issues (default 60)")

    demo = sub.add_parser("demo", help="the demo: this Mac's worker and the Slack app")
    demo_options(demo)
    demo.add_argument("--no-slack", action="store_true", help="only the worker")
    watch = sub.add_parser("watch", help="start watching a repo's issues (le-chonk-issues on Mistral Workflows)")
    watch.add_argument("repo", help="https://github.com/<owner>/<repo>")
    demo_options(watch)
    watch.add_argument("--max-polls", type=int, default=None, help="stop after this many checks without work")

    nxt = sub.add_parser("next", help="the next phase: Studio in the cloud, Vibe on this Mac (docs/next.md)")
    nxt_sub = nxt.add_subparsers(dest="next_command", required=True)
    worker = nxt_sub.add_parser("worker", help="make this Mac the worker of le-chonk-vibe")
    worker.add_argument("--deployment", default=None, help="the deployment name (default le-chonk-vibe)")
    start = nxt_sub.add_parser("start", help="start the cloud watchers of a Slack channel and a repo, on your behalf")
    stop = nxt_sub.add_parser("stop", help="stop the cloud watchers")
    for command in (start, stop):
        command.add_argument("--channel", required=command is start, help="the Slack channel: its name or ID")
        command.add_argument("--repo", default=None, help="https://github.com/<owner>/<repo>")
    start.add_argument("--smoke", action="store_true", help="a fake harbor on your Mac and a fake judge: to try the flow")
    start.add_argument("--model", default="mistral/mistral-large-4", help="Vibe's model, unless an issue sets one")
    start.add_argument("--max-cost", type=float, default=3.0, help="USD limit per Vibe run (default 3)")
    start.add_argument("--ab-attempts", type=int, default=3, help="runs per arm of an A/B, unless the issue sets it (default 3)")
    start.add_argument("--poll", type=int, default=60, help="seconds between checks of the channel and the repo (default 60)")
    start.add_argument("--control", default="le-chonk-control", help="the managed deployment's name (default le-chonk-control)")
    start.add_argument("--vibe", default="le-chonk-vibe", help="your Mac's deployment name (default le-chonk-vibe)")

    qa = sub.add_parser("qa", help="check a release's APK on an emulator against its issue (docs/next.md, QA)")
    qa_sub = qa.add_subparsers(dest="qa_command", required=True)
    qa_run = qa_sub.add_parser("run", help="install the release's APK on lechonk-qa and check every acceptance criterion")
    qa_run.add_argument("--repo", required=True, help="https://github.com/<owner>/<repo>")
    qa_run.add_argument("--issue", type=int, required=True, help="the issue whose criteria are checked")
    qa_run.add_argument("--release", required=True, help="the release's tag, such as issue-1-20261021-202046")
    qa_run.add_argument("--model", default=None, help="the QA agent's model (default mistral-large-4, or LECHONK_QA_MODEL)")
    qa_run.add_argument("--serial", default=None, help="a running emulator or phone, such as emulator-5554; default: boot lechonk-qa")
    args = parser.parse_args(argv)

    if args.command == "qa":
        from .qa.inputs import QaInput
        from .qa.run import run_qa, table

        if not os.environ.get("MISTRAL_API_KEY"):
            sys.exit("MISTRAL_API_KEY is not set. Put it in .env (see .env.example).")
        params = QaInput(repo=args.repo, number=args.issue, release=args.release, model=args.model, serial=args.serial)
        result = asyncio.run(run_qa(params))
        print(table(result))
        print(f"\nVerdicts, screenshots and flows: {result['out']}")
        return

    if args.command == "next":
        from .next import start as next_start

        if args.next_command == "worker":
            next_start.run_worker(args.deployment)
            return
        if args.next_command == "start":
            options = {"model": args.model, "max_cost_usd": args.max_cost, "ab_attempts": args.ab_attempts, "poll_seconds": args.poll,
                       "smoke": args.smoke, "vibe_deployment": args.vibe}
            print(json.dumps(asyncio.run(next_start.start(args.channel, args.repo, options, args.control)), indent=2))
            return
        print(json.dumps(asyncio.run(next_start.stop(args.channel, args.repo)), indent=2))
        return

    if args.command in ("demo", "watch"):
        from .demo import runner

        options = {"model": args.model, "max_cost_usd": args.max_cost, "ab_attempts": args.ab_attempts, "poll_seconds": args.poll, "smoke": args.smoke}
        if args.command == "demo":
            asyncio.run(runner.run_demo(slack=not args.no_slack, local=args.local, options=options))
            return
        if args.max_polls is not None:
            options["max_polls"] = args.max_polls
        print(json.dumps(asyncio.run(runner.watch(args.repo, local=args.local, options=options)), indent=2, default=str))
        return

    if args.command == "chat":
        from .agents.chat import run_chat

        run_chat(skill=args.skill, model=args.model, project=args.project, max_cost_usd=args.max_cost or None, keep=args.keep)
        return

    if args.command == "compare":
        from .utils.compare import compact, compare

        summary = compare(args.jobs, args.ab_id)
        print(json.dumps(compact(summary), indent=2))
        print(f"\nDashboard: {summary['dashboard']}")
        return

    params = _ab_input(args)
    if args.dry_run:
        from .workflow.harbor_jobs import run_command
        from .workflow.pipeline import plan_jobs

        for job in plan_jobs(params, params.ab_id):
            print(shlex.join(run_command(job.job_name, job.arm, model=job.model, attempts=job.attempts, concurrency=job.concurrency,
                                         max_cost_usd=job.max_cost_usd, agent_options=job.agent_options)))
        print(f"\nThen: uv run le-chonk compare jobs/{params.ab_id}-no-skill jobs/{params.ab_id}-skill")
        return
    if (args.cloud or not params.smoke) and not os.environ.get("MISTRAL_API_KEY"):
        sys.exit("MISTRAL_API_KEY is not set. Put it in .env (see .env.example).")

    if args.cloud:
        from mistralai.client import Mistral

        client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
        from .workflow.pipeline import DEMO_DEPLOYMENT

        client.workflows.execute_workflow(workflow_identifier="le-chonk-harbor-ab", execution_id=params.ab_id, input=params.model_dump(),
                                          deployment_name=os.environ.get("DEPLOYMENT_NAME") or DEMO_DEPLOYMENT)
        print(f"Started le-chonk-harbor-ab, execution {params.ab_id}. Follow it in Studio > Workflows.")
        print(f"Your worker writes jobs/{params.ab_id}-*/ and results/{params.ab_id}/dashboard.html.")
        return

    import mistralai.workflows as workflows

    from .workflow.pipeline import HarborAb

    summary = asyncio.run(workflows.execute_workflow(HarborAb, params))
    print(json.dumps(summary, indent=2))
    print(f"\nDashboard: {summary.get('dashboard')}")


if __name__ == "__main__":
    main()
