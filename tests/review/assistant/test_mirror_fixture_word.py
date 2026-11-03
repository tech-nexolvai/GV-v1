"""The word MIRROR on its own is the bathroom mirror, never a note that the drawing is flipped.

An architect's bath sheet prints "MIRROR" over the mirror above the vanity (with its centre line).
Code read that word as "drawn opposite-hand", so the vendor's drawing paired with that sheet had
its positions from an end left out of the check. Now:

- a fixture label ("MIRROR", "LED MIRROR", "MIRROR ABOVE", "MIRROR BY OWNER", "FRAMED MIRROR",
  "MIRROR 30x36", a furniture tag "X-1 (MIRROR)", a mirror tag "MR-1") names no hand;
- a real note still does: MIRRORED, MIRROR IMAGE, MIRROR OF, MIRROR HAND, MIRROR UNITS, OPP. HAND,
  OPPOSITE HAND, REVERSE(D), and MIRROR closing a unit or type's name or a view's kind
  ("UNIT B (MIRROR)", "TYPE 2 - MIRROR", "ELEVATION (MIRROR)");
- the set index's hand word "MIRROR" (the model reading the fixture as a hand) is left out the
  same way, its real notes kept.

Made-up text only: $0, no model call (the set index and the page matcher are fake answers).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant.simple_check.pairing import (
    Facts,
    fixture_mirror,
    markers_in,
    opposite_hand,
)
from app.review.assistant.simple_check.worklist import _marked_words, worklist_from
from tests.review.assistant.test_cross_file_projects import _entry, _label, _phrase, _read
from tests.review.assistant.test_pair_by_index_titles import _across, _matched

SHOP, ARCH = "shop.pdf", "arch.pdf"

FIXTURE_LABELS = [
    "MIRROR",
    "Mirror",
    "MIRROR ABOVE",
    "MIRROR BY OWNER",
    "MIRROR BY OTHERS",
    "FRAMED MIRROR",
    "LED MIRROR",
    'LIGHTED MIRROR @ 48" AFF',
    'MIRROR 30"x36"',
    "MIRROR 30x36",
    "MR-1",
    "X-123 (MIRROR)",
    "X-124 (FULL MIRROR)",
    "FULL LENGTH MIRROR.",
    "GUEST BATH MIRROR",
    "SAMPLE VANITY MIRROR",
    "BOTTOM OF MIRROR",
    "C/L OF MIRROR",
    "MIRRORS",
    "CUBBIES, MIRRORS, HOOKS",
    "MIRROR, SEE SPEC",
    "SECTION - MIRROR CLIP DETAIL",
    "TYPE OF MIRROR TBD",
    "TYPE 2 MIRROR ABOVE VANITY",
    "MIRROR TYPE M1",
    "REV.DATE",
    "REV",
]

GENUINE_NOTES = {
    "MIRROR IMAGE": "MIRROR IMAGE",
    "mirror image of unit A": "MIRROR IMAGE",
    "MIRROR-IMAGE": "MIRROR-IMAGE",
    "MIRRORED": "MIRRORED",
    "DRAWN MIRRORED": "MIRRORED",
    "SHOWN MIRRORED FROM 2/X-100": "MIRRORED",
    "MIRROR OF UNIT A": "MIRROR OF",
    "MIRROR HAND": "MIRROR HAND",
    "MIRROR UNITS": "MIRROR UNITS",
    "OPP. HAND": "OPP. HAND",
    "OPP HAND": "OPP HAND",
    "OPPOSITE HAND": "OPPOSITE HAND",
    "OPPOSITE HANDED": "OPPOSITE HANDED",
    "REVERSE": "REVERSE",
    "REVERSED": "REVERSED",
    "REVERSE HAND": "REVERSE HAND",
    "SAMPLE VANITY REVERSED UNITS": "REVERSED",
    "UNIT B (MIRROR)": "MIRROR",
    "TYPE 2 - MIRROR": "MIRROR",
    "TYPE A1 – MIRROR": "MIRROR",
    "SAMPLE VANITY TYPE-1(MIRROR)": "MIRROR",
    "TYPE 1 MIRROR": "MIRROR",
    "KITCHEN ELEVATION (MIRROR)": "MIRROR",
}


@pytest.mark.parametrize("text", FIXTURE_LABELS)
def test_a_mirror_label_names_no_hand(text: str) -> None:
    assert markers_in([text])[1] == frozenset(), text


@pytest.mark.parametrize(("text", "word"), sorted(GENUINE_NOTES.items()))
def test_a_real_opposite_hand_note_is_still_read(text: str, word: str) -> None:
    assert markers_in([text])[1] == {word}, text


def test_a_sheet_with_both_keeps_the_real_note() -> None:
    hand = markers_in(["MIRROR", "LED MIRROR", "OPP. HAND", "MIRROR ABOVE"])[1]
    assert hand == {"OPP. HAND"}
    # The accessible marker is read as before, beside them.
    assert markers_in(["ADA", "MIRROR"]) == (frozenset({"ADA"}), frozenset())


def test_a_mirror_label_never_flags_the_pair() -> None:
    labels = Facts("a1", hand=markers_in(["MIRROR", "C L", "LED MIRROR"])[1])
    assert opposite_hand(Facts("p1.v0"), labels) is None
    noted = Facts("a1", hand=markers_in(["MIRROR", "MIRROR IMAGE"])[1])
    flagged = opposite_hand(Facts("p1.v0"), noted)
    assert flagged is not None and "MIRROR IMAGE" in flagged and "opposite-hand" in flagged


def test_the_set_index_hand_word_mirror_is_left_out() -> None:
    assert fixture_mirror("MIRROR") and fixture_mirror("led mirror")
    assert fixture_mirror("X-123 (MIRROR)")
    for kept in ("MIRROR IMAGE", "MIRRORED", "UNIT B (MIRROR)", "Reverse", "OPP HAND", "Right"):
        assert not fixture_mirror(kept), kept
    read = {
        "variant_words": ["ADA"],
        "hand_words": [
            "MIRROR",
            "Mirror",
            "LED MIRROR",
            "MIRROR IMAGE",
            "Reverse",
            "Type-1(Right)",
        ],
    }
    assert _marked_words(read) == (
        frozenset({"ADA"}),
        frozenset({"MIRROR IMAGE", "REVERSE", "TYPE-1(RIGHT)"}),
    )
    assert _marked_words({"hand_words": ["MIRROR"]}) == (frozenset(), frozenset())


# ---- the work list ---------------------------------------------------------------------------------


def _sizes(side: str) -> list[dict[str, Any]]:
    return [
        _label(1, '63"', 63, [300, 200, 320, 210], side, "h"),
        _label(2, '22"', 22, [130, 300, 140, 320], side, "v"),
        _label(3, '34"', 34, [500, 300, 510, 320], side, "v"),
    ]


def _pair(printed: str, hand_words: list[str]) -> Any:
    """Vendor page 1 "SAMPLE VANITY"; architect page 1 the same vanity printing `printed` over its
    mirror, the set index reading `hand_words` on it; the matcher sure of it."""
    vendor = {1: "SAMPLE VANITY"}
    architect = {1: "SAMPLE VANITY", 2: "SAMPLE BENCH", 3: "SAMPLE WARDROBE"}
    pages = [_entry(SHOP, p, "vendor") for p in vendor] + [
        _entry(ARCH, p, "architect") for p in architect
    ]
    dims = {
        "pages": [
            *(
                {
                    "file": SHOP,
                    "page": p,
                    "labels": _sizes("vendor"),
                    "phrases": [_phrase(t, [700, 700, 900, 710])],
                }
                for p, t in vendor.items()
            ),
            *(
                {
                    "file": ARCH,
                    "page": p,
                    "labels": _sizes("architect"),
                    "phrases": [
                        _phrase(t, [700, 700, 900, 710]),
                        *([_phrase(printed, [300, 150, 340, 160])] if p == 1 else []),
                    ],
                }
                for p, t in architect.items()
            ),
        ]
    }
    index = {
        "version": 1,
        "pages": {
            **{f"{SHOP}#{p}": _read(SHOP, p, item=t) for p, t in vendor.items()},
            **{
                f"{ARCH}#{p}": _read(ARCH, p, item=t, hand_words=hand_words if p == 1 else [])
                for p, t in architect.items()
            },
        },
    }

    def work(matches: dict[str, Any] | None) -> Any:
        return worklist_from(
            {
                "files": [SHOP, ARCH],
                "upload_roles": {SHOP: "vendor", ARCH: "architect"},
                "pages": pages,
            },
            dims=dims,
            set_index=index,
            matches=matches,
            main_file=SHOP,
        )

    return _across(work(_matched(work(None), {1: 1})), 1)


def test_a_bath_sheet_printing_mirror_is_compared_with_its_positions() -> None:
    # The architect's sheet prints MIRROR over the mirror, and the set index read it as a hand.
    pair = _pair("MIRROR", ["MIRROR"])
    assert pair.chosen, pair.why
    assert pair.opposite_hand is None, pair.opposite_hand


def test_a_sheet_noted_mirror_image_is_still_flagged() -> None:
    pair = _pair("MIRROR IMAGE", ["MIRROR IMAGE"])
    assert pair.chosen, pair.why
    assert pair.opposite_hand is not None and "MIRROR IMAGE" in pair.opposite_hand
    # Printed only (the set index did not read it): code's own words still flag it.
    printed_only = _pair("UNIT B (MIRROR)", [])
    assert printed_only.chosen, printed_only.why
    assert printed_only.opposite_hand is not None
