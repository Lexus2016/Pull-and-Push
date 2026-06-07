"""P4.6 adapter scaffolding: render a starter ``adapter.py`` that speaks the P3 protocol.

The protocol plumbing (`run_protocol_io`) is vetted boilerplate — byte-identical for every bot and
feeding ``decide()`` only the history-so-far, so an adapter built on it CANNOT look ahead. The user
fills exactly one function, ``decide(history) -> int in {-1,0,1}``, to call their strategy.
`check-adapter` then proves the result is protocol-sound, deterministic, and non-degenerate before
any run trusts it. We deliberately do NOT LLM-generate `decide()` here (a wrong one mis-scores
silently — the failure this milestone exists to prevent); the stub is a forcing function.
"""
from __future__ import annotations


def _entry_hints(profile) -> list[str]:
    """Profile-derived comment lines to guide the human filling decide() (best-effort, never raises)."""
    lines: list[str] = []
    ep = getattr(profile, "entry_point", None)
    if ep is not None:
        loc = getattr(ep, "location", "?")
        inp = getattr(ep, "inputs", "")
        out = getattr(ep, "outputs", "")
        lines.append(f"#     entry point: {loc}")
        if inp:
            lines.append(f"#       consumes: {inp}")
        if out:
            lines.append(f"#       emits:    {out}")
    tun = getattr(profile, "tunable_surface", None) or []
    if tun:
        names = ", ".join(str(getattr(t, "name", "?")) for t in tun[:8])
        lines.append(f"#     tunables: {names}")
    return lines


def render_adapter_stub(*, profile=None) -> str:
    """Return the source of a starter ``adapter.py`` (vetted plumbing + a decide() stub)."""
    hints = _entry_hints(profile) if profile is not None else []
    hint_block = "\n".join(hints) if hints else \
        "#     (no P1 profile supplied — open your bot and wire its decision call here)"
    return f'''"""Auto-scaffolded P3 adapter. Fill in `decide()`; the rest is vetted boilerplate.

`decide(history)` gets every bar SEEN SO FAR (causal — look-ahead is impossible) and must return an
int position: 1 = long, 0 = flat, -1 = short. Make it a PURE function of `history` (rebuild any
state from it each call). Then prove it:
    pull-and-push check-adapter --bot-dir . --bot-cmd "python adapter.py"
"""
from __future__ import annotations

import sys
from typing import List, Tuple

from tyani_tolkai import bot_protocol as bp

Bar = Tuple[float, float, float, float, float]  # (open, high, low, close, volume)


def decide(history: List[Bar]) -> int:
    """Return 1 (long) / 0 (flat) / -1 (short) for the current bar = history[-1].

    TODO: call your bot's decision logic and map its output to {{-1, 0, 1}}.
    Your bot (from the P1 profile):
{hint_block}
    `history[-1]` is the latest bar; close is index 3.
    """
    # Unwired stub: flat (0) on every bar. `check-adapter` FLAGS this as degenerate until you wire
    # your strategy — that is the intended forcing function, not a bug.
    return 0


def run_protocol_io(stdin, stdout) -> None:
    """Vetted protocol plumbing — do not edit. Feeds decide() only history-so-far (no look-ahead)."""
    history: List[Bar] = []
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        msg = bp.decode(line)
        t = msg.get("type")
        if t == bp.INIT:
            stdout.write(bp.encode({{"type": bp.READY}}))
            stdout.flush()
        elif t == bp.BAR:
            history.append((msg["o"], msg["h"], msg["l"], msg["c"], msg["v"]))
            stdout.write(bp.encode({{"type": bp.ORDER, "n": msg["n"], "want": decide(history)}}))
            stdout.flush()
        elif t == bp.END:
            return


if __name__ == "__main__":
    run_protocol_io(sys.stdin, sys.stdout)
'''
