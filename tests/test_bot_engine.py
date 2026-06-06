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


# ---------------------------------------------------------------------------
# Hand-computed PnL — HARDCODED regression anchor.
#
# The expected return is a HARDCODED literal (19.781), NOT recomputed with the
# engine's own arithmetic. A shared formula error therefore cannot hide. The
# literal was confirmed by independent hand arithmetic below.
#
# Setup:
#   - 5 bars, close price = [100, 100, 100, 110, 100]
#   - orders =              [0,   0,   1,   -1,  0  ]
#   - oos_start = 2  (OOS covers bars 2,3,4)
#   - params: leverage=2.0, risk_frac=0.5, stop_pct=50.0, take_pct=0.0
#     (stop_pct=50% -> stop at 50, liquidation at 50 -> never triggers on a 10% move)
#
# Execution trace (COMMISSION=0.0005, START_EQUITY=100):
#   bar 2 (i==oos_start): oos.eq0 = 100.0 (captured before the trade on this bar)
#                         OPEN LONG at close=100.0
#                           margin   = 0.5 * 100 = 50.0
#                           notional = 50 * 2    = 100.0
#                           entry_fee= 100 * 0.0005 = 0.05  -> equity = 99.95
#   bar 3: close long at 110 (opposite order want=-1)
#                           move  = (110-100)/100 = 0.10
#                           gross = 100*0.10 - 100*0.0005 = 9.95 -> equity = 109.90
#                         OPEN SHORT at close=110.0
#                           margin   = 0.5 * 109.90 = 54.95
#                           notional = 54.95 * 2    = 109.90
#                           entry_fee= 109.90*0.0005 = 0.05495 -> equity = 109.84505
#   bar 4: want=0 -> no close (want==0 never closes); position stays open
#   END: close open short at bars[-1][3] (close = 100.0)
#                           move  = (100-110)/110 * (-1) = 10/110
#                           gross = 109.90*(10/110) - 109.90*0.0005 = 9.935959...
#                           equity_final = 109.84505 + 9.935959... = 119.781009...
#   return_oos_pct = (119.781009.../100 - 1)*100 = 19.781009... -> round(.,4) = 19.781
def test_hand_computed_trade_pnl():
    bars = [
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (110.0, 110.0, 110.0, 110.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
    ]
    orders = [0, 0, 1, -1, 0]
    params = {"leverage": 2.0, "risk_frac": 0.5, "stop_pct": 50.0, "take_pct": 0.0}

    m = simulate(bars, orders, oos_start=2, params=params)

    # HARDCODED anchor — independently verified by the hand trace above.
    assert abs(m["return_oos_pct"] - 19.781) < 1e-3, (
        f"return_oos_pct={m['return_oos_pct']!r}, expected 19.781"
    )
    # OOS trades: long (closed in OOS) + short (closed at end)
    assert m["num_trades"] == 2
    assert m["liquidations"] == 0


# ---------------------------------------------------------------------------
# LIQUIDATION — long position whose liq level is pierced.
#
#   params: leverage=5.0 -> liq = entry*(1 - 1/5) = entry*0.80 = 80.0
#           risk_frac=0.5, stop_pct=50% (stop=50, well below liq so liq fires first)
#   bars (close): [100,100,100,79,79], orders=[0,0,1,0,0], oos_start=2
#
# Trace:
#   bar 2: OPEN LONG at 100
#            margin   = 0.5*100 = 50.0
#            notional = 50*5     = 250.0
#            entry_fee= 250*0.0005 = 0.125 -> equity = 99.875
#            liq = 100*(1 - 1/5) = 80.0
#   bar 3: low = 79.0 <= liq(80.0) -> LIQUIDATE
#            equity -= margin  =>  99.875 - 50.0 = 49.875
#   return_oos_pct = (49.875/100 - 1)*100 = -50.125
#
# Equity drops by EXACTLY the margin (50.0): 99.875 -> 49.875.
def test_liquidation_hits_and_wipes_margin():
    bars = [
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 79.0, 79.0, 1.0),   # low pierces liq=80
        (79.0, 79.0, 79.0, 79.0, 1.0),
    ]
    orders = [0, 0, 1, 0, 0]
    params = {"leverage": 5.0, "risk_frac": 0.5, "stop_pct": 50.0, "take_pct": 0.0}

    m = simulate(bars, orders, oos_start=2, params=params)

    assert m["liquidations"] == 1
    assert m["num_trades"] == 1
    # HARDCODED anchor: equity 99.875 -> 49.875 (lost exactly the 50.0 margin).
    assert abs(m["return_oos_pct"] - (-50.125)) < 1e-3, (
        f"return_oos_pct={m['return_oos_pct']!r}, expected -50.125"
    )


