"""Test only: a Harbor environment that runs a trial on this machine instead of in a container.

The container paths (/app, /logs, /tests, /solution, /harbor) are real paths here, so use it only on a
throwaway machine, as root, one trial at a time. It lets the integration test run real Harbor trials
(task loading, skill injection, agent, verifier, result files) without Docker.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult

CONTAINER_DIRS = ("/app", "/logs", "/tests", "/solution", "/harbor")


class HostEnvironment(BaseEnvironment):
    @staticmethod
    def type() -> str:
        return "host-test"

    def _validate_definition(self) -> None:
        pass

    async def start(self, force_build: bool) -> None:
        for path in CONTAINER_DIRS:
            shutil.rmtree(path, ignore_errors=True)
        for path in ("/app", "/logs/agent", "/logs/verifier", "/logs/artifacts"):
            Path(path).mkdir(parents=True, exist_ok=True)

    async def stop(self, delete: bool) -> None:
        pass

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source_path, target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        if Path(source_dir).is_dir():
            shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: str | int | None = None) -> ExecResult:
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=cwd or self.task_env_config.workdir or "/app",
            env={**os.environ, **(env or {})}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds")
        return ExecResult(stdout=out.decode(errors="replace") or None, stderr=err.decode(errors="replace") or None,
                          return_code=process.returncode or 0)
