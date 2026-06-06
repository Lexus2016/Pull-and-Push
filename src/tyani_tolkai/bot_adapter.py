"""Reference bot adapter that speaks the P3 streaming protocol.

A trusted reference implementation for testing the engine/runner end-to-end.
Momentum rule: want=1 if the latest close > the previous close, else 0.
First bar always returns want=0 (no previous close available).

Runnable as a module:
    python -m tyani_tolkai.bot_adapter
"""
from __future__ import annotations

from typing import List, Tuple

from tyani_tolkai import bot_protocol as bp

Bar = Tuple[float, float, float, float, float]  # (o, h, l, c, v)


def decide(bars: List[Bar]) -> int:
    """Return 1 if the last close > second-to-last close, else 0.

    First bar (no previous) returns 0.
    close is index 3 in the (o, h, l, c, v) tuple.
    """
    if len(bars) >= 2 and bars[-1][3] > bars[-2][3]:
        return 1
    return 0


def run_protocol_io(stdin, stdout) -> None:
    """Process the streaming protocol on the given IO objects.

    on INIT  -> write READY
    on BAR   -> accumulate bar, write ORDER with want=decide(history)
    on END   -> return
    Blank lines are ignored.
    """
    history: List[Bar] = []

    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue

        msg = bp.decode(line)
        msg_type = msg.get("type")

        if msg_type == bp.INIT:
            stdout.write(bp.encode({"type": bp.READY}))
            stdout.flush()

        elif msg_type == bp.BAR:
            bar: Bar = (msg["o"], msg["h"], msg["l"], msg["c"], msg["v"])
            history.append(bar)
            want = decide(history)
            stdout.write(bp.encode({"type": bp.ORDER, "n": msg["n"], "want": want}))
            stdout.flush()

        elif msg_type == bp.END:
            return


if __name__ == "__main__":
    import sys
    run_protocol_io(sys.stdin, sys.stdout)
