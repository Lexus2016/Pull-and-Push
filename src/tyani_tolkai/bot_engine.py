"""Order-driven backtest engine — faithful port of the harness simulation.

`simulate` is a pure function that replicates the economics of
`templates/btcusdt-futures/harness/run_backtest.py` exactly, with two
deliberate changes:

1. Order-driven: receives `orders` (list[int] in {-1, 0, 1}) instead of
   calling strat.signals().
2. Seeded OOS start: `oos_start` is passed in (already computed by the
   caller). Bars are plain (o, h, l, c, v) 5-tuples — no timestamp at
   index 0, so drawdown duration is measured in bars, not milliseconds
   (`max_drawdown_bars` instead of `max_drawdown_days`).

All other simulation details — leverage clamp, risk_frac clamp, stop/take/
liquidation logic, taker COMMISSION per side, START_EQUITY, entry fee
deduction, equity-wipeout break, OOS metric accounting — are identical to
the harness.
"""
from __future__ import annotations

from typing import Sequence

START_EQUITY = 100.0
COMMISSION = 0.0005          # 0.05% taker per side

# Single source of truth: the metric keys `simulate()` actually emits as SCORED outputs.
# The onboarding path (`score-bot` → simulate) can only score these — any config naming a
# metric outside this set would silently fail every iteration (the numeric adapter reports it
# as missing → verdict 'fail' until plateau). build_onboarding_config validates against this
# set so the mistake is caught at project-creation time, not after a run burns tokens.
SCORED_METRICS = (
    "return_oos_pct",
    "liquidations",
    "max_drawdown_pct",
    "max_drawdown_bars",
    "full_max_drawdown_pct",
)


