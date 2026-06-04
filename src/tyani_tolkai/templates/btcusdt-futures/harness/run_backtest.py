#!/usr/bin/env python3
"""Scoring harness for the BTCUSDT 5m futures project — DO NOT EDIT (and the executor
must not touch it). Lives in the committed seed so it survives the loop's git-clean.

Reads real BTCUSDT 5m OHLCV from data.csv (next to this file), imports PARAMS from the
strategy.py in the current working directory (the artifact), runs a leveraged long/short
trend backtest with mandatory stop-loss, liquidation, taker commission and reinvested
equity, then prints ONE JSON object. The first four keys are the scored metrics; the rest
are report-only stats (the operator asked for win rate, profit factor, trade count and the
tested period — they are ignored by the scorer but shown in the report/log):

    {"total_return_pct":.., "liquidations":.., "max_drawdown_pct":.., "max_drawdown_days":..,
     "win_rate_pct":.., "profit_factor":.., "num_trades":.., "period_start":"..", "period_end":".."}

Stdlib only (runs under CPython or PyPy). Usage: run_backtest.py [--strategy strategy.py] [--symbol X]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import importlib.util
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
START_EQUITY = 100.0
COMMISSION = 0.0005          # 0.05% taker per side
MS_PER_DAY = 86_400_000


def load_bars():
    rows = []
    with open(HERE / "data.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows.append((int(float(r["time"])), float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])))
    return rows


def load_params() -> dict:
    spec = importlib.util.spec_from_file_location("strategy", "strategy.py")  # cwd = artifact
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return dict(getattr(m, "PARAMS", {}))


def sma(xs, w, i):
    return sum(xs[i + 1 - w:i + 1]) / w if w > 0 and i + 1 >= w else None


def _iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="strategy.py")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.parse_args()

    p = load_params()
    fast = max(1, int(p.get("fast", 20)))
    slow = max(fast + 1, int(p.get("slow", 60)))
    trend = max(0, int(p.get("trend_filter", 0)))
    lev = min(50.0, max(1.0, float(p.get("leverage", 3.0))))
    risk = min(1.0, max(0.0, float(p.get("risk_frac", 0.5))))
    stop_pct = max(0.05, float(p.get("stop_pct", 2.0))) / 100.0
    take_pct = max(0.0, float(p.get("take_pct", 0.0))) / 100.0
    allow_short = bool(p.get("allow_short", True))

    bars = load_bars()
    closes = [b[4] for b in bars]
    equity = START_EQUITY
    pos = None                      # {side, entry, notional, margin, stop, take, liq, entry_fee}
    liquidations = 0
    trades: list[float] = []        # realized PnL per closed trade (entry+exit fees included)
    peak = equity
    max_dd = 0.0
    underwater_ms = 0
    peak_ts = bars[0][0] if bars else 0

    def close_pos(exit_price):
        nonlocal equity, pos
        move = (exit_price - pos["entry"]) / pos["entry"] * pos["side"]
        gross = pos["notional"] * move - pos["notional"] * COMMISSION   # exit-side fee
        equity += gross
        trades.append(gross - pos["entry_fee"])                        # net of entry fee too
        pos = None

    warmup = max(slow, trend) + 1
    for i, (ts, o, hi, lo, c) in enumerate(bars):
        if pos is not None:
            # intra-bar: liquidation first (worst case), then stop, then take
            if pos["side"] == 1:
                if lo <= pos["liq"]:
                    equity -= pos["margin"]; liquidations += 1
                    trades.append(-pos["margin"] - pos["entry_fee"]); pos = None
                elif lo <= pos["stop"]:
                    close_pos(pos["stop"])
                elif pos["take"] and hi >= pos["take"]:
                    close_pos(pos["take"])
            else:
                if hi >= pos["liq"]:
                    equity -= pos["margin"]; liquidations += 1
                    trades.append(-pos["margin"] - pos["entry_fee"]); pos = None
                elif hi >= pos["stop"]:
                    close_pos(pos["stop"])
                elif pos["take"] and lo <= pos["take"]:
                    close_pos(pos["take"])

        if equity <= 0.01:
            equity = 0.0
            break

        f, s = sma(closes, fast, i), sma(closes, slow, i)
        t = sma(closes, trend, i) if trend else None
        if i >= warmup and f is not None and s is not None:
            want = 0
            if f > s and (t is None or c > t):
                want = 1
            elif f < s and allow_short and (t is None or c < t):
                want = -1

            if pos is not None and want != 0 and want != pos["side"]:
                close_pos(c)                       # flip: close current at close price
            if pos is None and want != 0 and equity > 0.01:
                margin = risk * equity
                notional = margin * lev
                entry_fee = notional * COMMISSION
                equity -= entry_fee                # entry commission
                stop = c * (1 - stop_pct) if want == 1 else c * (1 + stop_pct)
                liq = c * (1 - 1.0 / lev) if want == 1 else c * (1 + 1.0 / lev)
                take = (c * (1 + take_pct) if want == 1 else c * (1 - take_pct)) if take_pct else 0.0
                pos = {"side": want, "entry": c, "notional": notional, "margin": margin,
                       "stop": stop, "take": take, "liq": liq, "entry_fee": entry_fee}

        # drawdown tracking on mark-to-market-ish equity (use realized equity)
        if equity > peak:
            peak = equity; peak_ts = ts;
        else:
            underwater_ms = max(underwater_ms, ts - peak_ts)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)

    if pos is not None and closes:
        close_pos(closes[-1])
        if equity > peak:
            peak = equity

    # ---- report-only stats (not scored) ----
    num_trades = len(trades)
    wins = [x for x in trades if x > 0]
    gross_profit = sum(x for x in trades if x > 0)
    gross_loss = -sum(x for x in trades if x < 0)
    win_rate = (len(wins) / num_trades * 100.0) if num_trades else 0.0
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None  # None = no losing trades

    out = {
        # scored metrics
        "total_return_pct": round((equity / START_EQUITY - 1.0) * 100.0, 4),
        "liquidations": liquidations,
        "max_drawdown_pct": round(max_dd, 4),
        "max_drawdown_days": round(underwater_ms / MS_PER_DAY, 4),
        # report-only stats (operator request)
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": profit_factor,
        "num_trades": num_trades,
        "period_start": _iso(bars[0][0]) if bars else None,
        "period_end": _iso(bars[-1][0]) if bars else None,
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
