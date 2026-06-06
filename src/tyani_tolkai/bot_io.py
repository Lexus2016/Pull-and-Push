"""OHLCV CSV loader for the P3 scoring CLI (spec: docs/design/p3.3-orchestrator-integration.md).

Returns (o, h, l, c, v) 5-tuples — the timestamp column is dropped, matching the P3.1 engine's
no-absolute-time convention (anti-exfiltration).
"""
from __future__ import annotations

import csv
from pathlib import Path

_REQUIRED = ("open", "high", "low", "close")


def load_bars_csv(path: str | Path) -> list[tuple]:
    """Load an OHLCV CSV (header time,open,high,low,close,volume) into (o,h,l,c,v) float tuples.

    The `time` column is dropped. Missing/empty `volume` becomes 0.0. Raises ValueError if any of
    open/high/low/close is absent.
    """
    bars: list[tuple] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        missing = [c for c in _REQUIRED if c not in fields]
        if missing:
            raise ValueError(f"CSV missing required column(s): {', '.join(missing)}")
        for row in reader:
            vol = row.get("volume", "") or ""
            bars.append((
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(vol) if vol.strip() else 0.0,
            ))
    return bars
