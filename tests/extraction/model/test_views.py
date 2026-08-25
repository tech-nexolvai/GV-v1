"""What a view is, and what makes two views the same one (#164, B7.1).

`docs/DESIGN_EXTRACTION.md` §4.1 gives the failure this file exists to prevent in five words: *"tags
repeat across pages; tag alone merges two views."* A vendor numbering details `D` on every sheet is
ordinary, and a model keyed on the letter fuses a kitchen detail with a bathroom one — after which every
item in both belongs to one view and nothing downstream notices.

The untagged case gets equal weight rather than being a footnote. Most drawings have untagged regions, so
if identity collapses there it collapses in the common case.

Source: `docs/DESIGN_EXTRACTION.md` §4.1 · Verification: this file
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon
from extraction.model.views import (
    DrawingView,
    TagStyle,
    ViewIdentity,
    ViewTag,
    normalise_tag,
    view_containing,
)

DOC = UUID("11111111-1111-1111-1111-111111111111")


def _box(
    left: str, bottom: str, right: str, top: str, *, page: int = 0, document: UUID = DOC
) -> Polygon:
    """A rectangle in stored space (0..1), which is the only space a `Polygon` accepts."""
    return Polygon(
        points=(
            StoredPoint(Decimal(left), Decimal(bottom)),
            StoredPoint(Decimal(right), Decimal(bottom)),
            StoredPoint(Decimal(right), Decimal(top)),
            StoredPoint(Decimal(left), Decimal(top)),
        ),
        space="stored",
        document_version_id=document,
        page=page,
    )


def _view(
    tag: str | None,
    *,
    page: int = 0,
    box: tuple[str, str, str, str] = ("0.1", "0.1", "0.5", "0.5"),
    style: TagStyle | None = None,
    references: str | None = None,
    document: UUID = DOC,
) -> DrawingView:
    return DrawingView(
        region=_box(*box, page=page, document=document),
        tag=ViewTag(tag, style) if tag is not None else None,
        references=ViewTag(references) if references is not None else None,
    )


# ---------------------------------------------------------------------------
# Identity is (page, tag), never tag alone
# ---------------------------------------------------------------------------


def test_the_same_tag_on_two_pages_is_two_views() -> None:
    """The fourth acceptance criterion and §4.1's whole point.

    A vendor labelling a detail `D` on every sheet is ordinary. Keyed on the letter, a kitchen detail and
    a bathroom detail become one view — and then every item in both belongs to it.
    """
    kitchen = _view("D", page=0)
    bathroom = _view("D", page=3)

    assert kitchen.identity != bathroom.identity
    assert kitchen.identity.tag == bathroom.identity.tag == "D", "the tags really are the same"
    assert kitchen.identity.page != bathroom.identity.page


def test_the_same_tag_on_one_page_is_one_view() -> None:
    """The other direction, or the test above would pass on an identity that was always unique."""
    first = _view("D", page=2, box=("0.1", "0.1", "0.4", "0.4"))
    again = _view("D", page=2, box=("0.5", "0.5", "0.9", "0.9"))

    assert (
        first.identity == again.identity
    ), "one tag on one page names one view, wherever it is drawn"


def test_the_same_tag_in_two_documents_is_two_views() -> None:
    """The document version is part of identity too — two packages both have a detail `D`."""
    ours = _view("D", document=DOC)
    theirs = _view("D", document=uuid4())

    assert ours.identity != theirs.identity


def test_identity_compares_the_normalised_tag() -> None:
    """A circled tag read with its bubble is not a different view from the same tag read without it."""
    plain = _view("D")
    bubbled = _view("(D)")

    assert plain.identity == bubbled.identity


# ---------------------------------------------------------------------------
# Untagged regions are views too, and do not collapse
# ---------------------------------------------------------------------------


def test_an_untagged_region_is_representable() -> None:
    """The third acceptance criterion. Most drawings have them, so this is the common case."""
    untagged = _view(None)

    assert not untagged.is_tagged
    assert untagged.tag is None
    assert untagged.identity.tag is None
    assert untagged.identity.region_key is not None


def test_two_untagged_regions_on_one_page_are_two_views() -> None:
    """**The merging failure by its other route.**

    `(page, None)` would make every unlabelled region on a sheet one view. Identity falls back to the
    region because two untagged regions are in two places, and being in two places is what makes them two
    views.
    """
    left = _view(None, box=("0.05", "0.05", "0.45", "0.45"))
    right = _view(None, box=("0.55", "0.55", "0.95", "0.95"))

    assert left.identity != right.identity


def test_the_same_untagged_region_twice_is_one_view() -> None:
    """Read twice from the same page, it is the same view — the region key is stable, not incidental."""
    once = _view(None, box=("0.1", "0.1", "0.5", "0.5"))
    twice = _view(None, box=("0.1", "0.1", "0.5", "0.5"))

    assert once.identity == twice.identity


def test_a_region_key_does_not_depend_on_the_winding_direction() -> None:
    """The same outline drawn clockwise and anticlockwise is one region.

    Two readers tracing the same box in opposite directions must not produce two views.
    """
    clockwise = DrawingView(
        region=Polygon(
            points=(
                StoredPoint(Decimal("0.1"), Decimal("0.1")),
                StoredPoint(Decimal("0.1"), Decimal("0.5")),
                StoredPoint(Decimal("0.5"), Decimal("0.5")),
                StoredPoint(Decimal("0.5"), Decimal("0.1")),
            ),
            space="stored",
            document_version_id=DOC,
            page=0,
        )
    )
    anticlockwise = _view(None, box=("0.1", "0.1", "0.5", "0.5"))

    assert clockwise.identity == anticlockwise.identity


def test_an_untagged_view_and_a_tagged_one_are_never_the_same() -> None:
    """Exactly one of tag and region key is set, so the two kinds cannot compare equal."""
    assert _view(None).identity != _view("D").identity


def test_an_identity_must_use_exactly_one_basis() -> None:
    """Neither would make every view equal; both would make the tag redundant."""
    with pytest.raises(ValueError, match="exactly one"):
        ViewIdentity(DOC, 0)
    with pytest.raises(ValueError, match="exactly one"):
        ViewIdentity(DOC, 0, tag="D", region_key="0.1:0.1")


# ---------------------------------------------------------------------------
# The tag as printed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("printed", ["D", "D1", "4/A-101", "A.2"])
def test_the_tag_is_kept_exactly_as_printed(printed: str) -> None:
    """A report cites what the drawing shows, not our tidied version — as with #183 and #162."""
    assert _view(printed).tag is not None
    assert _view(printed).tag.as_printed == printed  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("printed", "normalised"),
    [("D", "D"), ("(D)", "D"), (" d ", "D"), ("[E]", "E"), ("4/A-101", "4/A-101")],
)
def test_normalisation_strips_decoration_and_keeps_structure(printed: str, normalised: str) -> None:
    """`4/A-101` keeps its slash: the sheet reference is part of which view is meant."""
    assert normalise_tag(printed) == normalised


