"""Which drawing is the architect's: the heading and the content must agree (#1052).

Verification for: `extraction/architect/views.py`. Plain values only.
"""

from __future__ import annotations

import pytest

from extraction.architect.views import LabelCounts, Role, judge_content, judge_view
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
