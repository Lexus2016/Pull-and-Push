"""P1 read-only Bot Profile analyzer (spec: docs/design/p1-bot-profile-analyzer.md).

Pure single-pass LLM. We gather the bot's text into a prompt, call one agent the
same way ``configurator.py`` does (read-only adapter in a TemporaryDirectory), and
validate the agent's JSON into a BotProfile. The bot is never executed and the agent
never receives the bot's path.
"""
from __future__ import annotations

from pathlib import Path

from .configurator import extract_json
from .profile_schema import BotProfile


class ProfileError(Exception):
    """Raised when the analyzer cannot produce a valid BotProfile.

    NOTE: parse_profile intentionally lets ValueError (no JSON) and pydantic
    ValidationError (bad shape) propagate RAW — the orchestrator analyze_bot
    (Task 5) catches exactly those to drive its one-shot repair retry, and only
    then wraps a final failure as ProfileError. Do NOT wrap exceptions inside
    parse_profile, or analyze_bot's repair path will stop working.
    """


def parse_profile(text: str, *, engine: str, source_root: str | Path) -> BotProfile:
    """Extract the JSON object from agent output and validate it into a BotProfile.

    We stamp the provenance/identity fields (analyzer_engine, source_root, bot_name)
    ourselves — they are facts we know, not things the LLM should guess.
    """
    data = extract_json(text)                     # raises ValueError if no JSON object
    data["analyzer_engine"] = engine
    data["source_root"] = str(source_root)
    data.setdefault("bot_name", Path(source_root).name or "bot")
    return BotProfile.model_validate(data)        # raises ValidationError on bad shape
