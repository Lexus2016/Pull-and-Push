import pytest
from tyani_tolkai.bot_engine import simulate


def _flat_bars(n=100, price=100.0):
    return [(price, price, price, price, 1.0) for _ in range(n)]


def test_no_orders_is_flat_zero_pnl():
    m = simulate(_flat_bars(50), [0] * 50, oos_start=30, params={})
    assert m["num_trades"] == 0
    assert m["return_oos_pct"] == 0.0
    assert m["liquidations"] == 0


def test_metrics_keys_present():
    m = simulate(_flat_bars(50), [0] * 50, oos_start=30, params={})
    for k in ("return_oos_pct", "liquidations", "max_drawdown_pct", "max_drawdown_bars", "num_trades"):
        assert k in m


def test_orders_length_must_match_bars():
    with pytest.raises(ValueError):
        simulate(_flat_bars(10), [0] * 9, oos_start=5, params={})


def test_flat_market_long_no_profit_only_fees():
    bars = _flat_bars(40)
    orders = [0] * 40
    orders[35] = 1
    m = simulate(bars, orders, oos_start=30, params={"leverage": 2.0, "risk_frac": 0.5})
    assert m["return_oos_pct"] <= 0.0     # flat market: only commission lost, never profit


# Hand-computed PnL correctness test
# Setup:
#   - 5 bars total, price = [100, 100, 100, 110, 100]
#   - orders =       [0,   0,   1,   -1,  0  ]
#   - oos_start = 2  (OOS covers bars 2,3,4)
#   - params: leverage=2.0, risk_frac=0.5, stop_pct=50.0, take_pct=0.0
#     (stop_pct=50% → stop at 50, liquidation at 50 — never triggers on 10% move)
#
# Execution trace:
#   bar 0 (i=0): want=0, no pos → nothing
#   bar 1 (i=1): want=0, no pos → nothing
#   bar 2 (i=2): i==oos_start → oos.eq0 = 100.0 (START_EQUITY, no fees yet)
#                want=1, pos is None → OPEN LONG at close=100.0
#                  margin = 0.5 * 100.0 = 50.0
#                  notional = 50.0 * 2.0 = 100.0
#                  entry_fee = 100.0 * 0.0005 = 0.05
#                  equity = 100.0 - 0.05 = 99.95
#                  stop = 100 * (1 - 0.50) = 50.0
#                  liq  = 100 * (1 - 1/2)  = 50.0
#   bar 3 (i=3): pos exists, side=1, lo=110 > liq(50) and lo > stop(50) → no trigger
#                want=-1, pos.side=1, want != pos.side → close at close=110.0
#                  move = (110 - 100)/100 * 1 = 0.10
#                  gross = 100.0 * 0.10 - 100.0 * 0.0005 = 10.0 - 0.05 = 9.95
#                  equity = 99.95 + 9.95 = 109.90
#                  trade PnL recorded = gross - entry_fee = 9.95 - 0.05 = 9.90
#                want=-1 now, pos is None → OPEN SHORT at close=110.0
#                  margin = 0.5 * 109.90 = 54.95
#                  notional = 54.95 * 2.0 = 109.90
#                  entry_fee2 = 109.90 * 0.0005 = 0.054950
#                  equity = 109.90 - 0.054950 = 109.845050
#   bar 4 (i=4): pos exists, side=-1, hi=100 < liq_short(110*(1+0.5)=165) → no liq
#                hi=100 < stop_short(110*(1+0.50)=165) → no stop trigger
#                want=0, pos not None, want==0 → no close (only close when want != 0 and want != side)
#                [pos remains open, not closed mid-run by want=0]
#   End of bars: pos not None → close at bars[-1][4] = close of bar 4 = 100.0
#                move = (100 - 110)/110 * (-1) = (-10/110) * (-1) = 10/110
#                gross = 109.90 * (10/110) - 109.90 * 0.0005
#                      = 9.99090909... - 0.054950 = 9.935959...
#                equity = 109.845050 + 9.935959... = 119.781009...
#
# oos_eq0 = 100.0 (equity at i==oos_start, before open on that bar)
# return_oos_pct = (final_equity / 100.0 - 1.0) * 100.0
#
# But wait: the harness sets oos.eq0 at i==oos_start BEFORE the trade processing on that bar.
# At i=2, equity is still 100.0 before any trade → oos.eq0 = 100.0
#
# Let's compute final_equity more precisely:
# After bar 2 open: equity = 100.0 - 0.05 = 99.95
# After bar 3 close+open:
#   close long:  gross = 100*0.10 - 100*0.0005 = 9.95; equity = 99.95 + 9.95 = 109.90
#   open short:  entry_fee2 = 109.90*0.5*2*0.0005 = 0.05495; equity = 109.90 - 0.05495 = 109.84505
# After final close of short at price 100:
#   notional_short = 109.90*0.5*2 = 109.90
#   move = (100 - 110)/110 * (-1) = 10/110
#   gross2 = 109.90 * (10/110) - 109.90 * 0.0005
#           = 9.990909090... - 0.05495 = 9.935959...
#   equity_final = 109.84505 + 9.935959... = 119.781009...
#
# return_oos_pct = (119.781009... / 100.0 - 1.0) * 100.0 = 19.781009...%
#
# We check with a generous tolerance (1e-4) to allow for floating-point.

def test_hand_computed_trade_pnl():
    # prices: bar0=100, bar1=100, bar2=100, bar3=110, bar4=100
    bars = [
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (110.0, 110.0, 110.0, 110.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
    ]
    orders = [0, 0, 1, -1, 0]
    params = {"leverage": 2.0, "risk_frac": 0.5, "stop_pct": 50.0, "take_pct": 0.0}

    COMMISSION = 0.0005
    START_EQUITY = 100.0

    # --- hand calculation ---
    # bar 2: open long at 100.0
    eq0 = START_EQUITY  # oos.eq0 captured at start of bar 2 (before trade)
    margin1 = 0.5 * START_EQUITY           # 50.0
    notional1 = margin1 * 2.0              # 100.0
    entry_fee1 = notional1 * COMMISSION    # 0.05
    eq_after_open = START_EQUITY - entry_fee1  # 99.95

    # bar 3: close long at 110.0, then open short at 110.0
    move1 = (110.0 - 100.0) / 100.0       # 0.10
    gross1 = notional1 * move1 - notional1 * COMMISSION  # 9.95
    eq_after_close = eq_after_open + gross1   # 109.90

    margin2 = 0.5 * eq_after_close        # 54.95
    notional2 = margin2 * 2.0             # 109.90
    entry_fee2 = notional2 * COMMISSION   # 0.05495
    eq_after_open2 = eq_after_close - entry_fee2  # 109.84505

    # bar 4: no close; end-of-run: close short at 100.0
    move2 = (100.0 - 110.0) / 110.0 * (-1)   # 10/110
    gross2 = notional2 * move2 - notional2 * COMMISSION
    eq_final = eq_after_open2 + gross2

    expected_return_oos = (eq_final / eq0 - 1.0) * 100.0

    m = simulate(bars, orders, oos_start=2, params=params)

    assert abs(m["return_oos_pct"] - expected_return_oos) < 1e-4, (
        f"return_oos_pct={m['return_oos_pct']!r}, expected={expected_return_oos!r}"
    )
    # OOS trades: long (closed in OOS) + short (closed at end)
    assert m["num_trades"] == 2
    assert m["liquidations"] == 0
