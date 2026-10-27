# Frictions: from local runs to Harbor and Mistral's cloud

A running log of what got in the way, with the source of each item. Found while building this project in
October 2026 (Harbor 0.24.0, mistralai-workflows 3.16.0, mistralai 2.10.1). Add your own as you run it.

## Harbor and the Kotlin Toolchain

| # | Friction | Effect here | Source |
|---|---|---|---|
| 1 | Harbor's Docker environments are Linux, and iOS builds need macOS with Xcode | No iOS build in a trial; the eight assertions don't need one, and the instruction says so | Harbor `EnvironmentConfig.os`; Kotlin Toolchain iOS docs |
| 2 | Android's Linux build tools are published for x86-64 (assumption: check it with the oracle on arm64) | The image is `linux/amd64` and runs under Rosetta on Apple Silicon: slower builds | Dockerfile comment |
| 3 | The toolchain never accepts Android SDK licences by itself, and a container has no one to answer | The image accepts them while it's built, for `$ANDROID_HOME` and for any SDK the toolchain provisions | [kotlin-toolchain PR #17](https://github.com/JetBrains/kotlin-toolchain/pull/17), closed pending KTC-5695 |
| 4 | Scaffolding without prompts isn't documented: `kotlin init <template>` in the CLI, `kotlin new` in the Kotlin docs | The warm-up and the oracle try both | [Kotlin docs](https://kotlinlang.org/docs/multiplatform/kotlin-toolchain.html), [Amper releases](https://github.com/JetBrains/amper/releases) |

## Harbor and Mistral

| # | Friction | Effect here | Source |
|---|---|---|---|
| 5 | Harbor has no agent for Studio agents (Agents API) | `studio_agent.py` adapts a Studio agent to Harbor | Harbor `agents/` |
| 6 | Harbor and Workflows have no integration | Each arm is an activity that runs the `harbor` CLI as a subprocess, as with ZenML | — |
| 7 | Harbor owns the agent loop, so Workflows sees jobs, not model calls | Workflows' durable agents aren't used; each trial is still a conversation in Studio | Workflows durable agents docs |
| 8 | Managed deployments run your worker as a Linux container built from a GitHub repo, public or private, with no Docker inside | The worker stays on your Mac. A Harbor cloud sandbox (`-e daytona` or `-e modal`) would let the worker move to Mistral's cloud | [Managed deployments](https://docs.mistral.ai/studio/workflows/managing-workflows-in-production/managed-deployments/getting-started), `harbor run --help` |

## Studio

