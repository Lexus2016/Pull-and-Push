"""Hidden test suite — lives in the project's metrics/ dir (outside the artifact), so the
executor cannot see or edit it. The pytest-pass adapter runs it with the artifact on
PYTHONPATH and reports the fraction passing."""

from solution import fib, is_prime


def test_is_prime_basic():
    assert [n for n in range(2, 20) if is_prime(n)] == [2, 3, 5, 7, 11, 13, 17, 19]


def test_is_prime_edges():
    assert not is_prime(0)
    assert not is_prime(1)
    assert not is_prime(-7)
    assert is_prime(7919)          # a known prime


def test_fib_sequence():
    assert [fib(i) for i in range(9)] == [0, 1, 1, 2, 3, 5, 8, 13, 21]


def test_fib_larger():
    assert fib(20) == 6765
