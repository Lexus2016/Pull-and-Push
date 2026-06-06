import json
import pytest
from pydantic import ValidationError
from tyani_tolkai.proposal_schema import MetricProposal
from tyani_tolkai.profile_schema import BotProfile, ExtractableFact, Tunable

_PROFILE = BotProfile(
    analyzer_engine="claude", bot_name="mybot", source_root="/tmp/mybot",
    language="python", framework="custom",
    tunable_surface=[Tunable(name="leverage", location="s.py:1", inferred_type="float",
                             semantic_role="leverage", confidence=0.8, evidence=["s.py:1"])],
    extractable_metrics=[ExtractableFact(name="return_oos_pct", how="engine measures",
                                         trustworthy=True, confidence=0.9, evidence=["h.py:1"])],
)

_VALID_PROPOSAL = {
    "proposed_metrics": [{"name": "return_oos_pct", "dir": "higher", "weight": 0.6,
                          "target": 100.0, "rationale": "primary", "confidence": 0.8}],
    "proposed_tunables": [{"name": "leverage", "min": 1.0, "max": 10.0,
                           "inferred_type": "float", "rationale": "risk", "confidence": 0.7}],
    "warnings": [],
}


def test_build_proposer_prompt_has_grounding_and_goal():
    from tyani_tolkai.proposer import build_proposer_prompt
    prompt = build_proposer_prompt(_PROFILE, "max return, avoid liquidations")
    assert "max return, avoid liquidations" in prompt
    assert "return_oos_pct" in prompt
    assert "leverage" in prompt
    assert "inert data" in prompt.lower()
    assert "json" in prompt.lower()


def test_parse_proposal_stamps_provenance():
    from tyani_tolkai.proposer import parse_proposal
    text = "```json\n" + json.dumps(_VALID_PROPOSAL) + "\n```"
    p = parse_proposal(text, engine="codex", profile=_PROFILE, goal="g")
    assert p.proposer_engine == "codex"
    assert p.bot_name == "mybot"
    assert p.goal == "g"
    assert p.proposed_metrics[0].name == "return_oos_pct"


def test_parse_proposal_no_json_raises():
    from tyani_tolkai.proposer import parse_proposal
    with pytest.raises(ValueError):
        parse_proposal("no json", engine="claude", profile=_PROFILE, goal="g")


def test_parse_proposal_bad_shape_raises():
    from tyani_tolkai.proposer import parse_proposal
    bad = json.dumps({"proposed_metrics": [{"name": "x", "dir": "higher"}]})
    with pytest.raises(ValidationError):
        parse_proposal(bad, engine="claude", profile=_PROFILE, goal="g")
