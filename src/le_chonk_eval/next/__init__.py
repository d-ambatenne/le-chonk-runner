"""The next phase: Studio in Mistral's cloud, Vibe on your Mac (docs/architecture.md, docs/next.md).

le-chonk-control, a managed deployment, runs the cloud workflows on behalf of whoever starts them:
    le-chonk-slack    watches a Slack channel through the Slack connector; the triage agent answers each message
    le-chonk-tasks    watches a repo's issues through the GitHub connector, runs each on your Mac, then the PR,
                      the judge, the merge, the labels and the replies
le-chonk-vibe, a worker you run on your Mac, has Docker, git and the GitHub token:
    le-chonk-vibe-run one Vibe run in the task's container; the branch and a release with the APK
    le-chonk-vibe-ab  the skill A/B, judged here; a pre-release with the report and one APK per arm
    le-chonk-qa       a release's APK on the emulator, checked against the issue's criteria (le_chonk_eval.qa)
"""
