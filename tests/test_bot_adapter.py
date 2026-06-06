import io
from tyani_tolkai.bot_adapter import decide, run_protocol_io
from tyani_tolkai import bot_protocol as bp


def test_decide_momentum_rule():
    assert decide([(1, 1, 1, 1.0, 1)]) == 0                       # first bar: no previous
    assert decide([(1, 1, 1, 1.0, 1), (1, 1, 1, 2.0, 1)]) == 1    # close rose
    assert decide([(1, 1, 1, 2.0, 1), (1, 1, 1, 1.0, 1)]) == 0    # close fell


def test_run_protocol_io_streams_orders():
    inp = "".join([
        bp.encode({"type": bp.INIT, "schema_version": "1", "params": {}}),
        bp.encode({"type": bp.BAR, "n": 0, "o": 1, "h": 1, "l": 1, "c": 1.0, "v": 1}),
        bp.encode({"type": bp.BAR, "n": 1, "o": 1, "h": 1, "l": 1, "c": 2.0, "v": 1}),
        bp.encode({"type": bp.END}),
    ])
    out = io.StringIO()
    run_protocol_io(io.StringIO(inp), out)
    lines = [bp.decode(l) for l in out.getvalue().splitlines()]
    assert lines[0] == {"type": "ready"}
    assert lines[1] == {"type": "order", "n": 0, "want": 0}
    assert lines[2] == {"type": "order", "n": 1, "want": 1}
