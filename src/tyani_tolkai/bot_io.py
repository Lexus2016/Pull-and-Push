"""OHLCV CSV loader for the P3 scoring CLI (spec: docs/design/p3.3-orchestrator-integration.md).

Returns (o, h, l, c, v) 5-tuples — the timestamp column is dropped, matching the P3.1 engine's
no-absolute-time convention (anti-exfiltration).
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

_REQUIRED = ("open", "high", "low", "close")


def load_bars_csv(path: str | Path) -> list[tuple]:
    """Load an OHLCV CSV (header time,open,high,low,close,volume) into (o,h,l,c,v) float tuples.

    The `time` column is dropped. Missing/empty `volume` becomes 0.0. Raises ValueError (naming the
    line) if any of open/high/low/close is absent, empty, non-numeric or non-finite, or if the
    `time` column is present but not strictly increasing — a NaN price turns the whole backtest
    into NaN, and newest-first data would silently be replayed backwards.
    """
    bars: list[tuple] = []
    last_t = None
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        missing = [c for c in _REQUIRED if c not in fields]
        if missing:
            raise ValueError(f"CSV missing required column(s): {', '.join(missing)}")
        for line, row in enumerate(reader, start=2):
            try:
                o, h, l, c = (float(row[k]) for k in _REQUIRED)
                vol = row.get("volume", "") or ""
                v = float(vol) if vol.strip() else 0.0
            except (TypeError, ValueError) as e:
                raise ValueError(f"CSV line {line}: empty or non-numeric OHLCV value ({e})") from None
            if not all(math.isfinite(x) for x in (o, h, l, c, v)):
                raise ValueError(f"CSV line {line}: non-finite OHLCV value")
            t = (row.get("time") or "").strip()
            if t:
                try:
                    tv = float(t)
                except ValueError:
                    tv = None                       # non-numeric timestamps: order not checked
                if tv is not None:
                    if last_t is not None and tv <= last_t:
                        raise ValueError(f"CSV line {line}: time must strictly increase "
                                         "(oldest bar first)")
                    last_t = tv
            bars.append((o, h, l, c, v))
    return bars
