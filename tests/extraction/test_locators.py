"""Colour finds the sink cut-out; it never decides anything about it.

Source: `docs/decisions/CALL_2026_08_25_INPUTS.md` N1 — vendors may outline the cut-out in blue as a
locator hint; the extractor must still work without it, and colour is never verdict evidence.
Verification for: `extraction/locators.py`.

The load-bearing tests are the two structural ones: a hint has nowhere to put a value, and this
module cannot reach the verdict layer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from extraction.locators import LocatorHint, LocatorSource, prefer

SOURCE = Path(__file__).resolve().parents[2] / "extraction" / "locators.py"


def _hint(source: LocatorSource, page: int = 1) -> LocatorHint:
    return LocatorHint(source=source, page=page, region=(10, 10, 110, 60))


# ---------------------------------------------------------------------------
# A locator cannot become evidence
# ---------------------------------------------------------------------------


def test_a_hint_has_nowhere_to_put_a_dimension() -> None:
    """**The guarantee, made structural rather than promised in a docstring.**

    Colour says *look here*; it must never say *this is 32 1/2 inches*. The simplest way to ensure
    that is to leave no field for a value — a future author who wants colour to carry one has to
    change the shape, which is a change somebody reviews, rather than populate a field that was
    already there.
    """
    fields = set(LocatorHint.__dataclass_fields__)

    assert fields == {"source", "page", "region", "note"}
    for forbidden in ("value", "measurement", "dimension", "exact", "unit", "status", "evidence"):
        assert forbidden not in fields


def test_the_module_cannot_reach_the_verdict_or_evidence_layers() -> None:
    """Asserted on the imports, because a test showing today's code not doing it would pass just as
    happily the day somebody adds the import.

    **Parsed, not grepped.** The first version searched for the string `from rules.` and so missed
    `from rules import ...` entirely — a forbidden import could have been added with this test still
    green, which is the failure mode of every guard written as a substring search. Walking the AST
    checks the module root whatever the import is spelled like.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    forbidden = {"verdict", "evidence", "rules"}

    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])

    assert not (roots & forbidden), (
        f"locators.py imports {sorted(roots & forbidden)}. A locator must not be able to reach the "
        "layer that decides, or colour becomes evidence by import."
    )


# ---------------------------------------------------------------------------
# Colour is tried last, not first
# ---------------------------------------------------------------------------


def test_geometry_and_labels_are_preferred_over_colour() -> None:
    """**The opposite of how tempting colour is.**

    It is the only source depending on a vendor having adopted a convention nobody has agreed to. A
    search leaning on it first works beautifully on the drawings that follow it and quietly worse on
    the rest — which is the failure that looks like success during a demo.
    """
    ordered = prefer(
        (
            _hint(LocatorSource.COLOUR),
            _hint(LocatorSource.LABEL),
            _hint(LocatorSource.GEOMETRY),
        )
    )

    assert [hint.source for hint in ordered] == [
        LocatorSource.GEOMETRY,
        LocatorSource.LABEL,
        LocatorSource.COLOUR,
    ]


def test_ordering_never_discards_the_colour_hint() -> None:
    """Ordering, not filtering. A colour hint is still worth trying when nothing else suggested a
    region — dropping it would lose the case N1 was raised for."""
    hints = (_hint(LocatorSource.COLOUR, page=2), _hint(LocatorSource.COLOUR, page=1))

    assert len(prefer(hints)) == 2


def test_ordering_is_stable_for_equal_sources() -> None:
    """Two blue outlines on different pages must come back in a fixed order, or a pipeline that
    takes the first gets a different region depending on dictionary iteration."""
    hints = (_hint(LocatorSource.COLOUR, page=3), _hint(LocatorSource.COLOUR, page=1))

    assert [hint.page for hint in prefer(hints)] == [1, 3]


def test_no_hints_is_not_an_error() -> None:
    """Most packages have no colour convention and no detector output. Absence is the normal case."""
    assert prefer(()) == ()


# ---------------------------------------------------------------------------
# A hint that points at nothing is a bug in the detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("region", [(10, 10, 10, 60), (10, 10, 110, 10), (110, 60, 10, 10)])
def test_an_empty_or_inverted_region_is_refused(region: tuple[int, int, int, int]) -> None:
    """A zero-area or backwards box would send a search to look at nothing and report finding
    nothing, which reads downstream as "the cut-out is not on this page"."""
    with pytest.raises(ValueError, match="empty or inverted"):
        LocatorHint(source=LocatorSource.COLOUR, page=1, region=region)


def test_the_source_must_be_one_of_the_three() -> None:
    """A closed list, because each member is a claim about reliability and `prefer` ranks on it. A
    string would rank as nothing and sort unpredictably."""
    with pytest.raises(TypeError, match="LocatorSource"):
        LocatorHint(source="blue", page=1, region=(0, 0, 1, 1))  # type: ignore[arg-type]
