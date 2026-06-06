"""P3.2 Docker sandbox (spec: docs/design/p3.2-docker-sandbox.md).

Runs an untrusted bot inside a hardened `docker run` container and reuses the P3.1 protocol
driver unchanged. The held-out data and scoring code stay on the host and stream as bars over
the pipe; only the bot's own directory is mounted, read-only. Docker is the kernel-level jail
(network/FS/caps/resources) that P3.1's process separation did not provide.
"""
from __future__ import annotations

import subprocess
import uuid

from . import bot_protocol
from .bot_engine import simulate
from .bot_runner import drive_bot

SANDBOX_IMAGE = "python:3.11-slim"


class SandboxUnavailable(Exception):
    """Raised when sandboxing is requested but Docker is not available."""


def docker_available() -> bool:
    """True if the Docker CLI+daemon respond."""
    try:
        return subprocess.run(
            ["docker", "version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def build_docker_cmd(bot_cmd, *, bot_dir, container_name, image=SANDBOX_IMAGE,
                     mem_mb: int = 512, cpus: float = 1.0, pids: int = 64) -> list[str]:
    """The hardened `docker run` command that wraps the bot. Pure (no Docker call)."""
    return [
        "docker", "run", "--rm", "-i", "--name", container_name,
        "--network=none", "--read-only",
        "--tmpfs", "/tmp:rw,size=16m,noexec,nosuid",
        f"--memory={mem_mb}m", f"--cpus={cpus}", f"--pids-limit={pids}",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--user", "65534:65534",
        "--pull=never",
        "-v", f"{bot_dir}:/bot:ro", "-w", "/bot",
        "-e", "PYTHONPATH=/bot", "-e", "PYTHONDONTWRITEBYTECODE=1",
        image, *bot_cmd,
    ]
