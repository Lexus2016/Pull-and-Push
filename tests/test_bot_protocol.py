import pytest
from tyani_tolkai import bot_protocol as bp


def test_encode_decode_roundtrip():
    msg = {"type": "bar", "n": 3, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0}
    line = bp.encode(msg)
    assert line.endswith("\n")
    assert bp.decode(line) == msg


def test_decode_rejects_non_object():
    with pytest.raises(ValueError):
        bp.decode("[1,2,3]\n")
    with pytest.raises(ValueError):
        bp.decode("not json")


def test_message_type_constants():
    assert bp.INIT == "init" and bp.READY == "ready"
    assert bp.BAR == "bar" and bp.ORDER == "order" and bp.END == "end"


def test_seeded_oos_start_is_in_band_and_deterministic():
    n = 1000
    a = bp.seeded_oos_start(n, seed="proj-x")
    b = bp.seeded_oos_start(n, seed="proj-x")
    assert a == b
    assert int(0.60 * n) <= a < int(0.80 * n)
    c = bp.seeded_oos_start(n, seed="proj-y")
    assert int(0.60 * n) <= c < int(0.80 * n)


def test_seeded_oos_start_handles_small_n():
    assert bp.seeded_oos_start(0, seed="x") == 0
    assert 0 <= bp.seeded_oos_start(3, seed="x") <= 3
