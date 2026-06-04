#!/usr/bin/env python3
"""Scoring harness (the JUDGE) for the BTCUSDT 5m futures project — DO NOT EDIT, and the
executor must not touch it. Lives outside the artifact so the loop's git-clean can't wipe it
and the strategy can't grade its own exam.

It owns the FIXED market simulation and the objective, NOT the trading decisions:
  * reads real BTCUSDT 5m OHLCV from data.csv (next to this file),
  * imports PARAMS and signals() from the strategy.py in the cwd (the artifact) — the strategy
    decides the position per bar; the harness just executes it,
  * simulates a leveraged long/short book: position sizing from risk_frac, mandatory stop-loss,
    liquidation, take-profit, taker commission, reinvested equity,
  * prints ONE JSON object. The first four keys are the scored metrics; the rest are report-only
    stats (win rate, profit factor, trade count, tested period) — ignored by the scorer, shown
    in the report:

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
                         float(r["low"]), float(r["close"]),
                         float(r.get("volume", 0.0) or 0.0)))
    return rows


def load_strategy():
    spec = importlib.util.spec_from_file_location("strategy", "strategy.py")  # cwd = artifact
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="strategy.py")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.parse_args()

    strat = load_strategy()
    p = dict(getattr(strat, "PARAMS", {}))
    # execution / risk knobs — the strategy's choices, applied (clamped) by the harness
    lev = min(50.0, max(1.0, float(p.get("leverage", 3.0))))
    risk = min(1.0, max(0.0, float(p.get("risk_frac", 0.5))))
    stop_pct = max(0.05, float(p.get("stop_pct", 2.0))) / 100.0
    take_pct = max(0.0, float(p.get("take_pct", 0.0))) / 100.0

    bars = load_bars()

    # The STRATEGY decides positions (all signal logic + indicators live in strategy.py).
    if not hasattr(strat, "signals") or not callable(strat.signals):
        raise SystemExit("strategy.py must define signals(bars) -> list of -1/0/1 per bar")
    want_seq = list(strat.signals(bars))
    if len(want_seq) != len(bars):
        raise SystemExit(f"signals() returned {len(want_seq)} positions for {len(bars)} bars")

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

    for i, (ts, o, hi, lo, c, _v) in enumerate(bars):
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

        want = want_seq[i]                     # the strategy's desired position at this bar
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

        # drawdown tracking on realized equity
        if equity > peak:
            peak = equity; peak_ts = ts
        else:
            underwater_ms = max(underwater_ms, ts - peak_ts)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)

    if pos is not None and bars:
        close_pos(bars[-1][4])
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
        # report-only stats
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
