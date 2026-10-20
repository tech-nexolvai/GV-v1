"""One paid full check at a time per account (#1770, owner rule "one paid job at a time"): the
server's registry refuses a second paid full check in any chat while one runs, in plain words; an
ordinary message and a second message in the same chat keep their own rules. No database, no call.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.review.assistant.chat_service import PAID_JOB_WORDS, ChatRegistry, PaidJobRunning


def test_a_second_paid_full_check_in_another_chat_is_refused_until_the_first_ends() -> None:
    registry = ChatRegistry()
    package = uuid4()
    first_chat, second_chat, third_chat = uuid4(), uuid4(), uuid4()
    first = registry.claim(first_chat, package, uuid4(), paid=True)
    assert first is not None and first.paid
    first.started = True
    with pytest.raises(PaidJobRunning) as refused:
        registry.claim(second_chat, uuid4(), uuid4(), paid=True)  # another set too
    assert refused.value.other is first and str(refused.value) == PAID_JOB_WORDS
    # An ordinary question elsewhere is not a paid full check: it runs.
    question = registry.claim(third_chat, package, uuid4())
    assert question is not None and not question.paid
    # The same chat keeps its own rule: busy, not a paid-job refusal.
    assert registry.claim(first_chat, package, uuid4(), paid=True) is None
    registry.release(first)
    second = registry.claim(second_chat, package, uuid4(), paid=True)
    assert second is not None and second.paid


def test_the_refusal_words_are_plain() -> None:
    assert "Only one full check runs at a time" in PAID_JOB_WORDS
    assert "stop it" in PAID_JOB_WORDS
