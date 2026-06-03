import shutil
import subprocess

import pytest

from tyani_tolkai.sandbox import DockerBackend, get_backend


def _docker_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


docker = pytest.mark.skipif(not _docker_up(), reason="docker daemon not available")


def test_get_backend_docker_configures():
    be = get_backend("docker", type("S", (), {"image": "img:1", "network": "none",
                                              "memory": "1g", "cpus": 2})())
    assert isinstance(be, DockerBackend)
    assert be.image == "img:1" and be.memory == "1g"


@docker
def test_docker_runs_echo(tmp_path):
    (tmp_path / "artifact").mkdir()
    be = DockerBackend(image="python:3.12-slim", network="none")
    res = be.run("echo hi", cwd=tmp_path / "artifact", timeout=120)
    assert res.exit_code == 0
    assert "hi" in res.stdout
