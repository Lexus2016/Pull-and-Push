"""Return every prime <= n, in ascending order."""


def primes_upto(n: int) -> list[int]:
    primes = []
    for k in range(2, n + 1):
        if all(k % d for d in range(2, int(k ** 0.5) + 1)):
            primes.append(k)
    return primes
