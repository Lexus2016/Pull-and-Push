import shutil

import pytest

from tyani_tolkai.sandbox import DockerBackend, get_backend

docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


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