| # | Friction | Effect here | Source |
|---|---|---|---|
| 9 | Studio Observability and offline Evaluations, including the Evaluate dashboards, are Enterprise-only; Observability is in Private Preview | No built-in A/B dashboard on Pro with pay-as-you-go, so this project writes its own | [Observability](https://docs.mistral.ai/studio/observability), [Evaluations](https://docs.mistral.ai/studio/observability/evaluations) |
| 10 | Workflows is in Public Preview | APIs may change; the SDK is pinned | [Workflows overview](https://docs.mistral.ai/studio/workflows/getting-started/overview) |
| 11 | Trace APIs work per execution; there is no documented query across executions | Results are aggregated from Harbor's job folders | [Workflow observability](https://docs.mistral.ai/studio/workflows/observability) |
| 12 | The Agents API's built-in code execution runs Python only, in Mistral's sandbox | The Kotlin Toolchain needs your own container | [Agents API announcement](https://mistral.ai/fr/news/agents-api) |

## From the first version: agent tools as Workflows activities

| # | Friction | Source |
|---|---|---|
| 13 | `Runner.run` returns only the final outputs, not token usage | SDK `plugins/mistralai/runner.py` |
| 14 | The plugin executes any registered activity whose name the model returns, not only the agent's tools | SDK `plugins/mistralai/tool.py` |
| 15 | Outside a workflow, the default `run_as=AUTO` refuses to call the API ("requires workflow context"), which is undocumented | SDK `workflows/client.py` |
| 16 | `from mistralai.client import ...` in workflow code fails in the Temporal sandbox; only `mistralai.client.models` is passed through | SDK `core/sandbox.py` |
| 17 | Agents created by `RemoteSession` are never cleaned up, and every run with an id updates the agent | [Durable agents](https://docs.mistral.ai/studio/workflows/building-workflows/durable_agents) |

## The demo

| # | Friction | Effect here | Source |
|---|---|---|---|
| 18 | A Studio agent's function tools come back to the caller to run | The triage agent's GitHub tools run in the Slack app on your Mac, with your token | Agents API, function calling |
| 19 | The Workflows SDK has schedules but no event triggers, such as GitHub webhooks or Slack events | `le-chonk-issues` polls the repo every minute with a durable timer, and uses continue-as-new | SDK `exports.py` |
| 20 | Vibe Code Web's sandbox can't be configured yet: setup scripts and custom sandboxes are planned | Vibe runs as a CLI in the task's own container, through Harbor's `vibe` agent | [Vibe Code Web limits](https://docs.mistral.ai/vibe/code/vibe-code-web/limits-and-lifecycle) |
| 21 | Vibe runs inside the container, so Harbor's `vibe` agent passes the Mistral key in | Use a separate key for the demo; the GitHub token stays on the Mac | Harbor `agents/installed/vibe.py` |
| 22 | A child workflow runs on its parent's deployment: `execute_workflow` takes a workflow class and no task queue | In the [next phase](docs/architecture.md), the cloud deployment starts the Mac's runs through the Workflows API | SDK `core/workflow.py` |
| 23 | Studio's Slack connector signs in as you (a user token), so what it sends posts as you | The agent's replies start with 🤖, and le-chonk-slack skips its own messages by that mark | [Slack MCP server](https://docs.slack.dev/ai/slack-mcp-server/) scopes; `slack_send_message` |
| 24 | The GitHub connector has no tool to make labels or releases, or to upload a release's file | GitHub makes a label the first time it's set, and `le-chonk next start` colors them with your token; your Mac makes the release with the APK | The connector's tools (`scripts/check_next.py`) |
| 25 | A workflow's connectors need on-behalf-of credentials, which need a hardened deployment, or credentials stored beforehand for the deployment's identity | The cloud workflows are on-behalf-of, started from your Mac with your key; `le-chonk-control` holds no secret | [Connectors in workflows](https://docs.mistral.ai/studio/workflows/building-workflows/connectors), [hardened deployments](https://docs.mistral.ai/studio/workflows/managing-workflows-in-production/hardened_deployments) |
| 26 | httpx can't be imported in Temporal's workflow sandbox: it subclasses `urllib.request.Request` | The workflow modules import only names; the clients are imported inside activities, and a test imports each workflow in the sandbox | temporalio's sandbox restrictions |
| 27 | Continue-as-new, and a child workflow, start without the on-behalf-of token: only activities get it | The watchers never continue as new: when their history grows long, an activity starts their next execution (`-a`, `-b`) through the API, with their state | SDK `core/temporal/context_handler_interceptor.py` |
| 28 | A worker doesn't start without a deployment name, and the SDK reads it when it's imported | Every worker here sets one: `le-chonk` for the demo, `le-chonk-vibe` for the next phase; executions are started on it by name | SDK `core/config/config.py` |
| 29 | A connector tool's error raises a non-retryable error that only says "connector tool call failed" | The clients turn it into an error with the tool's own text and status, so a 404 reads as missing and activities retry the rest | SDK `plugins/mistralai/connectors/activities.py` |
| 30 | A workflow's dict result is stored as `{"result": ...}` (assumption: the API returns it that way) | Results read through the API are unwrapped | SDK `core/definition/validation/_parameter_conversion.py` |
| 31 | Slack's read tools answer with text written for a model: messages as `===` blocks inside a JSON string, without the subtypes and thread fields Slack's API has | le-chonk-slack parses the text: a message by its `Message TS:` line, system messages by their words, and a reply also sent to the channel by its thread | `scripts/check_next.py --channel` samples |
| 32 | The GitHub connector has a `create_repository` tool, but GitHub refuses it (403, "Resource not accessible by integration"): the Mistral GitHub App doesn't ask for the Administration permission, which creating a repository needs. Installing it on more repositories doesn't change that | You create the app's repo on GitHub and give the App access to it; asked for a new repo, the agent says so | First run of le-chonk-slack; the App's permissions: `GET https://api.github.com/apps/mistralai` |
| 33 | Slack shows what Studio's Slack connector sends as "Sent using Mistral Vibe": the Slack app behind the connector carries Vibe's name, whichever Mistral product sends | A Studio agent's replies read as Vibe's; only the 🤖 mark at their start tells them apart from yours | First run of le-chonk-slack |
| 34 | The Mistral GitHub App subscribes to issue, comment, pull request and push events, but a workflow can't start on them | le-chonk-tasks reads the issues every minute to find `vibe:ready` ones | `GET https://api.github.com/apps/mistralai` (its events); [scheduling](https://docs.mistral.ai/studio/workflows/building-workflows/scheduling) |
| 35 | A function result is text only: `FunctionResultEntry.result` is a string, so a tool can't hand the model a screenshot | The QA agent's screenshots go in as an image in a user message, in the same append as the results; `scripts/check_screenshot.py` checks that the model reads them | mistralai 2.10.1, `FunctionResultEntry` |
| 36 | The SDK's loop for local MCP servers (`RunContext` with a stdio client) raises on any result that isn't text: "Only supporting text tool responses for now." | Phone MCP servers such as mobile-mcp and Maestro MCP return screenshots, so the QA agent runs its own loop with function tools | mistralai 2.10.1, `extra/mcp/base.py` |
| 37 | Found no Mistral model or tool for computer use: operating a screen, finding where to tap in a screenshot | The QA agent taps by the UI tree's elements; what an app draws on a Canvas is checked from screenshots, or from descriptions the app adds | Mistral's docs and model cards, October 2026 |
| 38 | Greedy sampling is refused: `temperature: 0` alone gets a 400, "top_p must be 1 when using greedy sampling", unless `top_p: 1` is sent too | The judge and `scripts/check_screenshot.py` send both. Before that, the judge failed on both real runs that reached it (#1 on 21 Oct, #3 on 22 Oct) | Chat Completions and Conversations, `mistral-large-4`, 21–22 Oct 2026 |
| 39 | The SDK's default timeout is httpx's 5 seconds: `Mistral()` without `timeout_ms` gives every request 5 s, and a model's answer with screenshots can take longer. With connection retries on, the client sends the same request again until its retry budget is spent | QA's run of #5 stopped after 5 minutes of retries on one append. The clients now wait up to 5 minutes per request, and a criterion whose model call still fails is marked not checked while the run goes on | mistralai 2.10.1, `httpx.AsyncClient` defaults; QA of #5, 26 Oct 2026 |
| 40 | Harbor's `vibe` agent writes only the input and output prices into Vibe's model config. Without `cached_input_price`, Vibe bills cached input at the input price, and without `supports_images` it offers the model no images | In #9, 92% of 4.3M input tokens were cached: Vibe counted $3.15 and stopped at the $3 limit, about $0.75 at the cached price. It couldn't see the sprites it downloaded, so it wrote a PNG decoder and read them as ASCII art. Our Vibe agent adds both settings from LiteLLM's table | mistral-vibe 3.0.3, `vibe/core/config/models.py`; Harbor `agents/installed/vibe.py`; run of #9, 26 Oct 2026 |

## Your observations

| Date | Step (image build, oracle, local A/B, worker, Studio) | What happened | Workaround | Severity (1–3) |
|---|---|---|---|---|
| | | | | |
