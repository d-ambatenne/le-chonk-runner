# le-chonk-control: the next phase's cloud workflows, as a Mistral managed deployment (docs/next.md).
# Mistral builds this image from your GitHub repo and runs it as it is. Build context: the repo's root.
# Python only: Docker, git, Harbor and the GitHub token stay on your Mac (le-chonk-vibe). No API key either:
# Mistral mounts the deployment's service-account token and sets DEPLOYMENT_NAME and SERVER_URL.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.17
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# The dependencies first, so a change to the code doesn't install them again
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY demo ./demo
RUN uv sync --frozen --no-dev

CMD ["/app/.venv/bin/python", "-m", "le_chonk_eval.next.control_worker"]
