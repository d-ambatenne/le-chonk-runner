"""Names the cloud and the Mac share: connectors and deployments. Light on purpose: the workflows import it in
Temporal's sandbox, where httpx and the clients can't be imported."""

GITHUB = "github_app"  # Studio's GitHub connector
SLACK = "slack"  # Studio's Slack connector
CONTROL = "le-chonk-control"  # the managed deployment: the cloud workflows
VIBE = "le-chonk-vibe"  # your Mac's deployment: Vibe in Docker
