"""An unknown `GV_ASSISTANT_*` / `GV_REVIEW_*` variable is warned about at start (owner decision
2026-10-19).

`extra="forbid"` refuses an unknown key in `.env` only: an unknown variable in the process's own
environment was ignored without a word, so a typo in a start script left the default in place.
Now the app warns loudly at start, naming each one, and still starts (a refusal could stop a
running deployment over a stale line). Other `GV_` families are read by other code and are not
judged.
"""

from __future__ import annotations

import logging

import pytest

from app.config import Settings, unknown_settings


def test_only_unknown_assistant_and_review_names_are_listed() -> None:
    environ = {
        "GV_ASSISTANT_FULL_CHECK_CAPX_USD": "3",
        "GV_REVIEW_ASSISTANT_SANDBOXX": "docker",
        "GV_ASSISTANT_MESSAGE_CAP_USD": "15",
        "GV_ASSISTANT_FULL_CHECK_CAP_USD": "5",
        "GV_REVIEW_ASSISTANT_ENABLED": "1",
        "GV_READER_LINE_MINIMUM_PT": "6",
        "GV_DEV_PORT": "8000",
        "PATH": "/usr/bin",
    }

    assert unknown_settings(environ) == [
        "GV_ASSISTANT_FULL_CHECK_CAPX_USD",
        "GV_REVIEW_ASSISTANT_SANDBOXX",
    ]


def test_the_app_warns_loudly_and_still_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.main import create_app

    monkeypatch.setenv("GV_ASSISTANT_FULL_CHECK_CAPX_USD", "3")
    caplog.set_level(logging.WARNING)

    app = create_app(
        Settings(database_url="postgresql+psycopg://unused@localhost/unused", environment="test")
    )

    assert app is not None
    warned = [r for r in caplog.records if "UNKNOWN SETTING" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING
    assert "GV_ASSISTANT_FULL_CHECK_CAPX_USD" in warned[0].getMessage()


def test_no_warning_when_every_name_is_a_setting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.main import create_app

    for name in list(__import__("os").environ):
        if name.startswith(("GV_ASSISTANT_", "GV_REVIEW_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GV_ASSISTANT_MESSAGE_CAP_USD", "15")
    caplog.set_level(logging.WARNING)

    create_app(
        Settings(database_url="postgresql+psycopg://unused@localhost/unused", environment="test")
    )

    assert not [r for r in caplog.records if "UNKNOWN SETTING" in r.getMessage()]
