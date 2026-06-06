import sys

from tyani_tolkai.sandbox import LocalBackend, get_backend


def test_local_runs_command(tmp_path):
    be = LocalBackend()
    res = be.run(f'{sys.executable} -c "print(\'hi\')"', cwd=tmp_path, timeout=10)
    assert res.exit_code == 0
    assert "hi" in res.stdout
    assert not res.timed_out


def test_local_timeout(tmp_path):
    be = LocalBackend()
    res = be.run(f'{sys.executable} -c "import time; time.sleep(5)"', cwd=tmp_path, timeout=1)
    assert res.timed_out
    assert res.exit_code == 124


def test_get_backend():
    assert get_backend("local").name == "local"


def test_local_backend_python_is_host_interpreter():
    assert LocalBackend.python == sys.executable


def test_python_is_forward_slashed_for_cross_platform_split():
    # The interpreter path must be forward-slashed so shlex.split(posix=True) keeps it intact
    # on Windows (C:/...\python.exe works there) WHILE still unquoting -c "..." args correctly.
    import shlex
    assert "\\" not in LocalBackend.python
    argv = shlex.split('C:/Py/python.exe -c "print(1)" ../m/run.py', posix=True)
    assert argv == ["C:/Py/python.exe", "-c", "print(1)", "../m/run.py"]
