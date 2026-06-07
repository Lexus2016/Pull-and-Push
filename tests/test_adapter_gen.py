"""P4.6: the scaffolded adapter is valid Python, speaks the P3 protocol, and an UNWIRED stub is
caught by the gate (degenerate flat) — the intended forcing function."""

import sys
import pytest

from tyani_tolkai.adapter_gen import render_adapter_stub
from tyani_tolkai.validation import synth_bars, check_adapter_orders
from tyani_tolkai.bot_runner import drive_bot
from tyani_tolkai.profile_schema import BotProfile, EntryPoint, Tunable


def test_render_produces_compilable_adapter():
    src = render_adapter_stub()
    assert "def decide(" in src and "def run_protocol_io(" in src
    assert "bot_protocol" in src
    compile(src, "adapter.py", "exec")                  # valid Python


def test_render_includes_profile_hints():
    profile = BotProfile(
        analyzer_engine="claude", bot_name="b", source_root=".", language="python",
        framework="custom",
        entry_point=EntryPoint(kind="function", location="strategy.py:signals",
                               inputs="a DataFrame of OHLCV", outputs="a +1/-1 signal", confidence=0.8),
        tunable_surface=[Tunable(name="window", location="s.py:3", inferred_type="int",
                                 semantic_role="lookback", confidence=0.7)],
    )
    src = render_adapter_stub(profile=profile)
    assert "strategy.py:signals" in src and "window" in src


def test_scaffolded_stub_speaks_protocol_but_is_degenerate(tmp_path):
    # write the rendered adapter, drive it for real over the protocol, twice
    adapter = tmp_path / "adapter.py"
    adapter.write_text(render_adapter_stub(), encoding="utf-8")
    bars = synth_bars(16)
    cmd = [sys.executable, str(adapter)]
    run1 = drive_bot(cmd, bars, params={}, per_read_timeout=30.0, total_timeout=60.0)
    run2 = drive_bot(cmd, bars, params={}, per_read_timeout=30.0, total_timeout=60.0)
    assert run1 == [0] * len(bars)                      # unwired stub = flat everywhere
    v = check_adapter_orders(run1, run2, len(bars))
    assert v["well_formed"] and v["deterministic"]      # protocol plumbing is sound...
    assert v["degenerate"] is True and v["ok"] is False  # ...but the gate flags the unwired stub
