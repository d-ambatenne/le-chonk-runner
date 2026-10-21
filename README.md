# Le Chonk Runner: a Kotlin Toolchain skill A/B on Harbor

One Harbor task, two arms. Your Mistral Studio agent builds *Le Chonk Runner*, a small Compose
Multiplatform endless runner for Android and iOS, with the Kotlin Toolchain. It does this once without the
[`kotlin-tooling-kotlin-toolchain`](https://github.com/Kotlin/kotlin-agent-skills) skill and once with it.

Each arm is a Harbor job. The two jobs run side by side in Docker on your Mac, and `le-chonk compare`
turns them into statistics and a dashboard. The same steps also run as a Mistral Workflows pipeline.

[The demo](docs/demo.md) builds on it: tasks come from Slack through a Studio agent, become GitHub issues,
and Vibe implements them in the task's container through Mistral Workflows, with releases of the APK and
skill A/Bs on request. [The next phase](docs/next.md) moves the Slack and GitHub steps to Mistral's cloud, through
Studio's connectors, and keeps only Vibe on your Mac: [what runs where](docs/architecture.md).

## Layout

```
tasks/le-chonk-runner/        the Harbor task: everything about the eval is here
  task.toml                   resources, timeouts, and the project kept as an artifact
  instruction.md              the prompt
  environment/                Dockerfile (Ubuntu, Kotlin Toolchain, Android SDK) and warmup.sh
  tests/                      the verifier: the skill's eight assertions → reward.json
  solution/solve.sh           the oracle, to check the image and the verifier
skills/kotlin-tooling-kotlin-toolchain/   SKILL.md and references/, given to the skill arm with --skill
src/le_chonk_eval/
  agents/studio_agent.py      the Harbor agent that runs your Studio agent
  agents/chat.py              le-chonk chat: the same agent in your terminal
  demo/                       the demo: Slack triage agent, GitHub, issue workflows, judge (docs/demo.md)
  next/                       the next phase: cloud workflows with connectors, the Mac's Vibe workflows (docs/next.md)
  workflow/                   the Workflows pipeline, the harbor commands for each arm, the fake harbor
  utils/                      job results, statistics, the dashboard, paths
  cli.py                      le-chonk ab, compare, chat, demo, watch and next
demo/                         the demo's task checklist and Slack app manifest
deploy/control/Dockerfile     the next phase's managed deployment, le-chonk-control
scripts/check_next.py         what your Mistral account has for the next phase: a read-only check
docs/skill-evaluation.md      the skill's published A/B, moved out of the skill folder
docs/demo.md                  the demo: setup and use
docs/architecture.md          what runs where, now and in the next phase, with diagrams
docs/next.md                  the next phase: setup and use
```

The skill folder holds only what the agent may read. The rubric (`evals.json`) is in the task's `tests/`,
which Harbor uploads only after the agent has finished.

## The agent

`le_chonk_eval.agents.studio_agent:StudioAgent` lets Harbor run your Studio agent (Agents API). In Studio it's
`le-chonk-builder-no-skill` or `le-chonk-builder-skill`, and every trial is a conversation you can open there.

- The model calls (`mistral/mistral-large-4`) go from your Mac, so your key never enters the container.
- Its four tools, `run_command`, `read_file`, `write_file` and `list_files`, run in the trial's container.
- With `--skill`, Harbor copies the skill into the container, and the agent lists it in its instructions the
  way coding agents show skills: name, description and the path of `SKILL.md`.
- Options, with `--ak`: `max_turns` (80), `max_cost_usd`, `temperature` and `command_timeout_sec`.
  `uv run harbor agent schema le_chonk_eval.agents.studio_agent:StudioAgent` lists them.

LiteLLM's price table knows Mistral Large 4, so tokens turn into cost.

## Set up once

1. Docker Desktop or OrbStack, with Rosetta emulation on and at least 12 GB of memory for two containers.
2. `uv sync`, then `uv run pytest -q` (offline, about a minute).
3. `.env` with `MISTRAL_API_KEY` (see `.env.example`).
4. Build the image and check it with the oracle. The first build downloads the toolchain, the Android SDK and
   the Compose dependencies, so expect half an hour or more under emulation:

   ```sh
   uv run harbor run -p tasks/le-chonk-runner -a oracle --job-name oracle-1
   ```

   Expect a reward of 1.0. If it's lower, read `jobs/oracle-1/*/verifier/details.json` and `build.log`.
   `environment.warmup` there says whether the image build accepted the Android licences and built a project.
   Use a new job name for each new check: Harbor resumes a job whose name it already knows.

## Chat with the agent

Before the A/B, you can talk to the agent yourself:

```sh
uv run le-chonk chat             # without the skill
uv run le-chonk chat --skill     # with it
```

It's the agent of the A/B: the same Studio agent, instructions and tools, in a container built from the
task's Dockerfile (the first start builds it). You type, the agent answers and runs its tools, and each
tool call shows up as it happens. The container's `/app` is `chat/<session>/app` on your Mac, so you can
open it in your IDE while you chat.

- `/task` sends the A/B task as your message. For a message of several lines, put `"""` on a line before and after it.
- `/sh CMD` runs a command in the container yourself; `/new` starts a new conversation with the same files.
- Ctrl-C stops the agent's turn and the command it was running; then tell it what to do next.
  `/exit` or Ctrl-D ends the chat and removes the container (`--keep` leaves it running).
- `--max-cost` ends the chat at $3 by default; `/cost` shows tokens and cost so far.
- `--project DIR` mounts a folder of yours instead, e.g. a trial's `artifacts/project`, to carry on from there.

## Run locally

```sh
uv run le-chonk ab --dry-run                  # print the two harbor commands
uv run le-chonk ab -k 1                       # one trial per arm, to check time and cost
uv run le-chonk ab -k 8                       # run both arms at once, then compare
uv run le-chonk compare jobs/<id>-no-skill jobs/<id>-skill   # rebuild the comparison
```

`--dry-run` shows plain Harbor. The two arms differ only by `--skill`:

```sh
uv run harbor run -p tasks/le-chonk-runner -a le_chonk_eval.agents.studio_agent:StudioAgent \
  -m mistral/mistral-large-4 -k 8 -n 1 --env-file .env --ak max_cost_usd=4 --job-name ab1-no-skill
uv run harbor run -p tasks/le-chonk-runner -a le_chonk_eval.agents.studio_agent:StudioAgent \
  -m mistral/mistral-large-4 -k 8 -n 1 --env-file .env --ak max_cost_usd=4 --job-name ab1-skill \
  --skill skills/kotlin-tooling-kotlin-toolchain
```

`harbor view jobs` browses the trajectories.

## Run on Mistral Workflows

```sh
uv run le-chonk-worker                        # terminal 1: this Mac becomes the worker (it has Docker)
uv run le-chonk ab --cloud -k 8               # terminal 2: start le-chonk-harbor-ab
uv run le-chonk ab --cloud --smoke            # free: a fake harbor, to test the pipeline first
```

The workflow runs one activity per arm, which runs that arm's Harbor job on the worker, then a compare
activity. You follow the execution and its three activities in Studio; while a job runs, its activity
heartbeats the number of finished trials. A retried activity resumes its job (`harbor job resume`), so
finished trials are kept.

## What's measured

- **Reward:** the share of the eight assertions from the skill's own eval that a trial passes, as in
  [docs/skill-evaluation.md](docs/skill-evaluation.md). The verifier builds the APK and runs the shared tests
  itself. Trials it flags with machine problems (Android SDK licences, memory, network) are set aside.
- **Process,** from each trial's ATIF trajectory: model and tool calls, `gradle` and `kotlin` commands,
  whether `kotlin init` was used, and whether `SKILL.md` was read. Tokens, cost and agent time come from Harbor.
- **Statistics:** per-arm means with 95% intervals, and a permutation test on the reward difference.

Results go to `results/<id>/`: `dashboard.html`, `summary.json` and `runs.jsonl`. Each trial's folder is
`jobs/<id>-<arm>/<trial>/`, with `agent/trajectory.json`, `verifier/details.json`, `verifier/build.log`
and `artifacts/project/`.

## Limits

- **No iOS build.** Harbor's containers are Linux, and iOS needs macOS with Xcode. None of the eight
  assertions builds iOS; the instruction tells the agent so.
- **Emulated amd64.** As far as I know, Android's Linux build tools are x86-64 only, so the image is
  `linux/amd64` and runs under Rosetta. If the oracle also passes on native arm64, remove `--platform` from
  the Dockerfile.
- **Android licences.** The toolchain never accepts them by itself, so the image accepts them while it's built.
- **Cost.** A long agent session resends its whole context on every call, so measure one trial with `-k 1`
  before running `-k 8`. `--max-cost` stops a trial at $4 by default (the agent's `max_cost_usd`).

[FRICTIONS.md](FRICTIONS.md) lists what got in the way between local runs, Harbor and Mistral's cloud.
