from tyani_tolkai import cli
from tyani_tolkai.profile_schema import BotProfile
from tyani_tolkai.proposal_schema import MetricProposal


def _write_profile(path):
    prof = BotProfile(analyzer_engine="claude", bot_name="b", source_root="/x",
                      language="python", framework="custom")
    path.write_text(prof.model_dump_json(), encoding="utf-8")


def test_propose_subcommand_writes_both_files(tmp_path, monkeypatch):
    pf = tmp_path / "profile.json"
    _write_profile(pf)
    out = tmp_path / "out"
    fake = MetricProposal(proposer_engine="claude", bot_name="b", goal="max return")

    def fake_propose(profile, goal, *, engine, model, timeout):
        assert isinstance(profile, BotProfile)
        assert goal == "max return"
        return fake

    monkeypatch.setattr(cli, "_propose_evaluation", fake_propose, raising=False)
    rc = cli.main(["propose", str(pf), "--goal", "max return", "--out", str(out)])
    assert rc == 0
    assert (out / "proposal.json").exists()
    assert (out / "proposal.md").exists()
    assert '"goal": "max return"' in (out / "proposal.json").read_text(encoding="utf-8")


def test_propose_subcommand_missing_profile_returns_2(tmp_path):
    rc = cli.main(["propose", str(tmp_path / "nope.json"), "--goal", "x"])
    assert rc == 2


def test_propose_subcommand_proposal_error_returns_2(tmp_path, monkeypatch):
    from tyani_tolkai.proposer import ProposalError
    pf = tmp_path / "profile.json"
    _write_profile(pf)

    def boom(profile, goal, *, engine, model, timeout):
        raise ProposalError("llm failed twice")

    monkeypatch.setattr(cli, "_propose_evaluation", boom, raising=False)
    rc = cli.main(["propose", str(pf), "--goal", "x"])
    assert rc == 2
