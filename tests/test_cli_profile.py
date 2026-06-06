from tyani_tolkai import cli
from tyani_tolkai.profile_schema import BotProfile


def test_profile_subcommand_writes_both_files(tmp_path, monkeypatch):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "s.py").write_text("x = 1\n", encoding="utf-8")
    out = tmp_path / "out"

    fake = BotProfile(analyzer_engine="claude", bot_name="bot", source_root=str(bot),
                      language="python", framework="custom")

    def fake_analyze(src, *, engine, model, timeout):
        assert str(src) == str(bot)
        return fake

    monkeypatch.setattr(cli, "_analyze_bot", fake_analyze, raising=False)

    rc = cli.main(["profile", str(bot), "--out", str(out)])
    assert rc == 0
    assert (out / "profile.json").exists()
    assert (out / "profile.md").exists()
    assert '"language": "python"' in (out / "profile.json").read_text(encoding="utf-8")


def test_profile_subcommand_missing_path_returns_2(tmp_path):
    rc = cli.main(["profile", str(tmp_path / "nope")])
    assert rc == 2
