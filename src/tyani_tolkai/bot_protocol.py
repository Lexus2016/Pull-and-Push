"""Wire protocol for the P3 bot evaluation core (spec: docs/design/p3-scoring-core.md).

JSON-lines over the bot subprocess's stdin/stdout. Strict request->response. Bars carry NO
absolute timestamp (only OHLCV + a counter) so the bot cannot fingerprint the date range and
recall it from training memory. The scored OOS tail starts at a seeded-but-hidden offset.
"""
from __future__ import annotations

import hashlib
import json

INIT = "init"
READY = "ready"
BAR = "bar"
ORDER = "order"
END = "end"

OOS_LO = 0.60
OOS_HI = 0.80


def encode(msg: dict) -> str:
    """Serialize one protocol message to a single newline-terminated JSON line."""
    return json.dumps(msg, separators=(",", ":")) + "\n"


def decode(line: str) -> dict:
    """Parse one protocol line into a dict. Raises ValueError if it is not a JSON object."""
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("protocol message must be a JSON object")
    return obj


def seeded_oos_start(n_bars: int, *, seed: str, lo: float = OOS_LO, hi: float = OOS_HI) -> int:
    """Deterministic, bot-hidden start index of the scored OOS tail.

    Reproducible for a given (n_bars, seed) so the determinism check works, but unpredictable
    to the bot (which never sees the seed), so it cannot behave differently on the scored tail.
    """
    if n_bars <= 0:
        return 0
    lo_i = int(lo * n_bars)
    hi_i = int(hi * n_bars)
    if hi_i <= lo_i:
        return min(lo_i, n_bars)
    digest = hashlib.sha256(f"{seed}:{n_bars}".encode()).hexdigest()
    span = hi_i - lo_i
    return lo_i + (int(digest, 16) % span)
