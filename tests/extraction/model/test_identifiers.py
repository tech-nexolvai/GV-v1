"""Identifier uniqueness (#166, B7.3).

A unique ID is the strongest matching signal in the system — priority 1 of the eight lanes. A lane that
trusts an identifier which is not actually unique will confidently pair two different cabinets, and every
check downstream then computes exact arithmetic about the wrong item.

The tests are shaped around one distinction: **which duplicates are findings and which are ordinary.** A
check that fires on two base cabinets sharing a catalogue number is a check somebody turns off, and then
the vendor-unique collisions stop being seen either.

Source: `docs/DESIGN_EXTRACTION.md` §4.1 · Verification: this file
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon
from extraction.model.identifiers import (
    UNIQUE_WITHIN,
    DuplicateIdentifier,
    IdentifierNotUnique,
    UniquenessScope,
    check_uniqueness,
    require_unique,
)
from extraction.model.items import DrawingItem, IdentifierKind, PrintedIdentifier, ViewIdentity
from vocabulary.semantic_types import SemanticType

DOC = UUID("11111111-1111-1111-1111-111111111111")
OTHER_DOC = UUID("22222222-2222-2222-2222-222222222222")


def _extent(document: UUID = DOC, offset: str = "0.1") -> Polygon:
    """An extent in the *same* document as the item's view.

    `DrawingItem` refuses a mismatch — *"an item whose geometry came from a different document is not an
    item, and containment against it would be answered confidently and wrongly."* My first helper
    hardcoded one document, so every cross-document test failed at construction. The invariant was right
    and the fixture was wrong.
    """
    base = Decimal(offset)
    return Polygon(
        points=(
            StoredPoint(base, base),
            StoredPoint(base + Decimal("0.2"), base),
            StoredPoint(base + Decimal("0.2"), base + Decimal("0.2")),
            StoredPoint(base, base + Decimal("0.2")),
        ),
        space="stored",
        document_version_id=document,
        page=0,
    )


def _item(
    *identifiers: tuple[IdentifierKind, str],
    document: UUID = DOC,
    tag: str = "D",
    item_id: UUID | None = None,
) -> DrawingItem:
    return DrawingItem(
        view=ViewIdentity(document_version_id=document, page=0, tag=tag),
        item_type=SemanticType.CABINET_WIDTH,
        extent=_extent(document),
        id=item_id or uuid4(),
        identifiers=tuple(PrintedIdentifier(kind, value) for kind, value in identifiers),
    )


# ---------------------------------------------------------------------------
# What this story did not need to build
# ---------------------------------------------------------------------------


def test_the_identifier_types_are_reused_rather_than_redefined() -> None:
    """Three of this story's four criteria were already met by `extraction/model/items.py`.

    Kind is explicit, the printed value is preserved, and `DrawingItem.identifiers` is a tuple so an item
    may carry several. Declaring those again here would be the second-definition problem #164 had to be
    corrected for — so this asserts the reuse rather than trusting a docstring to describe it.
    """
    import ast
    from pathlib import Path

    import extraction.model.identifiers as module

    tree = ast.parse(Path(module.__file__).read_text())
    declared = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}

    for already in ("IdentifierKind", "PrintedIdentifier", "DrawingItem"):
        assert already not in declared, f"{already} is redefined here instead of imported"


def test_an_item_may_carry_several_identifiers_of_different_kinds() -> None:
    """The third criterion, satisfied by the existing type — checked because this module relies on it.

    A cabinet often carries both a vendor code and a mark, and they disagree often enough that keeping
    only one would lose the disagreement.
    """
    item = _item((IdentifierKind.VENDOR_UNIQUE, "V-1"), (IdentifierKind.MARK, "C-1"))
    assert {i.kind for i in item.identifiers} == {IdentifierKind.VENDOR_UNIQUE, IdentifierKind.MARK}
    assert check_uniqueness([item]).checked == 2


# ---------------------------------------------------------------------------
# Vendor unique: unique across the package
# ---------------------------------------------------------------------------


def test_a_repeated_vendor_unique_id_is_reported() -> None:
    """The fourth criterion, on the kind that matters most.

    This is the strongest matching signal in the system. Two items sharing one means the drawing repeats
    it or we misread one — and a matching lane that trusted it would pair two different cabinets.
    """
    first, second = uuid4(), uuid4()
    report = check_uniqueness(
        [
            _item((IdentifierKind.VENDOR_UNIQUE, "V-1"), item_id=first),
            _item((IdentifierKind.VENDOR_UNIQUE, "V-1"), item_id=second),
        ]
    )

    assert not report.is_unique
    assert len(report.duplicates) == 1
    duplicate = report.duplicates[0]
    assert duplicate.value_as_printed == "V-1"
    assert duplicate.scope == UniquenessScope.PACKAGE
    assert set(duplicate.item_ids) == {first, second}, "every item is named, not just the later one"


def test_a_vendor_unique_id_repeated_across_documents_is_still_reported() -> None:
    """Package scope means package. Two drawings in one package may not reuse a unique ID."""
    report = check_uniqueness(
        [
            _item((IdentifierKind.VENDOR_UNIQUE, "V-1"), document=DOC),
            _item((IdentifierKind.VENDOR_UNIQUE, "V-1"), document=OTHER_DOC),
        ]
    )

    assert not report.is_unique
    assert report.duplicates[0].scope_key is None, "a package-wide scope has no document key"


def test_distinct_vendor_unique_ids_are_not_reported() -> None:
    report = check_uniqueness(
        [_item((IdentifierKind.VENDOR_UNIQUE, "V-1")), _item((IdentifierKind.VENDOR_UNIQUE, "V-2"))]
    )
    assert report.is_unique
    assert report.checked == 2


# ---------------------------------------------------------------------------
# Mark: unique per drawing, not per package
# ---------------------------------------------------------------------------


def test_the_same_mark_on_two_drawings_is_not_a_duplicate() -> None:
    """**The distinction quoted from `items.py` rather than invented:** *"a mark is unique to one
    drawing."*

    Two sheets both labelling a cabinet `C-1` is ordinary. Reporting it would bury the collisions that
    matter under noise from the ones that do not.
    """
    report = check_uniqueness(
        [
            _item((IdentifierKind.MARK, "C-1"), document=DOC),
            _item((IdentifierKind.MARK, "C-1"), document=OTHER_DOC),
        ]
    )

    assert report.is_unique


def test_the_same_mark_twice_on_one_drawing_is_reported() -> None:
    """The other side of that scope. One drawing labelling two items `C-1` is a real ambiguity."""
    report = check_uniqueness(
        [
            _item((IdentifierKind.MARK, "C-1"), document=DOC),
            _item((IdentifierKind.MARK, "C-1"), document=DOC, tag="E"),
        ]
    )

    assert not report.is_unique
    assert report.duplicates[0].scope == UniquenessScope.DOCUMENT_VERSION
    assert report.duplicates[0].scope_key == DOC, "the report names which drawing"


# ---------------------------------------------------------------------------
# Catalogue: duplicates are the normal case
# ---------------------------------------------------------------------------


def test_a_repeated_catalogue_code_is_never_reported() -> None:
    """*"a catalogue number is shared by every unit of that model"* — `items.py`.

    Reporting this would be reporting that two base cabinets are the same model, which is not a finding.
    A check that fires on the ordinary case is one somebody turns off, and then the vendor-unique
    collisions stop being seen either.
    """
    report = check_uniqueness(
        [
            _item((IdentifierKind.CATALOGUE, "B24")),
            _item((IdentifierKind.CATALOGUE, "B24")),
            _item((IdentifierKind.CATALOGUE, "B24")),
        ]
    )

    assert report.is_unique
    assert report.checked == 3, "they were examined, not skipped"


def test_every_kind_has_a_stated_expectation() -> None:
    """A kind absent from the table would be silently unchecked.

    An unchecked identifier reads exactly like a unique one, so a new `IdentifierKind` member has to state
    its expectation — this is what makes adding one fail rather than pass quietly.
    """
    assert set(UNIQUE_WITHIN) == set(IdentifierKind)


def test_an_unknown_kind_is_refused_rather_than_skipped() -> None:
    """The programming-error case, since the table is what the check trusts."""
    import extraction.model.identifiers as module

    original = dict(module.UNIQUE_WITHIN)
    module.UNIQUE_WITHIN.pop(IdentifierKind.MARK)
    try:
        with pytest.raises(ValueError, match="no stated uniqueness expectation"):
            check_uniqueness([_item((IdentifierKind.MARK, "C-1"))])
    finally:
        module.UNIQUE_WITHIN.clear()
        module.UNIQUE_WITHIN.update(original)


# ---------------------------------------------------------------------------
# As printed, and the limit that follows from it
# ---------------------------------------------------------------------------


def test_comparison_is_exactly_as_printed() -> None:
    """The second criterion: normalisation *"lives in B5.1 and never overwrites"* the printed value.

    So `C-1` and `c-1` are two identifiers here. That is a real limitation and the docstring says so —
    a duplicate differing only by OCR variance is B9.2's to catch, where the original is still available
    to show a reviewer. This check covers the unambiguous case and does not pretend to cover more.
    """
    report = check_uniqueness(
        [
            _item((IdentifierKind.VENDOR_UNIQUE, "V-1")),
            _item((IdentifierKind.VENDOR_UNIQUE, "v-1")),
        ]
    )

    assert report.is_unique, "case folding here would be normalising, which is another story's"


def test_the_printed_value_is_carried_into_the_report() -> None:
    """A reviewer looks at the sheet, so the report must quote what is on it."""
    report = check_uniqueness(
        [
            _item((IdentifierKind.VENDOR_UNIQUE, "V-1 ")),
            _item((IdentifierKind.VENDOR_UNIQUE, "V-1 ")),
        ]
    )
    assert report.duplicates[0].value_as_printed == "V-1 "


# ---------------------------------------------------------------------------
# Reported, or asserted — two different needs
# ---------------------------------------------------------------------------


def test_check_uniqueness_never_raises_for_a_duplicate() -> None:
    """A report is what a reviewer reads; it must survive the thing it is reporting."""
    report = check_uniqueness(
        [_item((IdentifierKind.VENDOR_UNIQUE, "V-1")), _item((IdentifierKind.VENDOR_UNIQUE, "V-1"))]
    )
    assert len(report.duplicates) == 1


def test_require_unique_raises_and_carries_the_whole_report() -> None:
    """An exception is what stops a matching lane trusting an identifier it should not.

    It carries the report rather than the first duplicate, because a caller that wanted the invariant will
    want to show everything that broke it.
    """
    items = [
        _item((IdentifierKind.VENDOR_UNIQUE, "V-1")),
        _item((IdentifierKind.VENDOR_UNIQUE, "V-1")),
        _item((IdentifierKind.VENDOR_UNIQUE, "V-2")),
        _item((IdentifierKind.VENDOR_UNIQUE, "V-2")),
    ]

    with pytest.raises(IdentifierNotUnique) as raised:
        require_unique(items)

    assert len(raised.value.report.duplicates) == 2, "both collisions, not the first"


def test_require_unique_returns_the_report_when_everything_is_unique() -> None:
    report = require_unique([_item((IdentifierKind.VENDOR_UNIQUE, "V-1"))])
    assert report.is_unique and report.checked == 1


# ---------------------------------------------------------------------------
# The report itself
# ---------------------------------------------------------------------------


def test_an_empty_input_is_distinguishable_from_a_clean_one() -> None:
    """`checked` exists for this: nothing examined and nothing wrong are different facts.

    Without it, a caller passing an empty list would read the same "all unique" as one passing a hundred
    clean items.
    """
    empty = check_uniqueness([])
    clean = check_uniqueness([_item((IdentifierKind.VENDOR_UNIQUE, "V-1"))])

    assert empty.is_unique and clean.is_unique
    assert empty.checked == 0 and clean.checked == 1


def test_items_with_no_identifiers_are_not_a_problem() -> None:
    """Plenty of fillers carry nothing printed, and requiring one would make somebody invent a value."""
    report = check_uniqueness([_item(), _item(), _item()])
    assert report.is_unique and report.checked == 0


def test_the_report_is_ordered_deterministically() -> None:
    """Two runs over the same items produce the same report, whatever order they arrived in."""
    items = [
        _item((IdentifierKind.VENDOR_UNIQUE, "V-2")),
        _item((IdentifierKind.VENDOR_UNIQUE, "V-2")),
        _item((IdentifierKind.VENDOR_UNIQUE, "V-1")),
        _item((IdentifierKind.VENDOR_UNIQUE, "V-1")),
    ]

    forwards = check_uniqueness(items)
    backwards = check_uniqueness(list(reversed(items)))

    assert [d.value_as_printed for d in forwards.duplicates] == ["V-1", "V-2"]
    assert [d.value_as_printed for d in backwards.duplicates] == ["V-1", "V-2"]


def test_a_duplicate_of_one_item_cannot_be_constructed() -> None:
    """One item is not a collision, and reporting it would make the report meaningless."""
    with pytest.raises(ValueError, match="at least two items"):
        DuplicateIdentifier(
            kind=IdentifierKind.VENDOR_UNIQUE,
            value_as_printed="V-1",
            scope=UniquenessScope.PACKAGE,
            scope_key=None,
            item_ids=(uuid4(),),
            detail="only one",
        )


def test_the_detail_says_what_is_wrong_and_declines_to_guess() -> None:
    """A duplicate unique ID means the drawing is wrong or we misread it, and nothing here knows which.

    The detail has to say that, or a reader will assume the first item listed is the real one.
    """
    report = check_uniqueness(
        [_item((IdentifierKind.VENDOR_UNIQUE, "V-1")), _item((IdentifierKind.VENDOR_UNIQUE, "V-1"))]
    )
    detail = report.duplicates[0].detail

    assert "V-1" in detail
    assert "nothing here can tell which" in detail
    assert "every item is listed" in detail


def test_this_module_does_not_reach_the_verdict_engine() -> None:
    """`extraction/` must never import `verdict/` or `rules/` — §2."""
    import ast
    from pathlib import Path

    import extraction.model.identifiers as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("verdict", "rules", "retrieval"):
        assert forbidden not in imported
