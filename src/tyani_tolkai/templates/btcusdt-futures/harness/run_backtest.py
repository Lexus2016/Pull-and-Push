#!/usr/bin/env python3
"""Scoring harness (the JUDGE) for the BTCUSDT 5m futures project — DO NOT EDIT, and the executor
must not touch it. Lives outside the artifact so the loop's git-clean can't wipe it and the strategy
can't grade its own exam.

PROFESSIONAL EVALUATION (walk-forward / out-of-sample). A backtest that scores on the SAME data the
optimizer tunes on rewards overfitting — an amateur result that collapses on unseen data. So this
harness splits the series: the strategy may look at the whole history (signals are causal), but the
SCORE is measured ONLY on the held-out tail (the last 30% — out-of-sample). A strategy that scores
well here generalised; one that memorised the past does not.

It imports PARAMS and signals(bars) from the strategy.py in the cwd (the artifact), runs the FIXED
leveraged long/short simulation (stop-loss, liquidation, taker fee, reinvested equity), and prints
ONE JSON object. Scored keys (all measured on the OUT-OF-SAMPLE tail):
    return_oos_pct, liquidations, max_drawdown_pct, max_drawdown_days
The rest are report-only (full-period return, in-sample return, win rate, profit factor, trades,
period, the OOS split point).

Stdlib only (CPython or PyPy). Usage: run_backtest.py [--strategy strategy.py] [--symbol X] [--oos 0.3]
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
                         float(r["low"]), float(r["close"]), float(r.get("volume", 0.0) or 0.0)))
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
    ap.add_argument("--oos", type=float, default=0.3)   # fraction held out for scoring (last 30%)
    args = ap.parse_args()

    strat = load_strategy()
    p = dict(getattr(strat, "PARAMS", {}))
    lev = min(50.0, max(1.0, float(p.get("leverage", 3.0))))
    risk = min(1.0, max(0.0, float(p.get("risk_frac", 0.5))))
    stop_pct = max(0.05, float(p.get("stop_pct", 2.0))) / 100.0
    take_pct = max(0.0, float(p.get("take_pct", 0.0))) / 100.0

    bars = load_bars()
    if not hasattr(strat, "signals") or not callable(strat.signals):
        raise SystemExit("strategy.py must define signals(bars) -> list of -1/0/1 per bar")
    want_seq = list(strat.signals(bars))
    if len(want_seq) != len(bars):
        raise SystemExit(f"signals() returned {len(want_seq)} positions for {len(bars)} bars")

    oos_frac = min(0.9, max(0.05, args.oos))
    oos_start = int(len(bars) * (1.0 - oos_frac))       # index where the held-out tail begins

    equity = START_EQUITY
    pos = None
    liquidations = 0                     # full-run (report)
    trades: list[float] = []             # full-run realized PnL (report)
    peak = equity; max_dd = 0.0; underwater_ms = 0; peak_ts = bars[0][0] if bars else 0
    # out-of-sample trackers (the SCORED segment) — initialised when we cross oos_start
    oos = {"eq0": None, "peak": None, "dd": 0.0, "uw": 0, "peak_ts": 0, "liq0": 0, "tr0": 0}

    def close_pos(exit_price):
        nonlocal equity, pos
        move = (exit_price - pos["entry"]) / pos["entry"] * pos["side"]
        gross = pos["notional"] * move - pos["notional"] * COMMISSION
        equity += gross
        trades.append(gross - pos["entry_fee"])
        pos = None

    for i, (ts, o, hi, lo, c, _v) in enumerate(bars):
        if i == oos_start:               # entering the held-out tail → start OOS accounting
            oos.update(eq0=equity, peak=equity, dd=0.0, uw=0, peak_ts=ts,
                       liq0=liquidations, tr0=len(trades))

        if pos is not None:
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

        want = want_seq[i]
        if pos is not None and want != 0 and want != pos["side"]:
            close_pos(c)
        if pos is None and want != 0 and equity > 0.01:
            margin = risk * equity
            notional = margin * lev
            entry_fee = notional * COMMISSION
            equity -= entry_fee
            stop = c * (1 - stop_pct) if want == 1 else c * (1 + stop_pct)
            liq = c * (1 - 1.0 / lev) if want == 1 else c * (1 + 1.0 / lev)
            take = (c * (1 + take_pct) if want == 1 else c * (1 - take_pct)) if take_pct else 0.0
            pos = {"side": want, "entry": c, "notional": notional, "margin": margin,
                   "stop": stop, "take": take, "liq": liq, "entry_fee": entry_fee}

        # full-run drawdown (report)
        if equity > peak:
            peak = equity; peak_ts = ts
        else:
            underwater_ms = max(underwater_ms, ts - peak_ts)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)
        # out-of-sample drawdown (scored)
        if oos["eq0"] is not None:
            if equity > oos["peak"]:
                oos["peak"] = equity; oos["peak_ts"] = ts
            else:
                oos["uw"] = max(oos["uw"], ts - oos["peak_ts"])
                if oos["peak"] > 0:
                    oos["dd"] = max(oos["dd"], (oos["peak"] - equity) / oos["peak"] * 100.0)

    if pos is not None and bars:
        close_pos(bars[-1][4])
        if equity > peak:
            peak = equity

    # ---- out-of-sample scored metrics ----
    oos_eq0 = oos["eq0"] if oos["eq0"] and oos["eq0"] > 0 else START_EQUITY
    return_oos = (equity / oos_eq0 - 1.0) * 100.0
    liq_oos = liquidations - oos["liq0"]
    oos_trades = trades[oos["tr0"]:]

    # ---- report-only stats ----
    num_trades = len(oos_trades)
    wins = [x for x in oos_trades if x > 0]
    gross_profit = sum(x for x in oos_trades if x > 0)
    gross_loss = -sum(x for x in oos_trades if x < 0)
    win_rate = (len(wins) / num_trades * 100.0) if num_trades else 0.0
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None
    in_sample_return = ((oos_eq0 / START_EQUITY - 1.0) * 100.0)

    out = {
        # scored metrics — measured on the OUT-OF-SAMPLE tail (generalisation, not memorisation)
        "return_oos_pct": round(return_oos, 4),
        "liquidations": liq_oos,
        "max_drawdown_pct": round(oos["dd"], 4),
        "max_drawdown_days": round(oos["uw"] / MS_PER_DAY, 4),
        # report-only
        "full_return_pct": round((equity / START_EQUITY - 1.0) * 100.0, 4),
        "in_sample_return_pct": round(in_sample_return, 4),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": profit_factor,
        "num_trades": num_trades,
        "oos_from": _iso(bars[oos_start][0]) if bars and oos_start < len(bars) else None,
        "period_start": _iso(bars[0][0]) if bars else None,
        "period_end": _iso(bars[-1][0]) if bars else None,
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
