import pytest
from pydantic import ValidationError
from tyani_tolkai.proposal_schema import MetricProposal, ProposedMetric, ProposedTunable


def test_minimal_proposal_roundtrips():
    p = MetricProposal(proposer_engine="claude", bot_name="b", goal="make money")
    back = MetricProposal.model_validate_json(p.model_dump_json())
    assert back == p
    assert back.schema_version == "1"
    assert back.proposed_metrics == []
    assert back.warnings == []


def test_full_proposal_validates():
    p = MetricProposal(
        proposer_engine="codex", bot_name="b", goal="max return, avoid liquidations",
        proposed_metrics=[ProposedMetric(name="return_oos_pct", dir="higher", weight=0.6,
                                         target=100.0, rationale="primary objective",
                                         confidence=0.8)],
        proposed_tunables=[ProposedTunable(name="leverage", min=1.0, max=10.0,
                                           inferred_type="float", rationale="risk knob",
                                           confidence=0.7)],
        warnings=["liquidations is self-reported"],
    )
    assert p.proposed_metrics[0].dir == "higher"
    assert p.proposed_tunables[0].max == 10.0


def test_invalid_fields_rejected():
    with pytest.raises(ValidationError):
        ProposedMetric(name="x", dir="sideways", weight=1.0, target=1.0,
                       rationale="", confidence=0.5)
    with pytest.raises(ValidationError):
        ProposedMetric(name="x", dir="higher", weight=0.0, target=1.0,
                       rationale="", confidence=0.5)
    with pytest.raises(ValidationError):
        ProposedMetric(name="x", dir="higher", weight=1.0, target=1.0,
                       rationale="", confidence=1.5)
