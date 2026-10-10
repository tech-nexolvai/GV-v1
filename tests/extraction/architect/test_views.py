"""Which drawing is the architect's: the heading and the content must agree (#1052).

Verification for: `extraction/architect/views.py`. Plain values only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from extraction.architect.views import (
    LabelCounts,
    Role,
    ViewJudgment,
    decide_without_headings,
    drawn_architectural_scale,
    judge_by_document,
    judge_content,
    judge_view,
)
from extraction.panels import PanelRoleProposal


def _counts(
    feet: int = 0,
    vendor: int = 0,
    small: int = 0,
    architectural: tuple[str, ...] = (),
    ratio: tuple[str, ...] = (),
) -> LabelCounts:
    return LabelCounts(feet, vendor, small, architectural, ratio)


ARCH_SCALE = ('1/4" = 1\'-0"',)
RATIO = ("1:10",)


@pytest.mark.parametrize(
    ("counts", "role"),
    [
        (_counts(feet=5, architectural=ARCH_SCALE), Role.ARCH),
        (_counts(feet=1, small=2, architectural=ARCH_SCALE), Role.ARCH),
        (_counts(ratio=RATIO), Role.SHOP),
        (_counts(vendor=4, ratio=RATIO), Role.SHOP),
        (_counts(vendor=2), Role.SHOP),
        # Feet-and-inches labels and no scale: a vendor may reprint the architect's row.
        (_counts(feet=6), None),
        # An architectural scale but no feet-and-inches label, or mostly vendor labels.
        (_counts(architectural=ARCH_SCALE), None),
        (_counts(feet=1, vendor=3, architectural=ARCH_SCALE), None),
        # A ratio scale over feet-and-inches labels contradicts itself.
        (_counts(feet=4, ratio=RATIO), None),
        (_counts(feet=4, architectural=ARCH_SCALE, ratio=RATIO), None),
        (_counts(), None),
        (_counts(small=3), None),
    ],
)
def test_the_content_judgment_decides_only_on_clear_evidence(
    counts: LabelCounts, role: Role | None
) -> None:
    judgment = judge_content(counts)

    assert judgment.role is role
    assert judgment.reason


def _proposal(role: str | None) -> PanelRoleProposal:
    return PanelRoleProposal(
        annotation_index=3,
        role=role,
        heading=None if role is None else "ID SET ELEVATION",
        reason="the label is above" if role else "no label is above this drawing",
    )


def test_agreement_decides_the_role() -> None:
    view = judge_view(_proposal("arch"), judge_content(_counts(feet=3, architectural=ARCH_SCALE)))

    assert view.agreed is Role.ARCH
    assert "agree" in view.reason


@pytest.mark.parametrize(
    ("heading", "counts", "said"),
    [
        (None, _counts(feet=3, architectural=ARCH_SCALE), "heading says nothing"),
        ("arch", _counts(feet=6), "content says nothing"),
        ("shop", _counts(feet=3, architectural=ARCH_SCALE), "heading says shop"),
        ("arch", _counts(ratio=RATIO), "heading says arch"),
    ],
)
def test_a_silent_or_contrary_judgment_decides_nothing(
    heading: str | None, counts: LabelCounts, said: str
) -> None:
    view = judge_view(_proposal(heading), judge_content(counts))

    assert view.agreed is None
    assert said in view.reason


# --- A page with no headings: both drawings' content decides (#1052 fix) --------------------------

AGREEMENT = Decimal("0.04")


@pytest.mark.parametrize(
    ("points_per_inch", "paste", "scale"),
    [
        (Decimal("1.5"), Decimal(1), '1/4" = 1\'-0"'),
        # Drawn a little short of the scale, as a CAD print through a paste is: still 1/4".
        (Decimal("1.1536"), Decimal("0.7888"), '1/4" = 1\'-0"'),
        (Decimal(3), Decimal(1), '1/2" = 1\'-0"'),
        (Decimal("2.25"), Decimal("0.5"), '3/4" = 1\'-0"'),
        # 1.35 pt per inch at 1:1 is 1:53.3 — no architectural scale within 4%.
        (Decimal("1.35"), Decimal(1), None),
        (Decimal("1.5"), None, None),
        (None, Decimal(1), None),
    ],
)
def test_the_scale_a_drawing_is_drawn_at_is_named_only_when_it_is_an_architectural_one(
    points_per_inch: Decimal | None, paste: Decimal | None, scale: str | None
) -> None:
    assert drawn_architectural_scale(points_per_inch, paste, AGREEMENT) == scale


def _headless(index: int, counts: LabelCounts) -> ViewJudgment:
    return judge_view(
        PanelRoleProposal(index, None, None, "no label is above this drawing"),
        judge_content(counts),
    )


def test_with_no_headings_the_content_of_both_drawings_decides_both_roles() -> None:
    views = (
        _headless(0, _counts(feet=3, architectural=ARCH_SCALE)),
        _headless(1, _counts(vendor=4, ratio=RATIO)),
    )

    arch, shop = decide_without_headings(views, {})
    assert arch.agreed is Role.ARCH and arch.by_content_alone
    assert shop.agreed is Role.SHOP and shop.by_content_alone
    assert "content of both drawings" in arch.reason


def test_feet_and_inches_drawn_to_an_architectural_scale_stand_for_a_printed_scale() -> None:
    """The architect's elevation often prints no scale of its own; its labels, drawn to one
    architectural scale, say the same."""
    views = (
        _headless(0, _counts(feet=5)),
        _headless(1, _counts(vendor=4, ratio=RATIO)),
    )

    arch, shop = decide_without_headings(views, {0: '1/4" = 1\'-0"'})
    assert arch.agreed is Role.ARCH
    assert "drawn to" in arch.reason
    assert shop.agreed is Role.SHOP


def test_a_drawing_with_no_labels_and_no_lean_does_not_stop_the_decision() -> None:
    """A floor plan beside the elevation: an architect's scale, no dimension labels. It gets no
    role, and the other two still do."""
    views = (
        _headless(0, _counts(architectural=('3/8" = 1\'-0"',))),
        _headless(1, _counts(feet=3, architectural=ARCH_SCALE)),
        _headless(2, _counts(vendor=4, ratio=RATIO)),
    )

    plan, arch, shop = decide_without_headings(views, {})
    assert plan.agreed is None
    assert arch.agreed is Role.ARCH and shop.agreed is Role.SHOP


@pytest.mark.parametrize(
    ("views", "drawn"),
    [
        # Feet-and-inches labels, no printed scale, and not drawn to an architectural one.
        ((_headless(0, _counts(feet=5)), _headless(1, _counts(vendor=4, ratio=RATIO))), {}),
        # No vendor drawing on the page.
        ((_headless(0, _counts(feet=3, architectural=ARCH_SCALE)), _headless(1, _counts())), {}),
        # Two drawings look like the architect's.
        (
            (
                _headless(0, _counts(feet=3, architectural=ARCH_SCALE)),
                _headless(1, _counts(feet=4, architectural=ARCH_SCALE)),
                _headless(2, _counts(vendor=4, ratio=RATIO)),
            ),
            {},
        ),
        # A third drawing's labels lean to the architect (a vendor reprinting the architect's row).
        (
            (
                _headless(0, _counts(feet=3, architectural=ARCH_SCALE)),
                _headless(1, _counts(feet=6)),
                _headless(2, _counts(vendor=4, ratio=RATIO)),
            ),
            {},
        ),
    ],
)
def test_anything_less_than_clear_on_a_page_without_headings_decides_nothing(
    views: tuple[ViewJudgment, ...], drawn: dict[int, str]
) -> None:
    decided = decide_without_headings(views, drawn)

    assert all(view.agreed is None and not view.by_content_alone for view in decided)
    assert decided == views


def test_a_page_with_a_heading_is_never_decided_by_content_alone() -> None:
    views = (
        judge_view(_proposal("arch"), judge_content(_counts(feet=3))),
        _headless(1, _counts(vendor=4, ratio=RATIO)),
    )

    assert decide_without_headings(views, {}) == views


# --- the architect's own file (#1163) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "heading", "agreed"),
    [
        # The document's kind and the drawing's content agree: the architect's, by code.
        (_counts(feet=5, architectural=ARCH_SCALE), None, Role.ARCH),
        (_counts(feet=5, architectural=ARCH_SCALE), "arch", Role.ARCH),
        # The content is silent or says the vendor's: nothing is decided.
        (_counts(feet=6), None, None),
        (_counts(vendor=4, ratio=RATIO), None, None),
        # A heading saying the vendor's drawing stops it, whatever the content says.
        (_counts(feet=5, architectural=ARCH_SCALE), "shop", None),
    ],
)
def test_on_the_architects_own_file_the_kind_and_the_content_must_agree(
    counts: LabelCounts, heading: str | None, agreed: Role | None
) -> None:
    proposal = (
        None
        if heading is None
        else PanelRoleProposal(
            annotation_index=2, role=heading, heading=f"{heading} heading", reason="printed"
        )
    )

    judgment = judge_by_document(2, judge_content(counts), proposal=proposal)

    assert judgment.agreed is agreed
    assert judgment.by_document_kind is (agreed is not None)
    assert judgment.annotation_index == 2
    assert "uploaded as the architect's drawings" in judgment.reason
