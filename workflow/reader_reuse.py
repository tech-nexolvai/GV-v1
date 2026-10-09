"""Reuse a reader's stored answer when a re-run asks it the identical question (#1112).

**Why.** The Claude readers have no fixed temperature, so the same question asked twice can come back
different: the same label's `combined` flag, a page's walls, even the chosen row moved between runs
of one drawing. A re-run after a reviewer's answer could then change other rows' holds for no
reason the reviewer could see, and every re-run paid the full reading price again.

**What counts as the identical question.** A stored answer is reused only when all of these match:

* the **document version** and the page (both in the question packet);
* the **model id** and the **prompt id** (a changed question has a new prompt id, so it re-asks);
* every **picture's SHA-256** and the rest of the packet — the source page hash, the coordinate
  transform, the question id, the effort, the counts.

Left out of the comparison are only the values minted fresh for each run: the candidate ids
(replaced by how many there are), the packet's own hash (which covers those ids), where each
picture is stored (named after its hash), and the reuse marker itself. The drawing set's product,
told to a label reader in one line, is not in the packet; it is the package's, set when the package
is created and never changed, so it is the same for every revision that holds this document version.

**Which answers.** Only a call recorded `ok` with its raw reply kept. A failed, timed-out, refused or
rejected attempt is never reused. The stored reply is read again by exactly the code that read it
live (`extraction.slot_reader.bedrock.replay_stored_answer`); one that does not read as a complete
answer is asked again as usual. Of several matching answers, the newest is reused, so a proof run
that re-asked (`GV_READER_REUSE_ANSWERS=false`) sets the answer later runs reuse.

**What is recorded.** Each reused answer is still one `model_invocations` row under the new run, so
the run's evidence and its links to answers are complete: the same raw reply, the new run's own
question packet plus `reused_from` (the id of the call that was actually made), and zero tokens,
zero cost, zero time. Usage, ceilings and attribution leave such rows out (`made_a_call`), and the
spend guard and pacer never see the question, because no call is made.

Source: issue #1112 · Verification: `tests/workflow/test_reader_reuse.py`
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.runs import REUSED_FROM_KEY, ModelInvocation, ModelInvocationOutcome
from extraction.form_reader.bedrock import AttemptUsage
from extraction.slot_reader.bedrock import (
    ArchPairAnswer,
    CounterBreakAnswer,
    CropJob,
    RowChoiceAnswer,
    job_prompt_id,
    replay_stored_answer,
)
from extraction.slot_reader.claude_output import DEFAULT_CLAUDE_EFFORT, ClaudeEffort
from extraction.slot_reader.seal import ReaderAnswer
from extraction.slot_reader.walls import WallAnswer
from vocabulary.semantic_types import ProductType

__all__ = [
    "StoredAnswer",
    "StoredAnswerSource",
    "StoredReaderAnswers",
    "question_identity",
    "reuse_stored_answers",
]

type ReaderReply = (
    ReaderAnswer | WallAnswer | RowChoiceAnswer | CounterBreakAnswer | ArchPairAnswer
)

#: Packet lists of ids minted fresh for every run; only how many there are is part of the question.
_RUN_IDS: Final = frozenset({"candidate_ids", "architect_candidate_ids"})


def _signed(packet: Mapping[str, object]) -> bool:
    """The packet's own hash still matches its contents (the reuse marker is never covered)."""
    expected = packet.get("packet_sha256")
    body = {
        key: value for key, value in packet.items() if key not in ("packet_sha256", REUSED_FROM_KEY)
    }
    actual = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return isinstance(expected, str) and expected == actual


def question_identity(packet: Mapping[str, object]) -> str | None:
    """The hash of what a question shows and asks, the same on every run; `None` if unusable.

    A packet without a document version, a prompt id or a hash for every picture cannot be
    compared safely, so it is never matched.
    """
    images = packet.get("images")
    if (
        not isinstance(packet.get("document_version_id"), str)
        or not isinstance(packet.get("prompt_id"), str)
        or not isinstance(images, Mapping)
        or not images
    ):
        return None
    pictures: dict[str, str] = {}
    for name, image in images.items():
        digest = image.get("sha256") if isinstance(image, Mapping) else None
        if not isinstance(digest, str) or not digest:
            return None
        pictures[str(name)] = digest
    body: dict[str, object] = {}
    for key, value in packet.items():
        if key in ("packet_sha256", REUSED_FROM_KEY, "images"):
            continue
        if key in _RUN_IDS:
            if not isinstance(value, list):
                return None
            body[f"{key}:count"] = len(value)
            continue
        body[key] = value
    body["images"] = pictures
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredAnswer:
    """One accepted stored reply and the id of the call that actually produced it."""

    invocation_id: UUID
    raw_response: str


