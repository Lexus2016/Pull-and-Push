import json
from pathlib import Path

from tyani_tolkai.agents.scripted_rival import ScriptedAdversaryRival, ScriptedRecognizerRival


def test_recognizer_rival_fits_counterexamples(tmp_path):
    arena = tmp_path / ".arena"
    arena.mkdir()
    (arena / "counterexamples.json").write_text(json.dumps(
        [{"s": "ab", "label": True}, {"s": "ba", "label": False},
         {"s": "b", "label": False}, {"s": "aab", "label": True}]))
    res = ScriptedRecognizerRival().run("brief", tmp_path, "writeable", 60)
    assert res.changed
    ns: dict = {}
    exec((tmp_path / "recognizer.py").read_text(), ns)
    assert ns["accepts"]("ab") and ns["accepts"]("aab")
    assert not ns["accepts"]("ba") and not ns["accepts"]("b")


def test_adversary_rival_emits_probes(tmp_path):
    arena = tmp_path / ".arena"
    arena.mkdir()
    (arena / "opponent_recognizer.py").write_text("def accepts(s):\n    return True\n")
    res = ScriptedAdversaryRival().run("brief", tmp_path, "writeable", 60)
    assert res.changed
    probes = [ln for ln in (tmp_path / "strings.txt").read_text().splitlines() if ln]
    assert len(probes) >= 4 and all(set(p) <= set("ab") for p in probes)


def test_recognizer_rival_no_examples_is_accept_all(tmp_path):
    (tmp_path / ".arena").mkdir()
    (tmp_path / ".arena" / "counterexamples.json").write_text("[]")
    ScriptedRecognizerRival().run("brief", tmp_path, "writeable", 60)
    ns: dict = {}
    exec((tmp_path / "recognizer.py").read_text(), ns)
    assert ns["accepts"]("anything") is True   # accept-all seed ('' in s)
