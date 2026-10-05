"""
.env loading (core/env.py) and CLI campaign-name matching.

Run: python -m pytest tests/test_env.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cli import _get_campaign  # noqa: E402
from core.env import load_dotenv  # noqa: E402


def test_dotenv_fills_only_what_is_missing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "DATABASE_URL=postgresql://localhost/from_file\n"
        "export QUOTED=\"two words\"\n"
        "SINGLE='x # not a comment'\n"
        "TRAILING=value # a comment\n"
        "EMPTY=\n"
        "OWNER=first@x.com\n"
        "OWNER=second@x.com\n"
        "SHELL_WINS=file\n"
        "not a setting\n"
    )
    for key in ("DATABASE_URL", "QUOTED", "SINGLE", "TRAILING", "EMPTY", "OWNER", "AGENCY_OS_DOTENV"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SHELL_WINS", "shell")
    loaded = load_dotenv(env)
    import os
    assert os.environ["DATABASE_URL"] == "postgresql://localhost/from_file"
    assert os.environ["QUOTED"] == "two words" and os.environ["SINGLE"] == "x # not a comment"
    assert os.environ["TRAILING"] == "value"
    assert "EMPTY" not in os.environ and os.environ["OWNER"] == "second@x.com"
    assert os.environ["SHELL_WINS"] == "shell" and "SHELL_WINS" not in loaded
    for key in loaded:
        monkeypatch.delenv(key)


def test_dotenv_can_be_switched_off(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SOMETHING_NEW=1\n")
    monkeypatch.setenv("AGENCY_OS_DOTENV", "off")
    assert load_dotenv(env) == []


def test_short_campaign_names():
    campaigns = [SimpleNamespace(db_name="voter-guide--cbo-outreach-los-angeles", name="Voter Guide — CBO Outreach"),
                 SimpleNamespace(db_name="voter-guide--elected-officials-outreach-california", name="Officials")]
    assert _get_campaign(campaigns, "voter-guide-cbo") is campaigns[0]
    assert _get_campaign(campaigns, "voter-guide--elected-officials-outreach-california") is campaigns[1]
    assert _get_campaign(campaigns, "Officials") is campaigns[1]
    assert _get_campaign(campaigns, "voter-guide") is None  # ambiguous
    assert _get_campaign(campaigns, "nope") is None
