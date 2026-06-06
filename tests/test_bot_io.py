import pytest
from tyani_tolkai.bot_io import load_bars_csv


def _write(tmp_path, text):
    p = tmp_path / "data.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_ohlcv_and_drops_time(tmp_path):
    p = _write(tmp_path,
               "time,open,high,low,close,volume\n"
               "1000,1.0,2.0,0.5,1.5,10\n"
               "2000,1.5,2.5,1.0,2.0,20\n")
    bars = load_bars_csv(p)
    assert bars == [(1.0, 2.0, 0.5, 1.5, 10.0), (1.5, 2.5, 1.0, 2.0, 20.0)]
    assert all(len(b) == 5 for b in bars)            # no timestamp


def test_missing_volume_defaults_zero(tmp_path):
    p = _write(tmp_path,
               "time,open,high,low,close\n"
               "1000,1.0,2.0,0.5,1.5\n")
    bars = load_bars_csv(p)
    assert bars == [(1.0, 2.0, 0.5, 1.5, 0.0)]


def test_missing_required_column_raises(tmp_path):
    p = _write(tmp_path,
               "time,open,high,low,volume\n"               # no 'close'
               "1000,1.0,2.0,0.5,10\n")
    with pytest.raises(ValueError):
        load_bars_csv(p)


def test_empty_volume_cell_defaults_zero(tmp_path):
    p = _write(tmp_path,
               "time,open,high,low,close,volume\n"
               "1000,1.0,2.0,0.5,1.5,\n")
    bars = load_bars_csv(p)
    assert bars[0][4] == 0.0
