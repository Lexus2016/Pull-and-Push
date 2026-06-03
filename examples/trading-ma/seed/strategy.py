# Tunable trading parameters — the Executor edits ONLY this file.
#
# A moving-average crossover on BTCUSDT 5m. The defaults are deliberately weak;
# improve `sharpe` and `total_return_pct` while keeping `max_drawdown` low by tuning
# the windows and (optionally) adding simple filters the backtest already understands.
PARAMS = {
    "fast": 3,            # fast MA window (bars)
    "slow": 8,            # slow MA window (bars)
    "long_only": True,    # True: flat instead of short when fast<slow
    "trend_filter": 0,    # optional: require price above this-bar SMA to go long (0 = off)
}
