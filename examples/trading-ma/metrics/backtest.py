#!/usr/bin/env python3
"""Honest backtest harness for the trading-ma example.

Lives OUTSIDE the artifact (in the project's metrics/ dir) so the Executor cannot
edit it — it only edits strategy.py. Reads real BTCUSDT 5m closes from data.csv next
to this file, imports PARAMS from the strategy.py in the current working directory
(the artifact), runs an MA-crossover backtest, and prints JSON metrics.

Stdlib only. Output: {"sharpe":..,"total_return_pct":..,"max_drawdown":..}
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import pathlib
import statistics

HERE = pathlib.Path(__file__).resolve().parent
BARS_PER_YEAR = 105_120  # 5-minute bars, 24/7 crypto


def load_prices() -> list[float]:
    with open(HERE / "data.csv", newline="") as f:
        return [float(r["close"]) for r in csv.DictReader(f)]


def load_params() -> dict:
    spec = importlib.util.spec_from_file_location("strategy", "strategy.py")  # cwd = artifact
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return dict(getattr(m, "PARAMS", {}))


def sma(xs, w, i):
    return sum(xs[i + 1 - w:i + 1]) / w if i + 1 >= w else None


def run(p: dict, prices: list[float]) -> dict:
    fast = max(1, int(p.get("fast", 3)))
    slow = max(2, int(p.get("slow", 8)))
    if fast >= slow:
        slow = fast + 1
    long_only = bool(p.get("long_only", True))
    tf = int(p.get("trend_filter", 0) or 0)

    rets, pos = [], 0
    for i in range(len(prices) - 1):
        f, s = sma(prices, fast, i), sma(prices, slow, i)
        if f is not None and s is not None:
            want_long = f > s
            if tf > 0:
                ref = sma(prices, tf, i)
                want_long = want_long and ref is not None and prices[i] > ref
            pos = 1 if want_long else (0 if long_only else -1)
        rets.append(pos * (prices[i + 1] - prices[i]) / prices[i])

    mean = statistics.fmean(rets) if rets else 0.0
    std = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mean / std * math.sqrt(BARS_PER_YEAR)) if std > 0 else 0.0

    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak * 100)
    return {
        "sharpe": round(sharpe, 4),
        "total_return_pct": round((eq - 1) * 100, 3),
        "max_drawdown": round(mdd, 3),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strategy")
    ap.parse_args()
    print(json.dumps(run(load_params(), load_prices())))
