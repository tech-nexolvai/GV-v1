"""The trainer seam is a socket and nothing more.

Verification for: `eval/trainer.py` (#553).

`test_nothing_in_this_repository_trains_anything` is the one that matters. It is the same guard the
redline enum has in `tests/workflow/test_generate_outputs.py`: adding an implementation has to be a
deliberate act that fails a test and makes somebody read the module docstring, rather than something
that arrives in a pull request about something else.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from eval.trainer import ModelArtifact, Trainer, TrainingNotImplemented

REPO_ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64


def test_nothing_in_this_repository_trains_anything() -> None:
    """**The hard stop.** No class implements `Trainer`, and nothing calls `train`.

    There is nothing to train on: `data/goldset/` holds no answer keys, #274 is open and #188 has
    never been started. Three of the plausible answers are external services with entirely different
    contracts and one is "do not train at all" — so an implementation now would answer a provider
    question by accident.

    Asserted against the source rather than by importing, because an implementation could live in a
    module nothing imports yet and still be the decision this guards.
    """
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.glob("*/**/*.py")):
        if ".venv" in path.parts or path.name == "test_trainer.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "Trainer" not in text and "train(" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            # A class claiming to be a Trainer.
            if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name)
                and base.id == "Trainer"
                or isinstance(base, ast.Attribute)
                and base.attr == "Trainer"
                for base in node.bases
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.name}")
            # A call to `.train(...)`, which is the other way this arrives.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "train"
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} calls .train(...)")

    assert not offenders, (
        "something now trains a model. That is a decision about a provider, privacy and cost — read "
        "eval/trainer.py's docstring, then change this test deliberately:\n  "
        + "\n  ".join(offenders)
    )


def test_the_interface_takes_an_answer_key_and_nothing_else() -> None:
    """Outcome: `train` takes the goldset and no pipeline, rulebook, session or drawing.

    The narrowness is the control. A reader trained on reviewed answers is a reader; a signature that
    reached further would let a trained component start deciding things, which `AGENTS.md` §2 forbids
    outright.
    """
    parameters = inspect.signature(Trainer.train).parameters

    assert list(parameters) == ["self", "goldset"]


def test_the_artifact_makes_no_accuracy_claim() -> None:
    """Outcome: no field a trainer could report its own score in.

    A number a trainer reports about itself is computed on data it chose, usually the data it fitted.
    The number that matters comes from `eval/metrics.py` against held-out cases, and a rival field
    here would give a release conversation two answers with one of them unearned.
    """
    fields = set(ModelArtifact.__dataclass_fields__)

    assert not {name for name in fields if "accur" in name or "score" in name or "metric" in name}
    assert fields == {"content_hash", "trainer_id", "trained_on"}


def test_an_artifact_must_name_what_it_learned_from() -> None:
    """Input: no cases. Outcome: refused.

    An artifact that cannot say what it was fitted to cannot be re-derived, compared, or withdrawn
    when one of those cases turns out to be wrong — and a case being wrong is the ordinary way an
    answer key improves.
    """
    with pytest.raises(ValueError, match="trained_on"):
        ModelArtifact(content_hash=DIGEST, trainer_id="t/1", trained_on=())


def test_an_artifact_is_identified_the_way_everything_else_is() -> None:
    """Input: a bare hex digest. Outcome: refused, because one dialect is the point.

    `storage/hashing.py` emits `sha256:…` and `eval/gold_set/schema.py` requires it. A second
    spelling would mean a stored artifact and a trained one could not be compared without somebody
    translating between them.
    """
    with pytest.raises(ValueError, match="sha256"):
        ModelArtifact(content_hash="a" * 64, trainer_id="t/1", trained_on=("case-1",))


def test_the_refusal_is_its_own_type() -> None:
    """Outcome: a `NotImplementedError` a caller can catch precisely.

    So a caller can say something useful rather than matching on a message, and so this file can
    assert on the type when an implementation is eventually written.
    """
    assert issubclass(TrainingNotImplemented, NotImplementedError)
