import sys
import pytest
from tyani_tolkai.bot_runner import drive_bot, BotProtocolError

_ADAPTER = [sys.executable, "-m", "tyani_tolkai.bot_adapter"]


def _bars(closes):
    return [(c, c, c, c, 1.0) for c in closes]


def test_drive_bot_collects_orders_from_reference():
    bars = _bars([1.0, 2.0, 1.5, 3.0])
    orders = drive_bot(_ADAPTER, bars, params={}, per_read_timeout=10.0, total_timeout=30.0)
    assert orders == [0, 1, 0, 1]            # momentum-1 on the closes


def test_drive_bot_times_out_on_silent_bot():
    silent = [sys.executable, "-c", "import time; time.sleep(60)"]
    bars = _bars([1.0, 2.0])
    with pytest.raises(BotProtocolError):
        drive_bot(silent, bars, params={}, per_read_timeout=1.0, total_timeout=3.0)


def test_drive_bot_rejects_garbage_bot():
    garbage = [sys.executable, "-c",
               "import sys\nwhile True:\n sys.stdout.write('garbage\\n'); sys.stdout.flush()"]
    bars = _bars([1.0])
    with pytest.raises(BotProtocolError):
        drive_bot(garbage, bars, params={}, per_read_timeout=2.0, total_timeout=5.0)


def test_drive_bot_rejects_wrong_n():
    # a bot that replies with the wrong bar index → protocol violation
    bad = [sys.executable, "-c",
           "import sys,json\n"
           "for line in sys.stdin:\n"
           " m=json.loads(line)\n"
           " t=m.get('type')\n"
           " if t=='init': sys.stdout.write('{\"type\":\"ready\"}\\n')\n"
           " elif t=='bar': sys.stdout.write('{\"type\":\"order\",\"n\":999,\"want\":0}\\n')\n"
           " elif t=='end': break\n"
           " sys.stdout.flush()"]
    bars = _bars([1.0])
    with pytest.raises(BotProtocolError):
        drive_bot(bad, bars, params={}, per_read_timeout=3.0, total_timeout=8.0)
