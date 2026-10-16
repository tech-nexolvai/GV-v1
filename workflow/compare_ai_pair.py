"""The direct compare's AI pair check, for the caller that records the compare (#1418's builder).

`ai_pair_unpaired` is what that caller runs **after the geometry pass**: it hands in the sheets and
every pairing, and gets every pairing back in the same order, an unpaired one now paired only where
both AIs chose the same vendor line and code's checks did not contradict them
(`extraction.compare.ai_pair`). **Off by default** (`GV_ARCH_COMPARE_AI_PAIR`): switched off, the
pairings come back unchanged and nothing is asked.

With a database session, a stored answer to the identical question (the newest accepted
`model_invocations` row with the same question identity) is replayed, never paid twice; without one
(`None`, a script), the caller's own `stored` lookup is used, if any. Every attempt goes to the
caller's `record_attempt` (its call ledger), so `model_invocations` gets each call's cost.

Source: issue #1427. Verification: `tests/workflow/test_compare_ai_pair.py`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from extraction.compare.ai_pair import (
    AI_PAIR_PROMPT_ID,
    CLAUDE_PAIR,
    MEASURED_AI_PAIR_SETTINGS,
    AiPairSettings,
    PdfPictures,
    Pictures,
    ai_pair_enabled,
    pair_with_ais,
)
from extraction.compare.page import ComparePage
from extraction.compare.pair import Pairing
from extraction.form_reader.bedrock import AttemptUsage

__all__ = ["ai_pair_unpaired", "stored_answers"]


def stored_answers(session: Session) -> Callable[[str, Mapping[str, object]], str | None]:
    """The newest accepted stored answer to the identical AI pair question, or `None`."""
    from app.models.runs import ModelInvocation, ModelInvocationOutcome
    from workflow.reader_reuse import question_identity

    def find(model_id: str, packet: Mapping[str, object]) -> str | None:
        identity = question_identity(packet)
        version = packet.get("document_version_id")
        if identity is None or not isinstance(version, str):
            return None
        rows = session.execute(
            select(ModelInvocation.reader_question_packet, ModelInvocation.private_raw_response)
            .where(
                ModelInvocation.outcome == ModelInvocationOutcome.OK.value,
                ModelInvocation.model_id == model_id,
                ModelInvocation.prompt_id == AI_PAIR_PROMPT_ID,
                ModelInvocation.private_raw_response.is_not(None),
                ModelInvocation.reader_question_packet["document_version_id"].astext == version,
            )
            .order_by(ModelInvocation.created_at.desc(), ModelInvocation.id.desc())
        ).all()
        for stored_packet, raw in rows:
            if isinstance(stored_packet, Mapping) and question_identity(stored_packet) == identity:
                return str(raw)
        return None

    return find


def ai_pair_unpaired(
    session: Session | None,
    pages: Sequence[ComparePage],
    pairings: Sequence[Pairing],
    *,
    pdf: bytes,
    document_version_id: str,
    clients: Mapping[str, Any] | None,
    record_attempt: Callable[[AttemptUsage], None],
    spend_guard: Any = None,
    stored: Callable[[str, Mapping[str, object]], str | None] | None = None,
    stored_vendor_inches: Callable[[Pairing], Fraction | None] | None = None,
    settings: AiPairSettings = MEASURED_AI_PAIR_SETTINGS,
    models: Sequence[str] = CLAUDE_PAIR,
    environ: Mapping[str, str] | None = None,
    pictures: Pictures | None = None,
) -> tuple[Pairing, ...]:
    """Every pairing again, in the same order: an unpaired one paired where both AIs and code agree.

    * `pages`, `pairings`: the geometry pass's output for one PDF (`compare.pair.pair_page` per
      page), every pairing exactly once; geometry pairs are never changed.
    * `clients`: each model id to a client with `converse(**request)`; `None` asks nothing.
    * `stored_vendor_inches`: the caller's stored, sealed reading of the vendor's label for a
      provisional AI pair (by its `page_index` and `vendor_label`), or `None`; used before the two
      AIs' own readings.
    * `spend_guard`: the job's spend guard (`BatchSpendGuard`); a refused call stops the asking.

    Switched off (`GV_ARCH_COMPARE_AI_PAIR`), the pairings come back unchanged."""
    if not ai_pair_enabled(os.environ if environ is None else environ):
        return tuple(pairings)
    if stored is None and session is not None:
        stored = stored_answers(session)
    return pair_with_ais(
        pages,
        pairings,
        pictures=PdfPictures(pdf, settings) if pictures is None else pictures,
        clients=clients,
        document_version_id=document_version_id,
        record_attempt=record_attempt,
        settings=settings,
        spend_guard=spend_guard,
        stored=stored,
        stored_vendor_inches=stored_vendor_inches,
        models=models,
    ).pairings
