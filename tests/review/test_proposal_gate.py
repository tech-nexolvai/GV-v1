"""No automated path runs from a correction to a rule change.

Source: `AGENTS.md` §2.6; system design §16; issue #235.
Verification: ``app/review/proposal.py``.

*"Corrections silently become rules"* is a named risk, and a plausible one: the ledger is exactly
the data you would mine to tune a tolerance, and tuning a tolerance from the drawings it failed on
is how a check comes to agree with whatever the vendor happened to send.

Two of these tests are structural rather than behavioural, deliberately. A comment saying "do not
automate this" is not a control — the import-graph assertion is, because it fails when somebody
wires the ledger to a publisher, which is the only way this risk actually materialises.
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from uuid import uuid4

import pytest

from app.review.proposal import RuleChangeSuggestion, suggest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Modules that can change what the system decides for future drawings.
PUBLISHING_MODULES = ("rules.governance.publish", "rules.snapshot", "rules.publication")


def _imported_modules(path: Path) -> set[str]:
    """Every dotted module name imported by one file, in full.

    Full paths rather than top-level packages: `app.review` importing `rules.parameters` is
    ordinary, and importing `rules.governance.publish` is the thing this file exists to catch. A
    check at package granularity cannot tell them apart.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _reachable_from(start: Path, *, depth: int = 6) -> set[str]:
    """Modules reachable from `start` by following first-party imports.

    Transitive, because the risk is not a direct call. A proposal module that imported a helper
    which imported a publisher would satisfy a direct-import check and still have the path.
    """
    seen: set[str] = set()
    frontier = [start]
    for _ in range(depth):
        following: list[Path] = []
        for path in frontier:
            if not path.exists():
                continue
            for module in _imported_modules(path):
                if module in seen:
                    continue
                seen.add(module)
                candidate = REPO_ROOT / Path(*module.split(".")).with_suffix(".py")
                if candidate.exists():
                    following.append(candidate)
        if not following:
            break
        frontier = following
    return seen


# ---------------------------------------------------------------------------
# The gate is structural
# ---------------------------------------------------------------------------


def test_the_proposal_module_cannot_reach_anything_that_publishes() -> None:
    """The control this story exists to add.

    A path from here to a publisher is all it would take for a correction pattern to become a rule
    change without a human writing one — and it would look like a helpful convenience in review.
    """
    reachable = _reachable_from(REPO_ROOT / "app" / "review" / "proposal.py")

    offenders = sorted(module for module in reachable if module in PUBLISHING_MODULES)
    assert not offenders, (
        f"app/review/proposal.py can reach {offenders}. A suggestion must not be able to become a "
        "rule change without a human writing one and D6 approving it."
    )


def test_the_correction_ledger_cannot_reach_anything_that_publishes() -> None:
    """Stated from the other end, because the risk is named as *corrections* becoming rules.

    Reading the ledger to find patterns is fine and already happens. What must not exist is a path
    from the data to the act.
    """
    reachable = _reachable_from(REPO_ROOT / "app" / "review" / "ledger.py")

    offenders = sorted(module for module in reachable if module in PUBLISHING_MODULES)
    assert not offenders, f"app/review/ledger.py can reach {offenders}"


def test_a_suggestion_carries_no_state_that_could_apply_it() -> None:
    """No `status`, no `approved`, no `auto_apply`, and no `Rule`.

    Each would turn a suggestion into a change waiting for a rubber stamp, and the distance between
    those two is the whole control. Asserted on the dataclass so adding one is a failing test rather
    than a plausible-looking commit.
    """
    names = {field.name for field in fields(RuleChangeSuggestion)}

    assert names == {"raised_by", "motivating_corrections", "suggestion", "raised_at"}
    for forbidden in ("status", "state", "approved", "auto_apply", "pending", "rule", "proposed"):
        assert forbidden not in names


def test_the_module_offers_no_way_to_build_one_from_the_ledger() -> None:
    """There is no `from_ledger`, and the omission is the point.

    A function that read the ledger and produced a suggestion would make the human step "review the
    generated list" — a much weaker control than "notice a pattern and argue for a change".
    """
    import app.review.proposal as module

    public = {name for name in vars(module) if not name.startswith("_")}
    assert "suggest" in public
    for automated in ("from_ledger", "generate", "auto_suggest", "mine", "propose_from"):
        assert automated not in public


# ---------------------------------------------------------------------------
# A suggestion is evidence plus an argument
# ---------------------------------------------------------------------------


def test_a_suggestion_names_the_corrections_that_motivated_it() -> None:
    corrections = (uuid4(), uuid4(), uuid4())

    raised = suggest(
        raised_by="anant",
        motivating_corrections=corrections,
        suggestion="Fourteen corrections moved the countertop width the same way; the tolerance "
        "may be wrong.",
    )

    assert raised.motivating_corrections == corrections
    assert raised.raised_by == "anant"


def test_a_suggestion_with_no_evidence_is_refused() -> None:
    """Without corrections there is nothing for an approver to check, and "the ledger suggests"
    becomes an assertion nobody can test."""
    with pytest.raises(ValueError, match="must name the corrections"):
        suggest(raised_by="anant", motivating_corrections=(), suggestion="widen the tolerance")


def test_the_same_correction_listed_twice_is_refused() -> None:
    """Fourteen distinct corrections and one correction listed fourteen times read identically in a
    summary and mean very different things."""
    once = uuid4()

    with pytest.raises(ValueError, match="listed twice"):
        suggest(
            raised_by="anant",
            motivating_corrections=(once, once),
            suggestion="widen the tolerance",
        )


@pytest.mark.parametrize("raised_by", ["", "   "])
def test_an_unattributed_suggestion_is_refused(raised_by: str) -> None:
    """The first question about a proposed rule change is who wanted it."""
    with pytest.raises(ValueError, match="must name the person"):
        suggest(
            raised_by=raised_by,
            motivating_corrections=(uuid4(),),
            suggestion="widen the tolerance",
        )


def test_a_suggestion_without_an_argument_is_refused() -> None:
    """A list of corrections with no claim attached leaves the reader to infer one, and they will
    infer the one they already believed."""
    with pytest.raises(ValueError, match="must say what looks wrong"):
        suggest(raised_by="anant", motivating_corrections=(uuid4(),), suggestion="  ")


def test_a_suggestion_is_frozen() -> None:
    """A revised suggestion is a new one. What was originally argued is part of why a later rule
    change is defensible."""
    raised = suggest(
        raised_by="anant", motivating_corrections=(uuid4(),), suggestion="tolerance may be wrong"
    )

    with pytest.raises(FrozenInstanceError):
        raised.suggestion = "something else"  # type: ignore[misc]
