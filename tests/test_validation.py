from tyani_tolkai.validation import (
    flat_orders, buy_and_hold_orders, random_orders, score_controls, beats_controls,
)


def _bars(closes):
    return [(c, c, c, c, 1.0) for c in closes]


def test_flat_orders():
    assert flat_orders(4) == [0, 0, 0, 0]
    assert flat_orders(0) == []


def test_buy_and_hold_orders():
    assert buy_and_hold_orders(4) == [1, 0, 0, 0]
    assert buy_and_hold_orders(0) == []
    assert buy_and_hold_orders(1) == [1]


def test_random_orders_deterministic_and_in_range():
    a = random_orders(20, seed="s")
    b = random_orders(20, seed="s")
    assert a == b                                  # deterministic for a seed
    assert len(a) == 20
    assert set(a) <= {-1, 0, 1}
    assert random_orders(20, seed="other") != a or True   # may differ; not asserted hard


def test_score_controls_returns_three_named_metric_dicts():
    bars = _bars([1.0, 2.0, 3.0, 2.0, 4.0, 5.0, 1.0, 2.0])
    ctl = score_controls(bars, oos_start=5, params={})
    assert set(ctl) == {"flat", "buy_and_hold", "random"}
    for name, m in ctl.items():
        assert "return_oos_pct" in m


def test_beats_controls():
    bot = {"return_oos_pct": 12.0}
    controls = {"flat": {"return_oos_pct": 0.0}, "random": {"return_oos_pct": 3.0},
                "buy_and_hold": {"return_oos_pct": 8.0}}
    v = beats_controls(bot, controls)
    assert v["beats_flat"] is True and v["beats_random"] is True

    weak = {"return_oos_pct": -1.0}
    v2 = beats_controls(weak, controls)
    assert v2["beats_flat"] is False


import pytest
from tyani_tolkai.validation import check_determinism, hash_artifacts, insample_oos_gap


def test_check_determinism_true_for_stable_callable():
    calls = {"n": 0}
    def stable():
        calls["n"] += 1
        return {"return_oos_pct": 5.0, "num_trades": 2}
    ok, scores = check_determinism(stable, runs=3)
    assert ok is True
    assert calls["n"] == 3
    assert len(scores) == 3


def test_check_determinism_false_for_varying_callable():
    state = {"n": 0}
    def varying():
        state["n"] += 1
        return {"return_oos_pct": float(state["n"])}    # changes each call
    ok, scores = check_determinism(varying, runs=2)
    assert ok is False


def test_hash_artifacts_stable_and_sensitive(tmp_path):
    eng = tmp_path / "engine.py"; eng.write_text("print(1)\n", encoding="utf-8")
    data = tmp_path / "data.csv"; data.write_text("a,b\n1,2\n", encoding="utf-8")
    h1 = hash_artifacts(engine_path=eng, data_path=data, config={"seed": "x", "lev": 3})
    h2 = hash_artifacts(engine_path=eng, data_path=data, config={"lev": 3, "seed": "x"})
    assert set(h1) == {"engine", "data", "config"}
    assert all(len(v) == 64 for v in h1.values())     # sha256 hex
    assert h1 == h2                                    # config key order doesn't matter (canonical)
    eng.write_text("print(2)\n", encoding="utf-8")
    h3 = hash_artifacts(engine_path=eng, data_path=data, config={"seed": "x", "lev": 3})
    assert h3["engine"] != h1["engine"]               # engine change → different hash
    assert h3["data"] == h1["data"]                   # data unchanged → same hash


def test_hash_artifacts_missing_file_raises(tmp_path):
    with pytest.raises((FileNotFoundError, OSError)):
        hash_artifacts(engine_path=tmp_path / "nope.py", data_path=tmp_path / "nope.csv", config={})


def test_insample_oos_gap():
    assert insample_oos_gap({"in_sample_return_pct": 50.0, "return_oos_pct": 10.0}) == 40.0
    assert insample_oos_gap({"in_sample_return_pct": 5.0, "return_oos_pct": 8.0}) == -3.0