def test_the_circle_style_is_recorded_when_known() -> None:
    """The first acceptance criterion's second half. The context says the real tags are circled."""
    circled = _view("D", style=TagStyle.CIRCLE)
    assert circled.tag is not None and circled.tag.style is TagStyle.CIRCLE


def test_an_unrecorded_style_is_none_rather_than_a_default() -> None:
    """`None` means we did not look, which is not the same as "not circled".

    Defaulting to `CIRCLE` would turn an absence of observation into a claim about the drawing.

    **Constructed without the argument, not with `style=None`.** My first version went through the `_view`
    helper, which passes `style=None` explicitly — so it overrode the dataclass default and the test
    passed however that default was set. Found by changing the default to `CIRCLE` and watching the suite
    stay green. A test that supplies the value it is checking the default of proves nothing.
    """
    unspecified = ViewTag("D")
    assert (
        unspecified.style is None
    ), "the default style is not None, so a tag nobody looked at claims to be circled"


def test_the_style_vocabulary_holds_only_what_has_been_seen() -> None:
    """Only `CIRCLE`, because that is the one style this project has evidence for (#274).

    A `HEXAGON` member nothing has ever produced is a name in a closed vocabulary pretending to be a
    finding.
    """
    assert {member.name for member in TagStyle} == {"CIRCLE"}


def test_an_empty_tag_is_refused() -> None:
    """An empty tag would make an untagged region look tagged, collapsing the distinction above."""
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="as_printed"):
            ViewTag(blank)


def test_a_referenced_view_is_recorded_but_not_resolved() -> None:
    """*"the view it references where the drawing states one"* — recorded as a tag, not as a link.

    Turning it into a link between views is grouping, which is B7.3's (#166).
    """
    detail = _view("D", references="4/A-101")
    assert detail.references is not None
    assert detail.references.as_printed == "4/A-101"


# ---------------------------------------------------------------------------
# Which view is this observation in?
# ---------------------------------------------------------------------------


def test_an_observation_inside_one_view_resolves_to_it() -> None:
    """The second acceptance criterion: answerable geometrically."""
    plan = _view("A", box=("0.0", "0.0", "0.6", "0.6"))
    elsewhere = _view("B", box=("0.7", "0.7", "1.0", "1.0"))

    match = view_containing(_box("0.1", "0.1", "0.2", "0.2"), [plan, elsewhere])

    assert match.is_resolved
    assert match.view is plan
    assert "contained by A" in match.reason