class StoredAnswerSource(Protocol):
    """Finds stored answers for a batch of jobs: `StoredReaderAnswers` in a run, a fake in tests."""

    def find(
        self, jobs: Sequence[CropJob], *, product: ProductType | None
    ) -> Mapping[tuple[str, str], StoredAnswer]: ...


@dataclass(frozen=True, slots=True)
class StoredReaderAnswers:
    """The stored answers the reader may reuse, read from `model_invocations`."""

    session: Session

    def find(
        self, jobs: Sequence[CropJob], *, product: ProductType | None
    ) -> dict[tuple[str, str], StoredAnswer]:
        """The newest accepted stored answer to each job's identical question, by job key."""
        wanted: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
        versions: set[str] = set()
        for job in jobs:
            packet = job.question_packet
            if packet is None:
                continue
            identity = question_identity(packet)
            version = packet.get("document_version_id")
            if identity is None or not isinstance(version, str):
                continue
            versions.add(version)
            wanted.setdefault((job.model_id, job_prompt_id(job, product), identity), []).append(
                (job.key, job.model_id)
            )
        if not wanted:
            return {}
        rows = self.session.execute(
            select(
                ModelInvocation.id,
                ModelInvocation.model_id,
                ModelInvocation.prompt_id,
                ModelInvocation.reader_question_packet,
                ModelInvocation.private_raw_response,
            )
            .where(
                ModelInvocation.outcome == ModelInvocationOutcome.OK.value,
                ModelInvocation.extraction_run_id.is_not(None),
                ModelInvocation.private_raw_response.is_not(None),
                ModelInvocation.model_id.in_({model for model, _prompt, _id in wanted}),
                ModelInvocation.prompt_id.in_({prompt for _model, prompt, _id in wanted}),
                ModelInvocation.reader_question_packet["document_version_id"].astext.in_(versions),
            )
            .order_by(ModelInvocation.created_at.desc(), ModelInvocation.id.desc())
        ).all()
        found: dict[tuple[str, str], StoredAnswer] = {}
        for invocation_id, model_id, prompt_id, packet, raw in rows:
            if not isinstance(packet, Mapping) or not isinstance(raw, str) or not _signed(packet):
                continue
            identity = question_identity(packet)
            if identity is None:
                continue
            asked = packet.get(REUSED_FROM_KEY)
            source = invocation_id
            if asked is not None:
                # A reused row points at the call that was made; reuse points there too.
                try:
                    source = UUID(str(asked))
                except ValueError:
                    continue
            for job_key in wanted.get((model_id, prompt_id, identity), ()):
                found.setdefault(job_key, StoredAnswer(source, raw))
        return found


def reuse_stored_answers(
    jobs: Sequence[CropJob],
    *,
    stored: StoredAnswerSource,
    record_attempt: Callable[[AttemptUsage], None],
    max_tokens: int,
    product: ProductType | None = None,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> tuple[dict[tuple[str, str], ReaderReply], list[CropJob]]:
    """Answer every job that has an identical stored question; return those answers and the rest.

    Each reused answer is recorded through `record_attempt` with `reused_from` set and no tokens.
    The jobs returned are asked as usual.
    """
    available = stored.find(jobs, product=product)
    answers: dict[tuple[str, str], ReaderReply] = {}
    remaining: list[CropJob] = []
    for job in jobs:
        hit = available.get((job.key, job.model_id))
        replayed = (
            None
            if hit is None
            else replay_stored_answer(
                job,
                hit.raw_response,
                reused_from=str(hit.invocation_id),
                max_tokens=max_tokens,
                product=product,
                claude_effort=claude_effort,
            )
        )
        if replayed is None:
            remaining.append(job)
            continue
        answer, attempt = replayed
        record_attempt(attempt)
        answers[(job.key, job.model_id)] = answer
    return answers, remaining
