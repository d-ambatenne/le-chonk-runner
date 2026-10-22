# The next phase: Studio in the cloud, Vibe on your Mac

The [demo](demo.md) with its Slack and GitHub steps moved to Mistral's cloud. Two workflows on a managed
deployment, `le-chonk-control`, read your Slack channel and your app repo's issues through Studio's connectors,
and the triage agent answers there. Only Vibe runs on your Mac, in Docker. What runs where:
[architecture.md](architecture.md).

Two repos: this one, le-chonk-runner, is the automation, and Mistral builds the cloud worker from it. The app's
repo, such as le-chonk-game, holds only the app: Vibe's changes, their PRs, and releases with the APKs.

The code is built and tested offline. It hasn't run against your accounts yet: see the end.

## Set up once

Use the same Slack workspace and GitHub account as for the demo.

1. **Pay-as-you-go** on your Mistral organization (Admin > Billing). Managed deployments need it.
2. **Connectors.** Studio > Connectors: GitHub and Slack connected. `uv run python scripts/check_next.py` says
   so, and lists their tools.
3. **A Slack channel** for the demo, such as `#le-chonk`. Every new message there is a request for the agent, and
   it answers in the message's thread. The Slack connector posts as you, so the agent's messages start with 🤖.
   `--channel` takes the channel's exact name or its ID.
4. **This repo on GitHub.** Push le-chonk-runner to a private repo of yours, and install the Mistral GitHub App on it
   (github.com/apps/mistralai). Mistral builds the cloud worker from it.
5. **The managed deployment.** Studio > Workflows > Deployments, a new managed deployment:
   - name `le-chonk-control`, from your le-chonk-runner repo, branch `main`
   - Dockerfile `deploy/control/Dockerfile`, build directory: the repo's root
   - hardened, the default: its workflows run on behalf of whoever starts them
   - no secrets: it uses your connectors

   Wait until it's Active.
6. **`.env`** on your Mac: `MISTRAL_API_KEY`. No Slack tokens; the GitHub token comes once the app's repo exists.
   For the script below, also `LECHONK_CHANNEL` and `LECHONK_REPO`: the channel's name and the app repo's URL.

After a change to the code, everything restarts, since a running execution keeps the code it started with:
`uv run python scripts/redeploy_next.py` stops the watchers, commits and pushes, restarts the deployment and waits
until it runs the new commit, stops your Mac's worker, starts the watchers again, and runs the worker in its terminal.
Add `--smoke` for a smoke test, `--queue 3` to queue issue #3 again, and `-m "…"` for the commit message.

## Run

1. Start the channel's watcher: `uv run le-chonk next start --channel le-chonk --smoke`. It runs on your behalf,
   with your connectors, because `start` uses your API key.