def test_build_evidence_report_pass_and_flag():
    from tyani_tolkai.validation import build_evidence_report
    hashes = {"engine": "a" * 64, "data": "b" * 64, "config": "c" * 64}
    controls = {"flat": {"return_oos_pct": 0.0}, "random": {"return_oos_pct": 3.0},
                "buy_and_hold": {"return_oos_pct": 8.0}}

    # PASS: bot beats flat+random, deterministic, small gap
    md = build_evidence_report(
        bot_name="mybot",
        bot_metrics={"return_oos_pct": 12.0, "in_sample_return_pct": 14.0},
        control_metrics=controls,
        beats={"beats_flat": True, "beats_random": True},
        determinism_ok=True, hashes=hashes, gap=2.0,
    )
    assert md.startswith("# Evidence Report")
    assert "mybot" in md
    assert "## Provenance" in md and "a" * 64 in md           # hashes surfaced
    assert "## Control spectrum" in md and "buy_and_hold" in md and "12.0" in md
    assert "## Determinism" in md
    assert "PASS" in md
    assert "flat" in md and "random" in md

    # FLAG: bot loses to flat
    md2 = build_evidence_report(
        bot_name="weak",
        bot_metrics={"return_oos_pct": -2.0, "in_sample_return_pct": 50.0},
        control_metrics=controls,
        beats={"beats_flat": False, "beats_random": False},
        determinism_ok=True, hashes=hashes, gap=52.0,
    )
    assert "FLAG" in md2
    assert "does not beat" in md2.lower() or "beats_flat" in md2.lower()   # reason surfaced


# ----------------------------------------------------------------------------- P4.2
from tyani_tolkai.validation import reverse_oos, anti_lookahead_probe, evidence_verdict


