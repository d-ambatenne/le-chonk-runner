# Le Chonk Runner: a mobile app built from Slack with Mistral Workflows

A demo pipeline that builds a mobile app from chat. You write a task in Slack. A Mistral Studio agent checks it
and files it as a GitHub issue. Mistral Workflows picks up the issue, and Vibe implements it in a Docker
container: a Compose Multiplatform app for Android and iOS, built with the Kotlin Toolchain. A judge reads the
change, it's merged if it builds and the tests pass, and its APK goes to a GitHub release. A QA agent can then
install that APK on an emulator and check it against the issue.

The app is *Le Chonk Runner*, a small endless runner, but the pipeline works the same for any task you give it.

```
Slack ──> triage agent (Studio)        checks the task, asks for what's missing, rejects duplicates
              │
              ▼
GitHub issue  vibe:ready ──> Mistral Workflows    one task at a time
              │
              ▼
Vibe in Docker (Harbor)                implements the issue, builds the APK, runs the shared tests
              │
              ▼
judge ──> PR, squash merge ──> GitHub release with the APK ──> QA on an emulator
              │
              └──> replies in the Slack thread and comments on the issue
```

From the same Slack thread you can also give a task a skill, run it again, or ask for a skill A/B: k runs
without the skill, k with it, judged and compared in a report.

## Two ways to run it

| | [The demo](docs/demo.md) | [The next phase](docs/next.md) |
|---|---|---|
| Slack and GitHub | a Slack app and a GitHub token, on your Mac | Studio's connectors, in Mistral's cloud |
| Triage and the issue watcher | workflows whose steps run on your Mac | a managed deployment, `le-chonk-control` |
| Vibe, Docker, the APK | your Mac | your Mac |
| Start | `uv run le-chonk demo` | `uv run le-chonk next start`, then `next worker` |

The next phase is built and tested offline, but not yet checked against real accounts: see the end of
[next.md](docs/next.md). What runs where, with diagrams:
[architecture.md](docs/architecture.md).

Two repos take part: this one, the automation, and the app's own repo (such as `le-chonk-game`), which holds
only the app, Vibe's PRs and the releases with the APKs.

## Quick start

1. Docker Desktop or OrbStack, with Rosetta emulation on and at least 12 GB of memory.
2. `uv sync`, then `uv run pytest -q` (offline, about a minute).
3. `.env` from [.env.example](.env.example): `MISTRAL_API_KEY`, and for the demo `GITHUB_TOKEN`,
   `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.
4. Set up the Slack app and the app's repo: [docs/demo.md](docs/demo.md#set-up-once).
5. Try the whole flow for free, with a fake harbor and judge but real Slack and GitHub:

   ```sh
   uv run le-chonk demo --smoke
   ```

6. Then the real thing, Vibe on Mistral Large 4 with a $3 limit per run: `uv run le-chonk demo`.

In the channel: `@Le Chonk use https://github.com/<you>/le-chonk-game`, then
`@Le Chonk build Le Chonk Runner: an endless runner where Le Chonk jumps over obstacles.`

The first run builds the task's image (the Kotlin Toolchain, the Android SDK and the Compose dependencies), which
takes half an hour or more under emulation. A run then takes 15 to 40 minutes.

## Commands

```
le-chonk demo                    the demo: this Mac's worker and the Slack app
le-chonk watch REPO              start watching a repo's issues on Mistral Workflows
le-chonk next start|stop|worker  the next phase: Studio in the cloud, Vibe on this Mac
le-chonk qa run|mark             check a release's APK on an emulator against its issue
le-chonk ab | compare | chat     the Kotlin Toolchain skill A/B on Harbor (docs/skill-ab.md)
```

`uv run le-chonk <command> --help` lists the options.

## Layout

```
src/le_chonk_eval/
  demo/                 the demo: Slack triage agent, GitHub, issue workflows, the judge, releases
  next/                 the next phase: cloud workflows with connectors, the Mac's Vibe workflows
  qa/                   QA: an agent checks a release's APK on an emulator against its issue
  agents/               Vibe for Harbor, and the Studio agent of the skill A/B
  workflow/             the skill A/B as a Workflows pipeline, and the fake harbor for smoke runs
  utils/                job results, statistics, the A/B dashboard
  cli.py                le-chonk and its commands
tasks/le-chonk-runner/  the Harbor task: the image (Ubuntu, Kotlin Toolchain, Android SDK) and the verifier
skills/                 the kotlin-tooling-kotlin-toolchain skill, for A/Bs
demo/                   the task checklist the triage agent uses, and the Slack app manifest
deploy/control/         the Dockerfile of the managed deployment le-chonk-control
scripts/                checks of your Mistral account, the latest runs' status, redeploys, the screenshot check
docs/                   demo.md, next.md, architecture.md, skill-ab.md, skill-evaluation.md
```

## Costs and limits

- **Cost.** Vibe stops at `--max-cost` ($3 by default) per run. An A/B is 2 × k runs; the judge adds a small
  call per run.
- **No iOS build.** The containers are Linux, and iOS needs macOS with Xcode, so the checks are the Android
  APK and the shared tests.
- **Emulated amd64.** Android's Linux build tools are x86-64, so the image runs under Rosetta on Apple Silicon.
- **Secrets.** The GitHub token stays on your Mac and never enters a container. The Mistral key goes into
  Vibe's container, because Vibe runs there, so use a separate key.

## Where it started

The repo began as an A/B of the Kotlin Toolchain skill: one Harbor task, a Studio agent building the app with
and without the skill, and statistics on the difference. That still runs, and the pipeline reuses its task and
verifier: [docs/skill-ab.md](docs/skill-ab.md).

[FRICTIONS.md](FRICTIONS.md) lists what got in the way between local runs, Harbor, Studio and Mistral Workflows.