def simulate(
    bars: Sequence[tuple[float, float, float, float, float]],
    orders: Sequence[int],
    *,
    oos_start: int,
    params: dict,
) -> dict:
    """Run a leveraged long/short simulation and return scored + report metrics.

    Parameters
    ----------
    bars:
        Sequence of (open, high, low, close, volume) 5-tuples.
    orders:
        Desired position at each bar: -1 (short), 0 (flat), 1 (long).
        Must be the same length as ``bars``.
    oos_start:
        Bar index where out-of-sample accounting begins (inclusive).
        Typically ``int(len(bars) * (1 - oos_frac))``.
    params:
        Dict of risk knobs.  Defaults match the harness defaults.
        - ``leverage``  (float, 1..50, default 3.0)
        - ``risk_frac`` (float, 0..1,  default 0.5)
        - ``stop_pct``  (float ≥ 0.05, percentage, default 2.0)
        - ``take_pct``  (float ≥ 0.0,  percentage, default 0.0 → disabled)

    Returns
    -------
    dict with keys:
        Scored (OOS tail):
            return_oos_pct, liquidations, max_drawdown_pct, max_drawdown_bars
        Scored (whole-period viability gate):
            full_max_drawdown_pct   # near-100% if the account was destroyed in-sample
        Report-only (full run / in-sample):
            num_trades, full_return_pct, in_sample_return_pct, win_rate_pct, profit_factor
    """
    if len(orders) != len(bars):
        raise ValueError(
            f"len(orders)={len(orders)} != len(bars)={len(bars)}"
        )

    # --- clamp risk knobs exactly as the harness does ---
    lev      = min(50.0, max(1.0,  float(params.get("leverage",  3.0))))
    risk     = min(1.0,  max(0.0,  float(params.get("risk_frac", 0.5))))
    stop_pct = max(0.05, float(params.get("stop_pct", 2.0))) / 100.0
    take_pct = max(0.0,  float(params.get("take_pct", 0.0))) / 100.0

    equity       = START_EQUITY
    pos          = None               # active position dict or None
    liquidations = 0                  # full-run
    trades: list[float] = []          # full-run realised PnL

    # full-run drawdown trackers (report only)
    peak     = equity
    max_dd   = 0.0
    peak_bar = 0                      # bar index of last equity high-water

    # OOS trackers — initialised when we cross oos_start
    oos: dict = {"eq0": None, "peak": None, "dd": 0.0,
                 "uw": 0, "peak_bar": 0, "liq0": 0, "tr0": 0}

    # ------------------------------------------------------------------
    def close_pos(exit_price: float) -> None:
        nonlocal equity, pos
        move  = (exit_price - pos["entry"]) / pos["entry"] * pos["side"]
        gross = pos["notional"] * move - pos["notional"] * COMMISSION
        equity += gross
        trades.append(gross - pos["entry_fee"])
        pos = None
    # ------------------------------------------------------------------

    for i, (o, hi, lo, c, _v) in enumerate(bars):

        # ---- enter OOS window ----
        if i == oos_start:
            oos.update(
                eq0=equity, peak=equity, dd=0.0, uw=0,
                peak_bar=i, liq0=liquidations, tr0=len(trades),
            )

        # ---- stop / take / liquidation checks ----
        if pos is not None:
            if pos["side"] == 1:
                if lo <= pos["liq"]:
                    equity -= pos["margin"]
                    liquidations += 1
                    trades.append(-pos["margin"] - pos["entry_fee"])
                    pos = None
                elif lo <= pos["stop"]:
                    close_pos(pos["stop"])
                elif pos["take"] and hi >= pos["take"]:
                    close_pos(pos["take"])
            else:  # short
                if hi >= pos["liq"]:
                    equity -= pos["margin"]
                    liquidations += 1
                    trades.append(-pos["margin"] - pos["entry_fee"])
                    pos = None
                elif hi >= pos["stop"]:
                    close_pos(pos["stop"])
                elif pos["take"] and lo <= pos["take"]:
                    close_pos(pos["take"])

        # ---- equity wipeout ----
        if equity <= 0.01:
            if peak > 0:
                max_dd = 100.0          # account destroyed → full-period drawdown is total
            equity = 0.0
            break

        # ---- position management (order-driven) ----
        want = int(orders[i])
        if pos is not None and want != 0 and want != pos["side"]:
            close_pos(c)
        if pos is None and want != 0 and equity > 0.01:
            margin    = risk * equity
            notional  = margin * lev
            entry_fee = notional * COMMISSION
            equity   -= entry_fee
            stop = c * (1 - stop_pct) if want == 1 else c * (1 + stop_pct)
            liq  = c * (1 - 1.0 / lev) if want == 1 else c * (1 + 1.0 / lev)
            take = (
                (c * (1 + take_pct) if want == 1 else c * (1 - take_pct))
                if take_pct else 0.0
            )
            pos = {
                "side": want, "entry": c, "notional": notional,
                "margin": margin, "stop": stop, "take": take,
                "liq": liq, "entry_fee": entry_fee,
            }

        # ---- full-run drawdown (report) ----
        if equity > peak:
            peak     = equity
            peak_bar = i
        else:
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)

        # ---- OOS drawdown (scored) ----
        if oos["eq0"] is not None:
            if equity > oos["peak"]:
                oos["peak"]     = equity
                oos["peak_bar"] = i
            else:
                oos["uw"] = max(oos["uw"], i - oos["peak_bar"])
                if oos["peak"] > 0:
                    oos["dd"] = max(oos["dd"], (oos["peak"] - equity) / oos["peak"] * 100.0)

    # ---- close any open position at final bar's close ----
    # bars are (o, h, l, c, v) — close is index 3, not 4 (harness had ts at 0, so close was at 4)
    if pos is not None and bars:
        close_pos(bars[-1][3])
        if equity > peak:
            peak = equity

    # ---- out-of-sample scored metrics ----
    oos_eq0    = oos["eq0"] if oos["eq0"] and oos["eq0"] > 0 else START_EQUITY
    return_oos = (equity / oos_eq0 - 1.0) * 100.0
    liq_oos    = liquidations - oos["liq0"]
    oos_trades = trades[oos["tr0"]:]

    # ---- report-only stats ----
    num_trades   = len(oos_trades)
    wins         = [x for x in oos_trades if x > 0]
    gross_profit = sum(x for x in oos_trades if x > 0)
    gross_loss   = -sum(x for x in oos_trades if x < 0)
    win_rate     = (len(wins) / num_trades * 100.0) if num_trades else 0.0
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None
    in_sample_return = (oos_eq0 / START_EQUITY - 1.0) * 100.0

    return {
        # scored — OOS tail
        "return_oos_pct":      round(return_oos, 4),
        "liquidations":        liq_oos,
        "max_drawdown_pct":    round(oos["dd"], 4),
        "max_drawdown_bars":   oos["uw"],          # bars underwater (replaces max_drawdown_days)
        # scored — WHOLE-period viability gate (the OOS return is scale-invariant, so draining the
        # in-sample account to ~0 to game a huge OOS % shows up as a near-100% drawdown here)
        "full_max_drawdown_pct": round(max_dd, 4),
        # report-only
        "num_trades":          num_trades,
        "full_return_pct":     round((equity / START_EQUITY - 1.0) * 100.0, 4),
        "in_sample_return_pct": round(in_sample_return, 4),
        "win_rate_pct":        round(win_rate, 2),
        "profit_factor":       profit_factor,
    }
