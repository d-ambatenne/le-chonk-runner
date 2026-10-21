# Demo: Slack → Studio agent → GitHub → Workflows → Vibe

You write a task in Slack. A Studio agent checks it and files it as a GitHub issue. A workflow on Mistral
Workflows picks up the issue, and Vibe 2.0 implements it in a container on your Mac. The change is merged
if it builds, and its APK goes to a GitHub release. From the same Slack thread, you can attach a skill to
a task and ask for a skill A/B.

```
Slack thread ──> le-chonk-triage (Studio agent)       checks the task, rejects duplicates, files the issue
                       │  tools run on your Mac: GitHub API with your token
                       ▼
GitHub issue  vibe:ready ──> le-chonk-issues (Workflows) polls the repo every minute
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
le-chonk-issue-run              le-chonk-issue-ab           (Workflows, activities on your Mac)
  Harbor + Vibe in Docker         k runs without the skill, k runs with it
  judge, then PR + merge          judge every run, report
  release with the APK            pre-release: report and one APK per arm
        └──────── Slack thread and issue comments ─────────┘
```

What runs where, and the next phase: [architecture.md](architecture.md).

## Set up once

Use a Slack workspace where you can add apps, and a GitHub account where you can make repos and tokens.

1. **GitHub.** Create the demo repo; it can be empty and private. Then create a fine-grained token for
   that repo only (Settings > Developer settings > Fine-grained tokens), with these permissions:
   - Contents: read and write (for the push and the releases)
   - Issues: read and write
   - Pull requests: read and write
2. **Slack.** In api.slack.com/apps, choose Create New App > From a manifest, and paste
   [demo/slack-app-manifest.yaml](../demo/slack-app-manifest.yaml). Install it to the workspace.
   - The bot token is under OAuth & Permissions (`xoxb-…`).
   - The app token is under Basic Information > App-Level Tokens: generate one with `connections:write` (`xapp-…`).
   - Invite the app to a channel: `/invite @Le Chonk`.
3. **`.env`**, next to `.env.example`:
   ```
   MISTRAL_API_KEY=…
   GITHUB_TOKEN=github_pat_…
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_APP_TOKEN=xapp-…
   ```
4. **Docker** must be running. The first run builds the task's image, which takes a while.

## Run

```sh
uv sync
uv run le-chonk demo --smoke     # try the whole flow for free: a fake harbor and judge, real Slack and GitHub
uv run le-chonk demo             # the real thing: Vibe on Mistral Large 4, $3 limit per run
```

`le-chonk demo` makes your Mac the worker of the three workflows, on the deployment `le-chonk` (set
`DEPLOYMENT_NAME` for another), and connects the Slack app. Add `--local`
to run the workflows in the same process, without Mistral Workflows. Other options: `--model`, `--max-cost`,
`--ab-attempts`, `--poll`.

## In Slack

Mention the app in the channel, then reply in the thread; the agent follows the thread.

- `@Le Chonk use https://github.com/<you>/le-chonk-game` sets the channel's repo and starts its watcher.
- `@Le Chonk build Le Chonk Runner: …` is the first task. In an empty repo, Vibe creates the project.
- `@Le Chonk Le Chonk should jump once more in the air` gives a feature task. The agent asks for anything the
  [checklist](../demo/task-checklist.md) misses, and links an existing issue if it's a duplicate.
- `add the skill <link> to #2`, where the link is the skill's folder on GitHub: copy it from the browser,
  it has `/tree/<branch>/` in it. Vibe gets the skill when it implements #2.
- `run an A/B on #2 with 3 runs per arm` starts the A/B, without the skill and with it. The report and the
  APKs come back to the thread.
- `queue #2 again` runs it again, for example after a failure or after an A/B. It also frees an issue left
  in `vibe:running`, for example when the Mac slept during a run or someone stopped the run.
- `create it, but hold it` files the task without starting it, so you can add a skill first.

## What happens where

- **Labels** show each issue's state: `vibe:ready`, `vibe:running`, then one of:
  - `vibe:done`: merged, and the judge agrees; the issue is closed.
  - `vibe:not-done`: merged because it builds, but the judge says it isn't done; the issue stays open.
  - `vibe:failed`: nothing merged.

  A/B issues go through `ab:requested`, `ab:running` and `ab:done`.
- **Merging.** A run is merged when the APK builds and the shared tests pass: a PR from
  `vibe/issue-<n>-<time>`, squashed into the default branch. Tasks run one at a time, so each starts
  from the last merge.
- **Releases.** Each merged run gets a release named after the run, with `le-chonk-issue-<n>.apk`. Each
  A/B gets a pre-release with `ab-report.html` and the best APK of each arm.
- **On your Mac:**
  - `runs/<run>/` holds the clone and the Harbor task made from the issue.
  - `jobs/<run>/` holds Harbor's trials, with `judge.json` next to each result.
  - `results/<run>/` holds the A/B report.
- **In Studio:** the triage agent's conversations, and the executions of `le-chonk-issues`,
  `le-chonk-issue-run` and `le-chonk-issue-ab` under Workflows.

## Costs and limits

- **Per run:** Vibe stops at `--max-cost` ($3 by default). An A/B is 2 × k runs; the judge adds one small
  call per run. Vibe needs the model's price to enforce the limit. For a model that LiteLLM's price table
  doesn't know, set `LECHONK_PRICE_INPUT` and `LECHONK_PRICE_OUTPUT` (USD per million tokens), or the run
  goes without a limit, and the issue says so.
- **Time:** a run takes 15 to 40 minutes under amd64 emulation on Apple Silicon, and the verifier builds
  once more.
- **Secrets:** the GitHub token stays on your Mac: git gets it as a one-off header, and containers never
  see it. The Mistral key goes into Vibe's container, because Vibe runs there, so use a separate key.
- **No iOS:** the container can't build the iOS app, so the checks are Android and the shared tests.
