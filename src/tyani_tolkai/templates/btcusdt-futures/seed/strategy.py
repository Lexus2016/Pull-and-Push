"""BTCUSDT 5m futures strategy — EDIT ONLY THIS FILE.

The scoring harness (metrics/run_backtest.py) imports `PARAMS` from this file and runs
a leveraged long/short trend strategy on real BTCUSDT 5m data, then prints the metrics.
You improve the numbers below (one focused change per iteration) to raise the score:

  - total_return_pct   higher is better   (target 300)
  - liquidations       lower is better    (target 0)
  - max_drawdown_pct   lower is better    (target 30)
  - max_drawdown_days  lower is better    (target 5)

Keep the keys; only change the values. Mind the trade-offs: more leverage and bigger
risk_frac raise returns but also drawdown and liquidation risk.
"""

PARAMS = {
    "fast": 20,           # fast moving-average window (bars)
    "slow": 60,           # slow moving-average window (bars); must differ from fast
    "trend_filter": 200,  # only go long above this MA / short below it (0 = disabled)
    "leverage": 3.0,      # position leverage (1..50)
    "risk_frac": 0.5,     # fraction of equity committed as margin per trade (0..1)
    "stop_pct": 2.0,      # stop-loss distance from entry, in percent of price (>0, mandatory)
    "take_pct": 4.0,      # take-profit distance, in percent (0 = let the trend run)
    "allow_short": True,  # also take short positions
}
