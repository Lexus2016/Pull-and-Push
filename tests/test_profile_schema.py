import pytest
from pydantic import ValidationError
from tyani_tolkai.profile_schema import (
    BotProfile, EntryPoint, Tunable, DataSource, ExtractableFact, Risk,
)


def test_minimal_profile_roundtrips():
    p = BotProfile(
        analyzer_engine="claude",
        bot_name="mybot",
        source_root="/tmp/mybot",
        language="python",
        framework="custom",
    )
    dumped = p.model_dump_json()
    back = BotProfile.model_validate_json(dumped)
    assert back == p
    assert back.schema_version == "1"
    assert back.tunable_surface == []
    assert back.unknowns == []


def test_full_profile_validates():
    p = BotProfile(
        analyzer_engine="codex",
        bot_name="b",
        source_root="b",
        language="python",
        runtime="python3.11",
        framework="freqtrade",
        entry_point=EntryPoint(
            kind="function", location="strategy.py:signals",
            inputs="OHLCV bars", outputs="position sequence",
            confidence=0.9, evidence=["strategy.py:40"],
        ),
        tunable_surface=[Tunable(
            name="leverage", location="strategy.py:21", current_value="3.0",
            inferred_type="float", semantic_role="leverage",
            confidence=0.8, evidence=["strategy.py:21"],
        )],
        data_source=DataSource(
            kind="bundled-file", location="data.csv",
            format="csv: time,open,high,low,close,volume",
            confidence=0.95, evidence=["strategy.py:5"],
        ),
        extractable_metrics=[ExtractableFact(
            name="return", how="not extractable — engine must measure",
            trustworthy=False, confidence=0.7, evidence=["strategy.py:60"],
        )],
        risks=[Risk(kind="look-ahead", severity="high",
                    detail="uses future bar", evidence=["strategy.py:55"])],
        unknowns=["fee model unclear"],
    )
    assert p.entry_point.location == "strategy.py:signals"
    assert p.tunable_surface[0].name == "leverage"
    assert p.data_source.format == "csv: time,open,high,low,close,volume"
    assert p.extractable_metrics[0].trustworthy is False
    assert p.risks[0].severity == "high"
    assert p.unknowns == ["fee model unclear"]


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        EntryPoint(kind="script", location="x.py", inputs="", outputs="",
                   confidence=1.5, evidence=[])
    with pytest.raises(ValidationError):
        EntryPoint(kind="script", location="x.py", inputs="", outputs="",
                   confidence=-0.1, evidence=[])