2. Create the app's repo on GitHub, empty and private, such as `le-chonk-game`, and give the Mistral GitHub App
   access to it (github.com/apps/mistralai › Configure). The connector can't create repos: GitHub refuses the App
   ([friction 32](../FRICTIONS.md#the-demo)). Then, in the channel: `Use https://github.com/<you>/le-chonk-game`. The
   agent starts watching its issues. Or start with `--repo https://github.com/<you>/le-chonk-game`, which also
   colors its labels.
3. Make a fine-grained GitHub token for that repo only, with Contents, Issues and Pull requests: read and write.
   Put it in `.env` as `GITHUB_TOKEN`: your Mac clones, pushes and releases with it.
4. Start your Mac's worker, and keep it running: `uv run le-chonk next worker`.
5. Ask for the first task, such as `Build Le Chonk Runner: an endless runner where Le Chonk jumps over obstacles.`

`--smoke` runs the fake harbor on your Mac and a judge that accepts every change: the whole flow, almost free.
For real runs, stop the watchers and start them again without it.

In the channel, write as in the demo, without mentioning the app: `build Le Chonk Runner: …`, `add the skill
<link> to #2`, `run an A/B on #2 with 3 runs per arm`, and `queue #2 again`.
Answers come within a minute: the watcher reads the channel every minute (`--poll`).

## What happens where

- **le-chonk-slack** (cloud) reads the channel, and the threads the agent is in. The triage agent answers each
  message; its tools file and label issues with the GitHub connector.
- **le-chonk-tasks** (cloud) picks up `vibe:ready` and `ab:requested` issues, one at a time. It labels the issue,
  starts the run on your Mac through the Workflows API, and checks on it every minute.
- **le-chonk-vibe-run** (your Mac) clones the repo, runs Vibe in the task's container and the checks. If the APK
  builds and the tests pass, it pushes `vibe/<run>` and makes a release with the APK.
- Back in the cloud: the PR, the judge on its diff, the squash merge, the labels, the comment and the reply.
- **le-chonk-vibe-ab** (your Mac) runs the A/B and judges every run there, where the changes are. A pre-release gets
  the report and one APK per arm; the cloud posts the links.
- **In Studio:** the executions of all four workflows, and the triage agent's conversations.

Stop the watchers with `uv run le-chonk next stop --channel le-chonk --repo https://github.com/<you>/le-chonk-game`.
A run in progress is stopped on your Mac too, and its issue is freed, so you can queue it again.

In Studio, a watcher shows as `le-chonk-slack-<channel>-a` or `-b`: when its history grows long, it starts the
other one with its state and stops ([friction 27](../FRICTIONS.md#the-demo)).

## QA: an agent checks the app

An agent installs a release's APK on an emulator and checks it against the issue's acceptance criteria. It sees
the criteria and the running app, never the code. Each criterion gets pass, fail or can't check. A check read
from the screen's UI tree would block a merge; one judged from a screenshot wouldn't. For a tree check, the agent
writes a Maestro flow: later runs replay the flows, and the agent explores only criteria without one, or whose flow
failed. A new flow is replayed before it's kept; when that fails, the agent gets the reason and may fix the flow twice. For now QA only reports: it files no issues and holds no merge.

Set up once, on your Mac:

1. **The Android SDK's tools**, from Android Studio. QA finds the SDK in `~/Library/Android/sdk`, or in `ANDROID_HOME`.
2. **The emulator lechonk-qa:**

   ```
   sdkmanager "system-images;android-35;google_apis;arm64-v8a"
   avdmanager create avd -n lechonk-qa -k "system-images;android-35;google_apis;arm64-v8a" -d pixel_7
   ```

   QA boots it headless on port 5580, so it never meets the emulator you use by hand. Its first boot is cold and
   saves a clean snapshot, `qa-clean`; later runs start from it and never save over it.
3. **Maestro**, which needs Java 17 or newer: `curl -fsSL "https://get.maestro.mobile.dev" | bash`, then a new
   terminal so it's on your PATH, or `LECHONK_MAESTRO` in `.env`.
4. **The screenshot check.** A tool's result is text only, so screenshots reach the model as an image beside it
   ([friction 35](../FRICTIONS.md#the-demo)). Check that the model reads them, with a screenshot of the game:

   ```
   uv run python scripts/check_screenshot.py --image <screenshot.png> --expect-hearts 5
   uv run python scripts/check_screenshot.py --serial emulator-5554 --expect-hearts 5
   ```

   The second takes the screenshot from the emulator you run by hand. PASS: QA can see. FAIL or a refusal:
   `tool_inputs()` in `src/le_chonk_eval/qa/agent.py` needs another way, before anything else.

Run QA on a release:

```
uv run le-chonk qa run --repo https://github.com/<you>/le-chonk-game --issue 1 --release issue-1-20261021-202046
```

`--serial emulator-5554` uses an emulator that's already running instead of booting lechonk-qa: for a quick try only.
An emulator that has run a game for hours slows down until Android kills the UI tree's dump. QA then stops and says
the device broke, rather than judging what's left from screenshots, and the criteria after it aren't checked. The table at the
end has a line per criterion. `runs/qa/<issue>-<release>/` has `verdicts.json`, `report.md`, the screenshots and
the flows; the flows kept for later runs are in `runs/qa/flows/`. `LECHONK_QA_MODEL` sets the agent's model.

The same run is the workflow le-chonk-qa, on your Mac's deployment: `scripts/redeploy_next.py` restarts your worker,
which then registers it. Nothing starts it by itself yet; in Studio > Workflows, start it with
`{"repo": "<you>/le-chonk-game", "number": 1, "release": "<tag>"}`.

The release's APK is the verifier's build, a debug one, so the descriptions that only debug builds have are in it.
Vibe's task asks for them: what the criteria name, the app describes in its accessibility tree.

## Not checked against your accounts yet

- That a workflow running on your behalf may start runs on your Mac's deployment and its own next execution, and
  that the results come back as the code reads them.
- The first build of the managed deployment's image.
- QA: that the model reads screenshots sent beside a tool's result, and the adb, emulator and Maestro commands on
  a real emulator. Its tests use a fake phone.

If something fails, `uv run python scripts/status_next.py` shows the latest runs of the four workflows: their steps, which
failed and why, and their warnings. It saves them to `runs/status/`. A watcher that can't read the channel three
times in a row says so in the channel. The demo still runs the same flow from your Mac: `uv run le-chonk demo`.