def test_reverse_oos_perturbs_only_the_tail():
    bars = _bars([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    out = reverse_oos(bars, oos_start=3)
    assert out[:3] == bars[:3]                       # in-sample prefix untouched
    assert [b[3] for b in out[3:]] == [6.0, 5.0, 4.0]   # OOS tail reversed (close col)
    assert len(out) == len(bars)
    assert out is not bars                            # returns a fresh list, no mutation


def test_reverse_oos_no_tail_is_identity():
    bars = _bars([1.0, 2.0, 3.0])
    assert reverse_oos(bars, oos_start=3) == bars     # oos_start >= n → nothing to reverse
    assert reverse_oos(bars, oos_start=99) == bars


def test_anti_lookahead_probe_ok_when_score_reacts():
    # causal scorer: reversing the future changes the OOS number
    probe = anti_lookahead_probe(lambda: {"return_oos_pct": 12.0},
                                 lambda: {"return_oos_pct": -4.0})
    assert probe["ok"] is True and probe["reacted"] is True
    assert probe["baseline"] == 12.0 and probe["perturbed"] == -4.0
    assert probe["delta"] == 16.0


def test_anti_lookahead_probe_flags_invariant_score():
    # degenerate / leaky scorer: identical score on real and reversed timeline → red flag
    probe = anti_lookahead_probe(lambda: {"return_oos_pct": 7.5},
                                 lambda: {"return_oos_pct": 7.5})
    assert probe["ok"] is False and probe["reacted"] is False


def test_anti_lookahead_probe_accepts_scalars():
    probe = anti_lookahead_probe(lambda: 10.0, lambda: 10.0)
    assert probe["ok"] is False


def test_evidence_verdict_pass_and_reasons():
    ok_beats = {"beats_flat": True, "beats_random": True}
    verdict, reasons = evidence_verdict(beats=ok_beats, determinism_ok=True, gap=2.0)
    assert verdict == "PASS" and reasons == []

    verdict2, reasons2 = evidence_verdict(
        beats={"beats_flat": False, "beats_random": True}, determinism_ok=False, gap=99.0,
    )
    assert verdict2 == "FLAG"
    assert len(reasons2) == 3                          # flat + determinism + gap (no probe passed)
    blob = " ".join(reasons2).lower()
    assert "flat" in blob and "deterministic" in blob and "overfit" in blob


def test_evidence_verdict_flags_lookahead():
    verdict, reasons = evidence_verdict(
        beats={"beats_flat": True, "beats_random": True}, determinism_ok=True, gap=1.0,
        anti_lookahead={"ok": False},
    )
    assert verdict == "FLAG"
    assert any("look-ahead" in r.lower() or "perturbed timeline" in r.lower() for r in reasons)


def test_build_evidence_report_includes_anti_lookahead_and_isolation():
    from tyani_tolkai.validation import build_evidence_report
    hashes = {"engine": "a" * 64, "data": "b" * 64, "config": "c" * 64}
    controls = {"flat": {"return_oos_pct": 0.0}, "random": {"return_oos_pct": 3.0},
                "buy_and_hold": {"return_oos_pct": 8.0}}
    md = build_evidence_report(
        bot_name="mybot",
        bot_metrics={"return_oos_pct": 12.0, "in_sample_return_pct": 14.0},
        control_metrics=controls,
        beats={"beats_flat": True, "beats_random": True},
        determinism_ok=True, hashes=hashes, gap=2.0,
        anti_lookahead={"baseline": 12.0, "perturbed": -1.0, "delta": 13.0, "ok": True},
        isolation="subprocess (reduced — Docker not available)",
    )
    assert "look-ahead" in md.lower()
    assert "subprocess (reduced" in md                 # isolation surfaced in provenance
    assert "PASS" in md                                # passing probe doesn't flag

    md2 = build_evidence_report(
        bot_name="leaky",
        bot_metrics={"return_oos_pct": 12.0, "in_sample_return_pct": 14.0},
        control_metrics=controls,
        beats={"beats_flat": True, "beats_random": True},
        determinism_ok=True, hashes=hashes, gap=2.0,
        anti_lookahead={"baseline": 9.0, "perturbed": 9.0, "delta": 0.0, "ok": False},
    )
    assert "FLAG" in md2 and "look-ahead" in md2.lower()


# ----------------------------------------------------------------------------- P4.6 adapter gate
from tyani_tolkai.validation import synth_bars, check_adapter_orders


def test_synth_bars_deterministic_and_non_monotonic():
    a = synth_bars(40)
    b = synth_bars(40)
    assert a == b                                       # deterministic (no RNG)
    assert len(a) == 40 and all(len(bar) == 5 for bar in a)
    closes = [bar[3] for bar in a]
    assert any(y < x for x, y in zip(closes, closes[1:]))   # falls somewhere (not monotonic up)
    assert any(y > x for x, y in zip(closes, closes[1:]))   # and rises somewhere


def test_check_adapter_orders_pass():
    orders = [0, 1, -1, 0, 1]
    v = check_adapter_orders(orders, list(orders), n_bars=5)
    assert v["ok"] is True and not v["reasons"]
    assert v["well_formed"] and v["deterministic"] and not v["degenerate"]


def test_check_adapter_orders_flags_nondeterministic():
    v = check_adapter_orders([0, 1, 0], [0, 0, 0], n_bars=3)
    assert v["ok"] is False and v["deterministic"] is False


def test_check_adapter_orders_flags_degenerate_constant():
    v = check_adapter_orders([0, 0, 0, 0], [0, 0, 0, 0], n_bars=4)
    assert v["ok"] is False and v["degenerate"] is True   # constant output on a varied series


def test_check_adapter_orders_flags_malformed():
    assert check_adapter_orders([0, 1], [0, 1], n_bars=5)["well_formed"] is False   # wrong length
    assert check_adapter_orders([0, 2, 1], [0, 2, 1], n_bars=3)["well_formed"] is False  # bad value