# ---------------------------------------------------------------------------
# STOP-LOSS — long closes at the STOP PRICE, not the bar close.
#
#   params: leverage=2.0 -> liq = 100*(1-1/2) = 50.0
#           stop_pct=5% -> stop = 100*(1-0.05) = 95.0
#   bars (close): [100,100,100,96,96], with bar 3 LOW=94 (pierces stop=95, NOT liq=50)
#   orders=[0,0,1,0,0], oos_start=2
#
# Trace:
#   bar 2: OPEN LONG at 100
#            margin=50, notional=100, entry_fee=0.05 -> equity=99.95
#            stop=95.0, liq=50.0
#   bar 3: low=94 <= stop(95) and > liq(50) -> CLOSE AT STOP PRICE 95.0 (NOT close 96)
#            move = (95-100)/100 = -0.05
#            gross= 100*(-0.05) - 100*0.0005 = -5.0 - 0.05 = -5.05
#            equity = 99.95 - 5.05 = 94.90
#   return_oos_pct = (94.90/100 - 1)*100 = -5.10
#
# Sanity: closing at the bar close (96) instead would give -4.10, so -5.10
# proves the exit used the STOP price.
def test_stop_loss_closes_at_stop_price():
    bars = [
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 94.0, 96.0, 1.0),   # low=94 pierces stop=95, not liq=50
        (96.0, 96.0, 96.0, 96.0, 1.0),
    ]
    orders = [0, 0, 1, 0, 0]
    params = {"leverage": 2.0, "risk_frac": 0.5, "stop_pct": 5.0, "take_pct": 0.0}

    m = simulate(bars, orders, oos_start=2, params=params)

    assert m["num_trades"] == 1
    assert m["liquidations"] == 0
    # HARDCODED anchor: closed at stop=95 -> -5.10 (NOT -4.10 from bar close 96).
    assert abs(m["return_oos_pct"] - (-5.1)) < 1e-3, (
        f"return_oos_pct={m['return_oos_pct']!r}, expected -5.1"
    )


# ---------------------------------------------------------------------------
# SHORT trade — profits when price FALLS (sign correctness).
#
#   params: leverage=2.0, risk_frac=0.5, stop_pct=50% (no trigger), take_pct=0
#   bars (close): [100,100,100,90,90], orders=[0,0,-1,1,0], oos_start=2
#
# Trace:
#   bar 2: OPEN SHORT at 100
#            margin=50, notional=100, entry_fee=0.05 -> equity=99.95
#   bar 3: opposite order want=1 -> CLOSE SHORT at close=90
#            move = (90-100)/100 * (-1) = +0.10   (short gains as price falls)
#            gross= 100*0.10 - 100*0.0005 = 9.95 -> equity = 109.90
#          then OPEN LONG at 90
#            margin=54.95, notional=109.90, entry_fee=0.05495 -> equity=109.84505
#   bar 4: want=0 -> no close
#   END: close long at bars[-1][3]=90 (no move): gross = -109.90*0.0005 = -0.05495
#            equity_final = 109.84505 - 0.05495 = 109.79010
#   return_oos_pct = (109.79010/100 - 1)*100 = 9.7901
def test_short_trade_profits_on_falling_price():
    bars = [
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (100.0, 100.0, 100.0, 100.0, 1.0),
        (90.0, 90.0, 90.0, 90.0, 1.0),     # short closed here at 90 (profit)
        (90.0, 90.0, 90.0, 90.0, 1.0),
    ]
    orders = [0, 0, -1, 1, 0]
    params = {"leverage": 2.0, "risk_frac": 0.5, "stop_pct": 50.0, "take_pct": 0.0}

    m = simulate(bars, orders, oos_start=2, params=params)

    assert m["liquidations"] == 0
    assert m["num_trades"] == 2   # short (closed at bar3) + long (closed at end)
    # HARDCODED anchor: short profited from the fall -> positive return.
    assert abs(m["return_oos_pct"] - 9.7901) < 1e-3, (
        f"return_oos_pct={m['return_oos_pct']!r}, expected 9.7901"
    )
    assert m["return_oos_pct"] > 0.0   # short side sign correct
