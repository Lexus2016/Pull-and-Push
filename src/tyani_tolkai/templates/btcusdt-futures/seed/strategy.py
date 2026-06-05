"""BTCUSDT 5m futures strategy — EDIT THIS FILE. This IS the strategy: the signal logic in
signals() AND its parameters in PARAMS. You may rewrite signals() however you like — add
indicators (RSI, MACD, ATR, Bollinger, regime/volatility filters, multi-timeframe, even an ML
model), change the entry/exit rules, restructure — and tune PARAMS. One focused change per step.

The scoring harness (../metrics/run_backtest.py) is the independent JUDGE: it imports PARAMS and
signals(), runs a FIXED leveraged long/short simulation on real BTCUSDT 5m data (stop-loss,
liquidation, taker commission, reinvested equity) and scores the result. You do NOT edit the
harness — that would be grading your own exam.

WALK-FORWARD: the score is measured ONLY on the held-out out-of-sample tail (the last 30% of the
data). Your signals may read the whole history, but a strategy that just memorises the past scores
badly on the holdout — build something that GENERALISES. The harness also reports full_return_pct
and in_sample_return_pct: a big gap between in-sample and OOS means overfitting.

Scored (OOS): return_oos_pct↑(100) · liquidations↓(0) · max_drawdown_pct↓(30) · max_drawdown_days↓(5)
The risk knobs below (leverage, risk_frac, stop_pct, take_pct) are applied by the harness exactly
as you set them — they are your choices, not the harness's.
"""

PARAMS = {
    # --- signal logic (read by signals() below) ---
    "fast": 20,           # fast moving-average window (bars)
    "slow": 60,           # slow moving-average window (bars); must differ from fast
    "trend_filter": 200,  # only go long above this MA / short below it (0 = disabled)
    "allow_short": True,  # also take short positions
    # --- risk / execution (applied by the harness) ---
    "leverage": 3.0,      # position leverage (1..50)
    "risk_frac": 0.5,     # fraction of equity committed as margin per trade (0..1)
    "stop_pct": 2.0,      # stop-loss distance from entry, in percent of price (>0, mandatory)
    "take_pct": 4.0,      # take-profit distance, in percent (0 = let the trend run)
}


def _sma(xs, w, i):
    """Simple moving average of xs over the last w samples ending at index i (None if not enough)."""
    return sum(xs[i + 1 - w:i + 1]) / w if w > 0 and i + 1 >= w else None


def signals(bars):
    """Decide the desired position for every bar — THIS is the whole strategy.

    bars: list of (time_ms, open, high, low, close, volume) tuples, chronological.
    Returns a list `want` of the same length, where want[i] is the position decided AT bar i
    using ONLY bars[0..i] (no look-ahead): 1 = long, -1 = short, 0 = no change (hold).

    Add any indicators you need by computing them from `bars` here. The harness turns this
    sequence into trades (with your PARAMS risk knobs) and scores the outcome.
    """
    closes = [b[4] for b in bars]
    fast = max(1, int(PARAMS.get("fast", 20)))
    slow = max(fast + 1, int(PARAMS.get("slow", 60)))
    trend = max(0, int(PARAMS.get("trend_filter", 0)))
    allow_short = bool(PARAMS.get("allow_short", True))
    warmup = max(slow, trend) + 1

    want = [0] * len(bars)
    for i in range(warmup, len(bars)):
        f, s = _sma(closes, fast, i), _sma(closes, slow, i)
        if f is None or s is None:
            continue
        t = _sma(closes, trend, i) if trend else None
        c = closes[i]
        if f > s and (t is None or c > t):
            want[i] = 1
        elif f < s and allow_short and (t is None or c < t):
            want[i] = -1
    return want
