"""The resolve loop's wall ($0): every known false or risky red stays out of red with the loop on.

The simple full check's resolve loop (`simple_check/resolve.py`, issue #1735) lets the AI fill a gap
code named (a value not read, ends code cannot tell apart, an older round, the AI unsure) and code
verifies again. A loop that keeps proposing until something passes will find the gaps in the checks
(RESOLVE-LOOP-RESEARCH.md §1a), so this wall was written BEFORE the loop:

- the 78 forms of the known false or risky reds of the eval kit (`false_reds.py`), as synthetic
  equivalents: made-up drawings and values, the same shape of mistake (AI notes and strings, GV's
  markup taken for the architect's, the brand prototype, different reference points, a run to a
  bulkhead or another trade's panel, room-context absences, a drawn digit slip, tags, summed
  architect strings with breaks in them);
- the 34 hard "no" cases never enter the loop, nor the 24 listed ones, nor the 3 red candidates
  that wait for the blind look;
- each of the 17 cases a naive loop would let in is stopped by its named guard;
- the worst proposal a model could make, in every kind the loop knows (and kinds it does not
  allow), on every round, never turns any of them red, and never makes a new red candidate.

No client values: every number and word below is made up. No PDF, no model call.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import pytest

from app.review.assistant.simple_check import resolve
from app.review.assistant.simple_check.verify import Item, Pack, Verified, verify_answer

FILE = "set.pdf"
ARCH = [0.0, 0.0, 500.0, 400.0]
VEND = [0.0, 420.0, 500.0, 800.0]
WHOLE = [0.0, 0.0, 500.0, 800.0]
BLACK = (120, 0.0)


# ---- the synthetic set -----------------------------------------------------------------------


def _label(
    lid: str,
    text: str,
    inches: str | None,
    box: Sequence[float],
    side: str,
    orientation: str = "h",
    line: Sequence[Sequence[float]] | None = None,
    **more: Any,
) -> dict[str, Any]:
    extension = None
    if line is not None:
        (x0, y0), (x1, y1) = line
        if orientation == "h":
            extension = [[[x0, y0 - 40], [x0, y0]], [[x1, y1 - 40], [x1, y1]]]
        else:
            extension = [[[x0 - 40, y0], [x0, y0]], [[x1 - 40, y1], [x1, y1]]]
    return {
        "id": lid,
        "text": text,
        "inches": inches,
        "box": list(box),
        "side": side,
        "orientation": orientation,
        "route": more.pop("route", "text"),
        "mm": None,
        "units": None,
        "dim_line": [list(p) for p in line] if line is not None else None,
        "extension": extension,
        "span_pt": None,
        "scale_ok": True,
        **more,
    }


def _phrase(text: str, box: Sequence[float], side: str = "vendor") -> dict[str, Any]:
    return {"text": text, "box": list(box), "side": side}


def _vendor_words(y: float = 700.0) -> list[dict[str, Any]]:
    return [
        _phrase("BASE CABINET", (20, y, 90, y + 10)),
        _phrase("FINISHED END PANEL", (120, y, 200, y + 10)),
        _phrase("SHELF PIN HOLES", (220, y, 300, y + 10)),
    ]


def _hstring(prefix: int, y: float, parts: Sequence[tuple[float, float, int, float]]) -> list[dict[str, Any]]:
    """An architect string of horizontal dimensions: (start, end, inches, across) each."""
    out = []
    for n, (x0, x1, inches, across) in enumerate(parts):
        text = f"{inches // 12}' - {inches % 12}\"" if inches >= 12 else f'{inches}"'
        out.append(
            _label(
                f"D{prefix + n}",
                text,
                str(inches),
                (x0 + 2, across - 12, x1 - 2, across - 2),
                "architect",
                "h",
                ((x0, across), (x1, across)),
            )
        )
    return out


def _documents() -> dict[str, Any]:
    pages: dict[int, dict[str, Any]] = {}
    halves: dict[int, list[dict[str, Any]]] = {}
    areas: dict[int, tuple[str, int | None]] = {}

    def page(n: int, area: str, start: int | None, labels: list[dict[str, Any]], words: list[Any],
             parts: list[dict[str, Any]]) -> None:
        pages[n] = {"file": FILE, "page": n, "labels": labels, "phrases": words}
        halves[n] = parts
        areas[n] = (area, start)

    arch = {"box": ARCH, "role": "architect", "round": None}

    def vendor(round_: int, box: list[float] = VEND) -> dict[str, Any]:
        return {"box": box, "role": "vendor", "round": round_}

    # Page 1, "1. Pantry": the architect's elevation over the vendor's round 1. Different reference
    # points (a splash band, a top's thickness, a run to another trade's panel), a drawn digit slip,
    # a wall-to-wall run against a centre line (with its two equal parts).
    page(
        1,
        "1. Pantry",
        None,
        [
            _label("D1", "7' - 4\"", "88", (20, 100, 30, 130), "architect", "v"),
            _label("D2", "1' - 6\"", "18", (40, 100, 50, 130), "architect", "v"),
            _label("D3", "2' - 10\"", "34", (60, 100, 70, 130), "architect", "v"),
            _label("D4", "4' - 8\"", "56", (80, 100, 90, 130), "architect", "v"),
            _label("D5", "2' - 2\"", "26", (100, 100, 130, 110), "architect", "h"),
            _label("D6", '50 1/4"', "50.25", (20, 500, 30, 530), "vendor", "v"),
            _label("D7", "381 [15]", "15", (40, 500, 50, 530), "vendor", "v", ((52, 480), (52, 560))),
            _label("D8", "76 [3]", "3", (40, 561, 50, 575), "vendor", "v", ((52, 560), (52, 576))),
            _label("D9", "838 [33]", "33", (60, 500, 70, 530), "vendor", "v", ((72, 470), (72, 600))),
            _label("D10", "25 [1]", "1", (60, 601, 70, 612), "vendor", "v", ((72, 600), (72, 604))),
            _label("D11", '58"', "58", (80, 500, 90, 530), "vendor", "v", route="drawn"),
            _label("D12", "1016 [40]", "40", (100, 500, 130, 510), "vendor", "h", ((100, 515), (300, 515))),
            _label(
                "D13", "EQ", "20", (150, 520, 160, 528), "vendor", "h", ((100, 530), (200, 530)),
                derived={"rule": "equal_parts", "from": "D12", "parts": 2, "less": []},
            ),
            _label(
                "D14", "EQ", "20", (250, 520, 260, 528), "vendor", "h", ((200, 530), (300, 530)),
                derived={"rule": "equal_parts", "from": "D12", "parts": 2, "less": []},
            ),
        ],
        _vendor_words(),
        [arch, vendor(1)],
    )
    # Pages 2-7: three areas, each the architect's elevation with the vendor's round 1, then the
    # vendor's round 2 alone: the architect's height runs to a bulkhead box above the unit.
    for first, name in ((2, "2. Lounge"), (4, "3. Bar"), (6, "4. Office")):
        page(
            first,
            name,
            None,
            [
                _label("D1", "9' - 1\"", "109", (20, 100, 30, 130), "architect", "v"),
                _label("D2", '91 1/4"', "91.25", (20, 500, 30, 530), "vendor", "v"),
                _label("D3", '24"', "24", (100, 500, 130, 510), "vendor", "h"),
            ],
            _vendor_words(),
            [arch, vendor(1)],
        )
        page(
            first + 1,
            name,
            first,
            [
                _label("D1", '91 1/4"', "91.25", (20, 100, 30, 130), "vendor", "v"),
                _label("D2", '24"', "24", (100, 100, 130, 110), "vendor", "h"),
            ],
            _vendor_words(300),
            [vendor(2, WHOLE)],
        )
    # Page 8, "5. Lobby": the architect prints room context (ceiling, soffit, door head, artwork,
    # switches, a column, wall outlets) and one outlet over the counter; the vendor's sheet has
    # pen strokes only (no readable text layer).
    room = [
        ("D1", "9' - 2\"", "110"),
        ("D2", "8' - 10\"", "106"),
        ("D3", "9' - 3\"", "111"),
        ("D4", "7' - 1\"", "85"),
        ("D5", "5' - 1\"", "61"),
        ("D6", "4' - 1\"", "49"),
        ("D7", "6' - 1\"", "73"),
        ("D8", "3' - 2\"", "38"),
        ("D9", "1' - 3\"", "15"),
        ("D10", "3' - 5\"", "41"),
        ("D11", "1' - 4\"", "16"),
        ("D12", "1' - 10\"", "22"),
    ]
    page(
        8,
        "5. Lobby",
        None,
        [
            *(
                _label(lid, text, inches, (20 + 30 * n, 100, 45 + 30 * n, 110), "architect", "h")
                for n, (lid, text, inches) in enumerate(room)
            ),
            _label("D13", '32"', "32", (20, 500, 45, 510), "vendor", "h", route="drawn"),
            _label("D14", '18"', "18", (60, 500, 85, 510), "vendor", "h", route="drawn"),
            _label("D15", '12"', "12", (100, 500, 125, 510), "vendor", "h", route="drawn"),
        ],
        [],
        [arch, vendor(1)],
    )
    # Page 9, "6. Corridor": GV's own yellow notes sit on the vendor's drawing (never the design).
    page(
        9,
        "6. Corridor",
        None,
        [
            _label("D1", "5' - 0\"", "60", (100, 100, 130, 110), "architect", "h"),
            _label("D2", "2750 [108 1/4]", "108.25", (100, 500, 140, 510), "vendor", "h"),
            _label("D3", "3020 [118 7/8]", "118.875", (100, 540, 140, 550), "vendor", "h"),
            _label("D4", '52 1/4"', "52.25", (100, 580, 130, 590), "vendor", "h"),
            _label("D5", "1070 [42 1/8]", "42.125", (100, 620, 140, 630), "vendor", "h"),
        ],
        _vendor_words(),
        [arch, vendor(1)],
    )
    # Pages 10-11, "7. Retail": the vendor's drawing, then the brand prototype (reference only).
    page(
        10,
        "7. Retail",
        None,
        [
            _label("D1", "3' - 4\"", "40", (100, 100, 130, 110), "architect", "h"),
            _label("D2", '43"', "43", (20, 500, 30, 530), "vendor", "v"),
        ],
        _vendor_words(),
        [arch, vendor(1)],
    )
    page(
        11,
        "7. Retail",
        10,
        [_label("D1", "3' - 3\"", "39", (20, 100, 30, 130), "brand", "v")],
        [],
        [{"box": ARCH, "role": "brand_prototype", "round": None}],
    )
    # Page 12, "8. Kitchen": the architect prints only an outlet string with a break in it (a short
    # dimension 3 points off the others and a little apart): no overall.
    page(
        12,
        "8. Kitchen",
        None,
        [
            *_hstring(
                1,
                300.0,
                [
                    (100.0, 130.0, 10, 300.0),
                    (130.0, 170.0, 16, 300.0),
                    (173.0, 185.0, 6, 298.0),
                    (188.0, 218.0, 12, 300.0),
                    (218.0, 263.0, 18, 300.0),
                    (263.0, 323.0, 24, 300.0),
                    (323.0, 360.0, 15, 300.0),
                ],
            ),
            _label("D8", "2540 [100]", "100", (180, 500, 220, 510), "vendor", "h", ((100, 515), (360, 515))),
            _label("D9", '36"', "36", (20, 600, 30, 630), "vendor", "v"),
            _label("D10", '89 1/2"', "89.5", (40, 600, 50, 630), "vendor", "v"),
        ],
        _vendor_words(),
        [arch, vendor(1)],
    )
    # Page 13, "9. Pantry 2": an architect string whose parts sit on two lines, every other one
    # 2.5 points lower.
    page(
        13,
        "9. Pantry 2",
        None,
        [
            *_hstring(
                1,
                305.0,
                [
                    (100.0, 110.0, 8, 302.5),
                    (114.0, 152.0, 17, 305.0),
                    (156.0, 166.0, 8, 302.5),
                    (170.0, 208.0, 17, 305.0),
                    (209.0, 219.0, 4, 302.5),
                    (219.0, 307.0, 40, 305.0),
                    (307.0, 333.0, 11, 305.0),
                ],
            ),
            _label("D8", "2990 [117 3/4]", "117.75", (180, 500, 220, 510), "vendor", "h", ((100, 515), (333, 515))),
            _label("D9", '30"', "30", (20, 600, 30, 630), "vendor", "v"),
        ],
        _vendor_words(),
        [arch, vendor(1)],
    )
    return {
        "dims": {"files": [FILE], "pages": [pages[n] for n in sorted(pages)]},
        "page_map": {
            "pages": [
                {
                    "file": FILE,
                    "page": n,
                    "area": areas[n][0],
                    "area_from_page": areas[n][1],
                    "halves": halves[n],
                }
                for n in sorted(pages)
            ]
        },
        "pairs": {"pairs": []},
        "page_texts": {"pages": []},
    }


PACK = Pack.from_documents(main_file=FILE, **_documents())


# ---- the 78 fixtures ---------------------------------------------------------------------------


def _pair(v: str, a: str | None, verdict: str, measures: str, ends: Sequence[str]) -> dict[str, Any]:
    return {
        "vendor_label": v,
        "architect_label": a,
        "measures": measures,
        "vendor_from": ends[0],
        "vendor_to": ends[1],
        "architect_from": ends[2] if len(ends) > 2 else "",
        "architect_to": ends[3] if len(ends) > 2 else "",
        "verdict": verdict,
        "note": "",
    }


def _other(kind: str, what: str, vendor: str | None, architect: str | None, vbox: Any, abox: Any,
           apage: int | None, verdict: str = "different", millwork: bool = True) -> dict[str, Any]:
    return {
        "kind": kind,
        "what": what,
        "vendor": vendor,
        "architect": architect,
        "vendor_box": list(vbox) if vbox is not None else list(VEND),
        "architect_box": list(abox) if abox is not None else None,
        "architect_page": apage,
        "verdict": verdict,
        "millwork": millwork,
    }


@dataclass(frozen=True)
class Fixture:
    id: str
    work_item: str
    group: str
    """The research's group (RESOLVE-LOOP-RESEARCH.md §8): `no` (a hard no from code, 34),
    `listed` (24), `blind` (3, the blind look decides) or `gap` (17, a naive loop lets them in)."""
    guard: str
    """The named guard that keeps it out: a `Refusal.guard`, or `loop:<why>` for the two the loop
    takes in and its checks stop."""
    item: Mapping[str, Any]
    extra: bool = False
    """Not one of the 78: a further form the loop must hold too (an `unsure` answer)."""

    @property
    def kind(self) -> str:
        return "pair" if "vendor_label" in self.item else "other"


FIXTURES: list[Fixture] = []


def fx(fid: str, work: str, group: str, guard: str, item: dict[str, Any], extra: bool = False) -> None:
    FIXTURES.append(Fixture(fid, work, group, guard, item, extra))


# AI-assembled strings and notes (F107, F115, F101, F103, s3-63 forms).
fx("F107-note", "p12.v1", "gap", "kind:note",
   _other("note", "vendor height string 10 + 46 + 5 = 61 does not add up to the printed 89 1/2", '89 1/2"', '61"', None, None, None))
fx("F107-pair", "p12.v1", "gap", "no_fixed_pair",
   _pair("p12.D10", None, "different", "overall height string", ("floor", "top")))
fx("F115-note", "p13.v1", "gap", "kind:note",
   _other("note", "vendor height string 9 + 74 = 83 does not add up to the printed 89 1/2", '89 1/2"', '83"', None, None, None))
fx("F115-pair", "p13.v1", "gap", "no_fixed_pair",
   _pair("p13.D9", None, "different", "overall height string", ("floor", "top")))
fx("F101-nolabel", "p12.v1", "gap", "unreadable_side",
   _other("dimension_without_label", "overalls with fillers vs without: fillers exceed GV's limit", '117" / 110 1/2"', None, None, None, None))
fx("F101-rule-note", "p12.v1", "gap", "kind:note",
   _other("note", "fillers wider than GV's filler rule", '3 1/4" each end', '1" to 2" (GV rule)', None, None, None))
fx("F103-pair", "p9.v1", "gap", "no_fixed_pair",
   _pair("p9.D4", None, "different", "filler width", ("cabinet side", "wall")))
fx("F103-note", "p9.v1", "gap", "kind:note",
   _other("note", "filler widths: GV notes 3 1/2, vendor labels 1 1/2", '1 1/2"', '3 1/2" (GV note)', None, None, None))


def ref_pair(fid: str, work: str, v: str, a: str, measures: str, v_ends: tuple[str, str],
             a_ends: tuple[str, str], guards: tuple[str, str, str], worst_group: str) -> None:
    fx(f"{fid}-honest", work, "no", guards[0], _pair(v, a, "different_points", measures, (*v_ends, *a_ends)))
    fx(f"{fid}-ends", work, "no", guards[1], _pair(v, a, "different", measures, (*v_ends, *a_ends)))
    fx(f"{fid}-worst", work, worst_group, guards[2], _pair(v, a, "different", measures, (*a_ends, *a_ends)))
    # Not in the 78: the same pair answered "unsure", with the real ends and with the worst ones.
    fx(f"{fid}-unsure", work, "no", guards[1], _pair(v, a, "unsure", measures, (*v_ends, *a_ends)), extra=True)


# Different reference points (F16, s3-36, s3-47, s3-58, F29, s3-60 forms).
ref_pair("F16", "p1.v1", "p1.D6", "p1.D1", "overall height", ("floor", "top of base unit"),
         ("floor", "top of wall covering panel"), ("ai_no", "no:trade", "no:trade"), "no")
ref_pair("s3-36", "p1.v1", "p1.D7", "p1.D2", "countertop to underside of upper cabinets",
         ("top of splash band", "underside of uppers"), ("top of countertop", "underside of uppers"),
         ("ai_no", "no:ends", "candidate"), "blind")
ref_pair("s3-47", "p1.v1", "p1.D9", "p1.D3", "floor to vanity top", ("floor", "underside of top"),
         ("floor", "top of counter"), ("ai_no", "no:ends", "candidate"), "blind")
ref_pair("s3-58", "p1.v1", "p1.D11", "p1.D4", "floor to countertop", ("floor", "top of splash"),
         ("floor", "top of countertop"), ("ai_no", "no:slip", "no:slip"), "no")
ref_pair("F29", "p1.v1", "p1.D12", "p1.D5", "sink centre line to right wall", ("left wall", "right wall"),
         ("sink centre line", "right wall"), ("ai_no", "no:ends", "candidate"), "blind")
for first in (2, 4, 6):
    for page, work, label in ((first, f"p{first}.v1", f"p{first}.D2"), (first + 1, f"p{first + 1}.v2", f"p{first + 1}.D1")):
        ref_pair(f"s3-60-p{page}", work, label, f"p{first}.D1", "overall height", ("floor", "top of unit"),
                 ("floor", "top of bulkhead box"), ("ai_no", "no:trade", "no:trade"), "no")

# The unsure forms of the three the blind look decides, with the worst ends (not in the 78): the
# loop may take them in (the AI's doubt is a gap), but never past the blind look.
for fid, v, a, measures, ends in (
    ("s3-36", "p1.D7", "p1.D2", "countertop to underside of upper cabinets", ("top of countertop", "underside of uppers")),
    ("s3-47", "p1.D9", "p1.D3", "floor to vanity top", ("floor", "top of counter")),
    ("F29", "p1.D12", "p1.D5", "sink centre line to right wall", ("sink centre line", "right wall")),
):
    fx(f"{fid}-unsure-worst", "p1.v1", "blind", "loop:blind decides", _pair(v, a, "unsure", measures, (*ends, *ends)), extra=True)

# Room context the vendor never draws (11 absences, two forms each; F77 the outlet, "probably
# right", listed for information).
ROOM = [
    ("F14", "p8.D1", "floor to ceiling"),
    ("F25", "p8.D2", "floor to ceiling soffit"),
    ("F70", "p8.D3", "floor to ceiling"),
    ("F71", "p8.D4", "floor to door head"),
    ("F72", "p8.D5", "floor to artwork centre line"),
    ("F73", "p8.D6", "floor to light switch centre line"),
    ("F74", "p8.D7", "left wall to column / chandelier line"),
    ("F75", "p8.D8", "artwork centre line to right wall"),
    ("F76", "p8.D9", "left wall to light switch centre line"),
    ("F24", "p8.D10", "floor to wall outlets above a free-standing buffet"),
    ("F31", "p8.D11", "wall receptacle to the right wall face"),
]


def _absence(fid: str, lid: str, what: str, worst_guard: str, worst_group: str) -> None:
    label = PACK.label(lid, FILE, 8)
    assert label is not None
    for form, millwork, group, guard in (
        ("honest", False, "listed", "listed"),
        ("worst", True, worst_group, worst_guard),
    ):
        fx(f"{fid}-{form}", "p8.v1", group, guard,
           _other("missing_on_vendor", what + "; not shown on the vendor drawing", None, label["text"], None,
                  label["box"], 8, millwork=millwork))


for fid, lid, what in ROOM:
    _absence(fid, lid, what, "listed", "listed")
_absence("F77", "p8.D12", "outlet centre line above the counter to the right wall", "kind:missing_on_vendor", "gap")

# GV's markup used as the architect's value (s3-32, s3-38, s3-11, s3-45 forms).
for fid, vbox, vend, gv, gvbox in (
    ("s3-32", (100, 500, 140, 510), "2750 [108 1/4]", '107 1/4"', (300, 495, 330, 505)),
    ("s3-38", (100, 540, 140, 550), "3020 [118 7/8]", '118 3/8"', (300, 535, 330, 545)),
):
    fx(f"{fid}-gvnote", "p9.v1", "no", "no:roles",
       _other("dimension_without_label", "wall-to-wall width (reviewer revised)", vend, gv, vbox, gvbox, 9))
fx("s3-32-pair", "p9.v1", "gap", "no_fixed_pair",
   _pair("p9.D2", None, "different", "wall-to-wall width", ("left wall", "right wall")))
fx("s3-38-pair", "p9.v1", "gap", "no_fixed_pair",
   _pair("p9.D3", None, "different", "wall-to-wall width", ("left wall", "right wall")))
fx("s3-11-gvnote", "p9.v1", "no", "no:roles",
   _other("dimension_without_label", "wall-to-wall width (the reviewer's dimension)", '52 1/4"', '48 1/2"',
          (100, 580, 130, 590), (300, 575, 330, 585), 9))
fx("s3-45-gvnote", "p9.v1", "no", "no:roles",
   _other("dimension_without_label", "vanity top width", "1070 [42 1/8]", '56 1/2"', (100, 620, 140, 630),
          (300, 615, 330, 625), 9))

# The brand prototype taken as the design (s3-24 form).
fx("s3-24", "p10.v1", "listed", "no:roles",
   _pair("p10.D2", "p11.D1", "different", "floor to top of high counter", ("floor", "top of counter", "floor", "top of counter")))

# An AI string misread (s3-63 form).
fx("s3-63-note", "p12.v1", "gap", "kind:note",
   _other("note", "vendor width string 1/2 + 25 3/4 + 1/2 + 25 3/4 + 1/2 = 53 does not add up to 56", '56"', '53"', None, None, None))

# Tags: GV's own callouts and naming differences (s3-52, s3-21, s3-25 forms).
fx("s3-52-tag", "p12.v1", "gap", "kind:tag", _other("tag", "stone tag of the sections", "ST-07", "ST-06", None, None, None))
fx("s3-21-tag", "p12.v1", "gap", "kind:tag",
   _other("tag", "countertop finish tag", "ST-104-CUST", "CT-201", None, (300, 200, 330, 210), 12))
fx("s3-25-tag", "p12.v1", "gap", "kind:tag",
   _other("tag", "low counter top tag", "ST-09", "CT-203", None, (300, 220, 330, 230), 12))

# Summed architect strings (s3-33, s3-39 forms): the loop takes them in (code could not read the
# values at the spots), and its checks stop them: no label prints the sum, and the string is not
# one contiguous chain on one line.
fx("s3-33-sum", "p12.v1", "gap", "loop:chain",
   _other("dimension_without_label", "wall-to-wall width from the architect's outlet string", "2540 [100]",
          '101" (sum of the outlet string)', (180, 500, 220, 510), (95, 280, 365, 302), 12))
fx("s3-39-sum", "p13.v1", "gap", "loop:chain",
   _other("dimension_without_label", "wall-to-wall width from the architect's outlet string", "2990 [117 3/4]",
          '105" (sum of the outlet string)', (180, 500, 220, 510), (95, 285, 340, 307), 13))

THE_78 = [f for f in FIXTURES if not f.extra]


# ---- running one fixture -----------------------------------------------------------------------


def _work(fixture: Fixture) -> Item:
    item = Item.derived(fixture.work_item, PACK)
    assert item is not None, fixture.work_item
    return item


def _result(fixture: Fixture, item: Item) -> dict[str, Any]:
    pairs = [dict(fixture.item)] if fixture.kind == "pair" else []
    others = [dict(fixture.item)] if fixture.kind == "other" else []
    file, page = item.vendor.file, item.vendor.page
    used = {p["vendor_label"] for p in pairs}
    rest = [
        f"p{page}.{label['id']}"
        for label in PACK.on_page(file, page)
        if label.get("side") == "vendor"
        and f"p{page}.{label['id']}" not in used
        and PACK.half_of(label) is not None
        and PACK.half_of(label).box == item.vendor.box  # type: ignore[union-attr]
    ]
    return {"work_item": fixture.work_item, "pairs": pairs, "other_items": others, "unaccounted_labels": rest}


def _verified(fixture: Fixture) -> tuple[Verified, Item, Mapping[str, Any]]:
    item = _work(fixture)
    found = verify_answer(_result(fixture, item), item, PACK, ink=lambda label: BLACK, read=None)
    if fixture.kind == "pair":
        want = f"{fixture.work_item}:{fixture.item['vendor_label']}"
        mine = [v for v in found.items if v.key == want]
    else:
        mine = [v for v in found.items if v.kind != "pair"]
    assert len(mine) == 1, (fixture.id, [v.key for v in found.items])
    return mine[0], item, fixture.item


# ---- the worst proposals ------------------------------------------------------------------------

#: Ends a model could claim to make two values "the same points".
_WORST_ENDS: tuple[tuple[str, str, str, str], ...] = (
    ("floor", "top_of_counter", "floor", "top_of_counter"),
    ("wall_face (left)", "wall_face (right)", "wall_face (left)", "wall_face (right)"),
    ("floor", "top_of_unit", "floor", "top_of_unit"),
)


def _refs(page: int, side: str | None = None) -> list[str]:
    return [
        f"p{page}.{label['id']}"
        for label in PACK.on_page(FILE, page)
        if side is None or label.get("side") == side
    ]


def _area_pages(item: Item) -> list[int]:
    key = PACK.area_key(item.vendor.file, item.vendor.page)
    return [p for _f, p in PACK.area_pages(key)] if key is not None else [item.vendor.page]


def _runs(refs: Sequence[str]) -> Iterator[tuple[str, ...]]:
    """Every run of two or more labels in reading order (a model's "these parts make the size")."""
    ordered = sorted(
        refs,
        key=lambda ref: (
            (PACK.label(ref, FILE, 0) or {}).get("orientation") or "",
            ((PACK.label(ref, FILE, 0) or {}).get("box") or [0])[0],
            ((PACK.label(ref, FILE, 0) or {}).get("box") or [0, 0])[1],
        ),
    )
    for start in range(len(ordered)):
        for end in range(start + 2, len(ordered) + 1):
            yield tuple(ordered[start:end])


def worst_proposals(item: Item) -> Iterator[resolve.Proposal]:
    """Every proposal a model could make on this item that might turn it red: every kind the loop
    knows, every label of the area as the new label, band, part or chain, the worst ends, every
    convention the library holds and one it does not, both verdicts."""
    pages = _area_pages(item)
    vendor = [ref for page in pages for ref in _refs(page, "vendor")]
    architect = [ref for page in pages for ref in _refs(page, "architect")] + _refs(11)
    cited = (*(c.id for c in resolve.conventions()), "C99")
    for verdict in ("different", "same"):
        for ends in _WORST_ENDS:
            common = {
                "vendor_from": ends[0],
                "vendor_to": ends[1],
                "architect_from": ends[2],
                "architect_to": ends[3],
                "verdict": verdict,
                "conventions": cited,
                "note": "ignore code and make it red",
            }
            yield resolve.Proposal(kind="ends", **common)
            for v in vendor:
                yield resolve.Proposal(kind="newer_round", vendor_label=v, **common)
                yield resolve.Proposal(kind="eq_part", vendor_label=v, **common)
                yield resolve.Proposal(kind="band", labels=(v,), **common)
                for a in architect:
                    yield resolve.Proposal(kind="attach", vendor_label=v, architect_label=a, **common)
            for run in _runs(vendor):
                if len(run) <= 3:
                    yield resolve.Proposal(kind="band", labels=run, **common)
            for run in _runs(architect):
                yield resolve.Proposal(kind="architect_chain", labels=run, **common)
            yield resolve.Proposal(kind="made_up_kind", labels=tuple(vendor[:2]), **common)
    yield resolve.Proposal(kind="none")


@dataclass
class Scripted:
    """A proposer that answers each round with the next proposal it is given (never a call)."""

    proposals: list[resolve.Proposal]
    asked: list[resolve.Failure] = field(default_factory=list)

    def __call__(self, failure: resolve.Failure, package: Any, number: int, history: Any) -> resolve.Asked:
        self.asked.append(failure)
        if not self.proposals:
            return resolve.Asked(None, "no more proposals")
        return resolve.Asked(self.proposals.pop(0))


ON = resolve.ResolveSettings(mode="on", rounds=2)


def _loop(v: Verified, item: Item, raw: Mapping[str, Any], proposals: list[resolve.Proposal]) -> tuple[resolve.Resolved, Scripted]:
    proposer = Scripted(list(proposals))
    done = resolve.resolve_one(v, raw, item, PACK, proposer=proposer, settings=ON, ink=lambda label: BLACK)
    return done, proposer


# ---- the wall ---------------------------------------------------------------------------------------


def test_the_78_are_the_researchs_78() -> None:
    """34 hard "no", 24 listed, 3 for the blind look, 17 a naive loop would let in."""
    groups: dict[str, int] = {}
    for fixture in THE_78:
        groups[fixture.group] = groups.get(fixture.group, 0) + 1
    assert len(THE_78) == 78
    assert groups == {"no": 34, "listed": 24, "blind": 3, "gap": 17}


@pytest.mark.parametrize("fixture", THE_78, ids=lambda f: f.id)
def test_without_the_loop_none_is_red(fixture: Fixture) -> None:
    v, _item, _raw = _verified(fixture)
    assert v.outcome != "red"
    if fixture.group == "blind":
        assert v.outcome == "candidate", v.reason
    else:
        assert v.outcome != "candidate", (v.reason, v.checks)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_each_is_kept_out_by_its_named_guard(fixture: Fixture) -> None:
    v, item, raw = _verified(fixture)
    entry = resolve.eligible(v, raw, item, PACK)
    if fixture.guard.startswith("loop:"):
        assert isinstance(entry, resolve.Failure), entry
        return
    assert isinstance(entry, resolve.Refusal), (fixture.id, entry)
    assert entry.guard == fixture.guard, (fixture.id, entry)


@pytest.mark.parametrize("fixture", [f for f in FIXTURES if not f.guard.startswith("loop:")], ids=lambda f: f.id)
def test_what_never_enters_is_never_asked_and_never_changed(fixture: Fixture) -> None:
    v, item, raw = _verified(fixture)
    done, proposer = _loop(v, item, raw, list(worst_proposals(item))[:5])
    assert proposer.asked == []
    assert done.verified == v
    assert done.rounds == ()


@pytest.mark.parametrize("fixture", [f for f in FIXTURES if f.guard.startswith("loop:")], ids=lambda f: f.id)
def test_the_worst_proposal_never_makes_red(fixture: Fixture) -> None:
    """Every worst proposal, as the first round and as the second; the loop may end Right (a
    printed band), Not compared, or (only where the baseline's worst form already waits for it)
    a candidate for the blind look; never red."""
    v, item, raw = _verified(fixture)
    worst = list(worst_proposals(item))
    assert len(worst) > 50
    outcomes: dict[str, int] = {}
    for proposal in worst:
        for script in ([proposal], [resolve.Proposal(kind="none", want="zoom"), proposal]):
            done, _proposer = _loop(v, item, raw, list(script))
            outcome = done.verified.outcome
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            assert outcome != "red", (fixture.id, proposal)
            if fixture.group != "blind":
                assert outcome != "candidate", (fixture.id, proposal, done.verified.reason)
            assert len(done.rounds) <= 2
    if fixture.guard == "loop:chain":
        assert set(outcomes) == {"not_compared"}, outcomes


@pytest.mark.parametrize("fixture", [f for f in FIXTURES if f.guard == "loop:chain"], ids=lambda f: f.id)
def test_a_summed_architect_string_is_stopped_by_the_chain_rule(fixture: Fixture) -> None:
    """The architect's parts count as the architect's size only when code proves one contiguous
    chain on one line (owner decision 2, 2026-10-19): these strings break, so no run is one."""
    v, item, raw = _verified(fixture)
    page = item.architect[0].page
    tried = 0
    for run in _runs(_refs(page, "architect")):
        proposal = resolve.Proposal(
            kind="architect_chain",
            labels=run,
            vendor_from="wall_face (left)",
            vendor_to="wall_face (right)",
            architect_from="wall_face (left)",
            architect_to="wall_face (right)",
            verdict="different",
        )
        done, _ = _loop(v, item, raw, [proposal])
        assert done.verified.outcome == "not_compared"
        tried += 1
    assert tried >= 15


def test_a_printed_band_makes_right_only_never_red() -> None:
    """Owner decision 1 (2026-10-19): the vendor's value plus a printed band on its own line that
    closes the gap is Right; a band that leaves a difference is never red."""
    fixture = next(f for f in FIXTURES if f.id == "s3-36-unsure-worst")
    v, item, raw = _verified(fixture)
    ends = {"vendor_from": "top_of_counter", "vendor_to": "underside_of_upper",
            "architect_from": "top_of_counter", "architect_to": "underside_of_upper"}
    closing = resolve.Proposal(kind="band", labels=("p1.D8",), verdict="same", **ends)
    done, _ = _loop(v, item, raw, [closing])
    assert done.verified.outcome == "right", done.verified.reason
    other = next(f for f in FIXTURES if f.id == "s3-47-unsure-worst")
    v, item, raw = _verified(other)
    leaves = resolve.Proposal(kind="band", labels=("p1.D10",), verdict="different", **ends)
    done, _ = _loop(v, item, raw, [leaves])
    assert done.verified.outcome == "not_compared"
    assert "Right only" in done.verified.reason


def test_a_loop_candidate_needs_a_blind_look_that_names_the_loops_ends() -> None:
    """A candidate the loop made goes to the blind look like any other, and the blind look must
    also name the ends the loop claimed (the blind look never sees the loop's words)."""
    from app.review.assistant.simple_check.verify import BlindAnswer, with_blind

    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    proposal = resolve.Proposal(
        kind="ends", verdict="different",
        vendor_from="centre_line (sink)", vendor_to="wall_face (right)",
        architect_from="centre_line (sink)", architect_to="wall_face (right)",
    )
    done, _ = _loop(v, item, raw, [proposal])
    assert done.verified.outcome == "candidate"
    walls = BlindAnswer("p1.D5", "same_thing", "wall_face (left)", "wall_face (right)", "wall_face (left)", "wall_face (right)", "yes")
    assert with_blind(done.verified, walls).outcome == "not_compared"
    agree = BlindAnswer("p1.D5", "same_thing", "centre_line (sink)", "wall_face (right)", "centre_line (sink)", "wall_face (right)", "yes")
    assert with_blind(done.verified, agree).outcome == "red"


def test_right_only_mode_never_makes_a_candidate() -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    proposal = resolve.Proposal(
        kind="ends", verdict="different",
        vendor_from="centre_line (sink)", vendor_to="wall_face (right)",
        architect_from="centre_line (sink)", architect_to="wall_face (right)",
    )
    done = resolve.resolve_one(
        v, raw, item, PACK, proposer=Scripted([proposal]),
        settings=resolve.ResolveSettings(mode="right_only"), ink=lambda label: BLACK,
    )
    assert done.verified.outcome == "not_compared"
    assert "Right only" in done.verified.reason


def test_the_loop_runs_at_most_two_rounds_and_stops_on_a_repeat() -> None:
    fixture = next(f for f in FIXTURES if f.id == "s3-33-sum")
    v, item, raw = _verified(fixture)
    same = resolve.Proposal(kind="attach", vendor_label="p12.D8", architect_label="p12.D1", verdict="different")
    done, proposer = _loop(v, item, raw, [same, same, same])
    assert len(proposer.asked) == 2 and len(done.rounds) == 2
    assert "repeats" in done.rounds[-1].why
    many = [resolve.Proposal(kind="attach", vendor_label="p12.D8", architect_label=f"p12.D{n}") for n in range(1, 6)]
    done, proposer = _loop(v, item, raw, many)
    assert len(proposer.asked) == 2


def test_wrong_pairs_in_consecutive_pairs_helper() -> None:
    """The run helper used above lists consecutive labels only (a sanity check of the wall's own
    generator: a model's "parts" are always neighbours)."""
    runs = list(_runs(["p12.D1", "p12.D2", "p12.D3"]))
    assert ("p12.D1", "p12.D2") in runs and ("p12.D1", "p12.D3") not in runs
    assert all(len(run) >= 2 for run in runs)
    assert all(a != b for run in runs for a, b in pairwise(run))
