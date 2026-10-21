"""The entrypoint of the le-chonk-control image (deploy/control/Dockerfile).

On a managed deployment, Mistral sets DEPLOYMENT_NAME, SERVER_URL and the service account's token: no API key.
"""

from __future__ import annotations

import asyncio


def main() -> None:
    import mistralai.workflows as workflows

    from .control import WORKFLOWS

    asyncio.run(workflows.run_worker(WORKFLOWS))


if __name__ == "__main__":
    main()