def test_an_observation_in_no_view_resolves_to_nothing() -> None:
    """Not to the nearest one. An observation outside every view has no view."""
    match = view_containing(
        _box("0.8", "0.8", "0.9", "0.9"), [_view("A", box=("0.0", "0.0", "0.5", "0.5"))]
    )

    assert not match.is_resolved
    assert "not placed in the nearest view" in match.reason


def test_nested_views_containing_one_observation_are_refused() -> None:
    """**The case that is normal rather than exotic.**

    A detail region inside a plan region is ordinary draughting, so two views containing one observation
    is ordinary too. Preferring the smaller is a plausible rule that needs real drawings to justify — and
    which view an observation belongs to decides which *item* it belongs to, which §3.2 establishes no
    tolerance check can catch.
    """
    plan = _view("A", box=("0.0", "0.0", "0.9", "0.9"))
    detail = _view("D", box=("0.1", "0.1", "0.4", "0.4"))

    match = view_containing(_box("0.2", "0.2", "0.3", "0.3"), [plan, detail])

    assert not match.is_resolved
    assert "2 views" in match.reason
    assert "A" in match.reason and "D" in match.reason, "both candidates are named for the reviewer"


def test_views_on_other_pages_are_skipped_not_compared() -> None:
    """`Polygon.contains` raises across pages, and a caller passing a package's views wants an answer.

    Skipping rather than raising is what lets this be called with everything and still work; the page
    check is what keeps it from being a cross-page comparison.
    """
    match = view_containing(
        _box("0.1", "0.1", "0.2", "0.2", page=1),
        [_view("A", page=0, box=("0.0", "0.0", "0.9", "0.9")), _view("B", page=1)],
    )

    assert match.view is not None
    assert match.view.page == 1


def test_a_view_in_another_document_is_not_a_candidate() -> None:
    """Same page number in a different document version is a different drawing entirely."""
    match = view_containing(
        _box("0.2", "0.2", "0.3", "0.3"),
        [_view("A", box=("0.0", "0.0", "0.9", "0.9"), document=uuid4())],
    )

    assert not match.is_resolved


def test_an_untagged_view_can_win_and_is_named_as_untagged() -> None:
    """A reviewer reading the reason needs to know it was an unlabelled region, not a tag we lost."""
    match = view_containing(
        _box("0.2", "0.2", "0.3", "0.3"), [_view(None, box=("0.0", "0.0", "0.9", "0.9"))]
    )

    assert match.is_resolved
    assert "untagged region" in match.reason


def test_every_answer_explains_itself() -> None:
    """Resolved or not. A bare `None` leaves a reviewer unable to tell "outside" from "ambiguous".

    **Asserted semantically, not by length.** My first version required more than 25 characters and failed
    on `"contained by A on page 0"` — 24 characters and a perfectly good reason. A length threshold is a
    test of prose volume; what matters is that a refusal distinguishes itself from the other refusal, so
    each case checks for the words that make it identifiable.
    """
    plan = _view("A", box=("0.0", "0.0", "0.9", "0.9"))
    detail = _view("D", box=("0.1", "0.1", "0.4", "0.4"))

    inside = view_containing(_box("0.2", "0.2", "0.3", "0.3"), [plan])
    assert "contained by A" in inside.reason

    ambiguous = view_containing(_box("0.2", "0.2", "0.3", "0.3"), [plan, detail])
    assert "2 views" in ambiguous.reason and "nearest" not in ambiguous.reason

    outside = view_containing(_box("0.95", "0.95", "0.99", "0.99"), [plan])
    assert "no view" in outside.reason and "nearest view" in outside.reason

    nothing = view_containing(_box("0.2", "0.2", "0.3", "0.3"), [])
    assert "no view" in nothing.reason

    # The two refusals must not read the same, or a reviewer cannot tell them apart.
    assert outside.reason != ambiguous.reason
    for match in (inside, ambiguous, outside, nothing):
        assert match.reason.strip(), "an answer with no reason at all"


def test_the_view_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    view = _view("D")
    with pytest.raises(FrozenInstanceError):
        view.tag = ViewTag("E")  # type: ignore[misc]


def test_this_module_does_not_reach_the_verdict_engine() -> None:
    """`extraction/` must never import `verdict/` or `rules/` — §2."""
    import ast
    from pathlib import Path

    import extraction.model.views as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("verdict", "rules", "retrieval"):
        assert forbidden not in imported, f"extraction/model/views.py imports {forbidden}"
