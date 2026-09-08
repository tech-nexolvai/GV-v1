"""The socket a trained reader would plug into. Deliberately empty.

There is no training in this project and there is nothing to train on: `data/goldset/` holds no
answer keys, `#274` is open, and #188 — annotating the first representative cases — has never been
started. This module exists so that the *shape* of the eventual decision is written down, and so
that whoever makes it does not have to invent a boundary at the same time as choosing a provider.

**Why an interface and no implementation.** Three of the plausible answers are external services
with entirely different contracts — Google Document AI, Azure Document Intelligence, a fine-tune of
one of the vision models already behind `extraction/models/` — and one is "do not train at all,
because a deterministic reader plus a reviewer is enough". Committing an implementation now would
answer that question by accident. The seam is the honest artifact; the choice is Abhishek's and it
has no deadline this module can help with.

**What the interface says, and it is not much on purpose.** A trainer takes an answer key and returns
an artifact identified by content. It does *not* take drawings, a pipeline, a rulebook or a database
handle: a reader trained on reviewed answers is a reader, and any signature that let it reach further
would let a trained component start deciding things. `ModelArtifact` carries no accuracy claim
either, because a number a trainer reports about itself is not a measurement — that is what
`eval/metrics.py` and the gold-set gate are for, and they run on held-out cases rather than on the
trainer's word.

**Nothing in this repository calls `train`.** `tests/eval/test_trainer.py` asserts that, and asserts
that no implementation has appeared, in the same spirit as the redline guard in
`tests/workflow/test_generate_outputs.py`: adding one has to be a deliberate act that fails a test
and makes somebody read this docstring.

Source: issue #553 · Verification: `tests/eval/test_trainer.py`
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from eval.gold_set.schema import GoldCase

__all__ = ["CONTENT_HASH", "ModelArtifact", "Trainer", "TrainingNotImplemented"]

#: `sha256:<64 lowercase hex>` — the one dialect this codebase uses for content identity, the same
#: form `storage/hashing.py` emits and `eval/gold_set/schema.py` requires. A second spelling would
#: mean a stored artifact and a trained one could not be compared without translation.
CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class TrainingNotImplemented(NotImplementedError):
    """Raised by any call that would train something. There is nothing to train on.

    A distinct type rather than a bare `NotImplementedError` so a caller can catch exactly this and
    say something useful, and so the guard test can assert on it rather than on a message.
    """


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """A trained reader, identified by what it is rather than by where it was put.

    **No accuracy field, deliberately.** A trainer reporting its own score is a claim, not a
    measurement: it is computed on data the trainer chose, usually the data it fitted. The number
    that matters is produced by `eval/metrics.py` against held-out gold cases, and putting a rival
    number here would give a release conversation two answers with one of them unearned.

    `trained_on` is the case ids, not the cases. An artifact is a statement about specific reviewed
    material and has to name it — but copying the answers in would put client dimensions inside an
    artifact record that may travel further than they should.
    """

    content_hash: str
    trainer_id: str
    """Which trainer produced it, version included. "Which model reads this" is not answerable from a
    provider name once the provider has moved on — the same reason `NovaConfig` names a model id in
    full."""

    trained_on: tuple[str, ...]

    def __post_init__(self) -> None:
        if CONTENT_HASH.fullmatch(self.content_hash) is None:
            raise ValueError(
                f"content_hash {self.content_hash!r} must be 'sha256:<64 lowercase hex>' — the form "
                "storage/hashing.py emits, so a trained artifact and a stored one are comparable."
            )
        if not isinstance(self.trainer_id, str) or not self.trainer_id.strip():
            raise ValueError("trainer_id must name what produced this, version included")
        if not self.trained_on:
            raise ValueError(
                "trained_on must name the gold cases this was fitted to. An artifact that cannot "
                "say what it learned from cannot be re-derived, compared, or withdrawn when a case "
                "turns out to be wrong."
            )


@runtime_checkable
class Trainer(Protocol):
    """Answer key in, model artifact out. The whole contract.

    A `Protocol` rather than a base class, for the reason `extraction/models/openmodel.py` gives
    about its own client: a provider adapter should not have to import this project to satisfy it,
    and `runtime_checkable` lets a test assert a candidate implements it without instantiating one.

    The narrowness is the point. No drawings, no pipeline, no rulebook, no session: a reader trained
    on reviewed answers is a reader, and a signature that reached further would let a trained
    component start deciding things — which is the one thing `AGENTS.md` §2 forbids outright.
    """

    @property
    def trainer_id(self) -> str:
        """Which trainer this is, version included, for the artifact it produces."""

    def train(self, goldset: Sequence[GoldCase]) -> ModelArtifact:
        """Fit a reader to reviewed answers and return the artifact.

        Takes whole `GoldCase`s rather than loose observations because the answer key's structure is
        load-bearing: a case binds each reading to the exact document bytes it was annotated against
        (`ReviewedDocument.content_hash`), and a trainer handed bare values could be fitted to
        annotations that no longer describe any drawing — the failure `eval/gold_set/store.py`
        exists to prevent.
        """
