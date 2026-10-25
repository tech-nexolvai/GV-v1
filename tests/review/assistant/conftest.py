"""Every answer a test's engine makes is read back as it would be served (harness pre-check,
change 11): the round trip from every block writer to the read model.

#1934 and #1940 each stored a value the read model refuses (`settled.was = "same"`, a label's
`route = "ocr"`), and opening those paid answers failed with a 500. Here every test that runs the
engine (a chat answer, a full check, a stop, a pause, a Continue) has the blocks it hands over
(`ChatEngine.stored_blocks`) checked by `chat_store.stored_problems`: the same payload and model
`message_out` serves. A writer that makes a block the reader refuses fails the test that made it,
naming the field.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest

from app.models import AssistantChatMessage
from app.review.assistant import chat_engine
from app.review.assistant.chat_store import stored_problems


@pytest.fixture(autouse=True)
def blocks_read_back(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The fields the read model refused in the blocks this test's engines handed over."""
    refused: list[str] = []
    original = chat_engine.ChatEngine.stored_blocks

    def read_back(self: chat_engine.ChatEngine) -> list[dict[str, Any]]:
        blocks = original(self)
        mode = getattr(getattr(self, "settings", None), "mode", None)
        message = AssistantChatMessage(
            chat_id=uuid4(),
            seq=2,
            role="assistant",
            created_by="test",
            text="",
            blocks=blocks,
            api_content=[],
            mode=mode if mode in ("chat", "full_check") else None,
        )
        refused.extend(stored_problems(message))
        return blocks

    monkeypatch.setattr(chat_engine.ChatEngine, "stored_blocks", read_back)
    yield refused
    assert not refused, f"blocks the read model refuses: {sorted(set(refused))[:10]}"
