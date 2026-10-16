"""The AI pair check's stored answers: an identical question is replayed from `model_invocations`.

Verification for: `workflow/compare_ai_pair.py` (`stored_answers`). Every value is made up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models.runs import ModelInvocation
from extraction.compare.ai_pair import AI_PAIR_PROMPT_ID, CLAUDE_PAIR
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_depth_evidence import _Set
from workflow.compare_ai_pair import stored_answers

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def test_a_stored_answer_to_the_identical_question_is_found(
    session: Session, tmp_path: Path
) -> None:
    sheet = _Set(session, LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only"))
    packet: dict[str, object] = {
        "reader": "arch-compare-ai-pair",
        "document_version_id": str(sheet.page.document_version_id),
        "prompt_id": AI_PAIR_PROMPT_ID,
        "images": {"picture_1": {"sha256": "a" * 64}},
    }
    session.add(
        ModelInvocation(
            extraction_run_id=sheet.run.id,
            model_id=CLAUDE_PAIR[0],
            prompt_id=AI_PAIR_PROMPT_ID,
            template_id=AI_PAIR_PROMPT_ID,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            outcome="ok",
            cost_micros=1,
            private_raw_response=json.dumps({"stored": True}),
            reader_question_packet=packet,
        )
    )
    session.flush()
    find = stored_answers(session)

    assert find(CLAUDE_PAIR[0], packet) == json.dumps({"stored": True})
    assert find(CLAUDE_PAIR[1], packet) is None
    other: Mapping[str, object] = {**packet, "images": {"picture_1": {"sha256": "b" * 64}}}
    assert find(CLAUDE_PAIR[0], other) is None
