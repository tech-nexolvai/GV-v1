"""The devil's advocate: what must hold before a Wrong item may become a red cloud (D1).

With nobody in the loop, a false red cloud goes straight to the vendor. A red cloud needs code to
have read both values at their spots (`witness.py`) AND proof that the two values measure the same
thing. Code can read values but cannot, on its own, tell which architect dimension goes with which
vendor dimension. So, under the orchestrator's decision D1 (provisional; Anant may switch the
setting `assistant_pairing_proof` to `code_only`), a pairing counts as proved only when ALL of these
hold:

1. **Two blind picks agree on the same label ids**: the helper's pick (its `vendor_dim` /
   `architect_dim`) and the advocate's, made without seeing the claim or the helper's pick: it is
   shown the vendor's label and the whole architect drawing with every label numbered, and asked
   which one measures the same thing. Both must also say the same about where each end lands.
2. **Code read both values at those spots** (the labels themselves, or `verify_value`).
3. **The drawn extents agree** (`same_extent`): code lines the two drawings up on dimensions they
   share and checks both dimension lines' end points land on the same drawn extent, allowing only
   the claimed difference.
4. **Every code check passes** (`code_checks`): the vendor value is the drawing's black or grey
   ink, not inside one of GV's text notes; the labels are on a vendor half and an architect half
   (never a brand prototype); both run the same way; the vendor value is from the newest round;
   its millimetres and inches agree; and the difference is not a unit slip or a one-digit misread.

In `code_only` mode only items code paired by itself can be red: the vendor's own sums (one string,
no pairing) and GV's rule checks. Anything less goes to the grey list with its reason; nobody is
asked a question.

**How the verdict reaches the drawing.** The advocate replaces the critic for red candidates and
writes through the same finding fields (`checks.critic`, `critic`): `passed` is an AI witness that
agrees; `objects` is a check that disagrees, so the trust rule (`witness.trust_level`) makes the
item "AI reading only" and the red/green drawing lists it in grey with the advocate's reason. The
full reasoning is kept on the finding as `pairing`.

Pure code here (no model call, no PDF): the roster (`full_check_roster`) makes the crops and the one
blind call per candidate and hands the results to `decide`.
"""

from __future__ import annotations

import io
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

__all__ = [
    "ADVOCATE_SCHEMA",
    "ADVOCATE_SYSTEM",
    "CODE_PAIRED_SOURCES",
    "PAIRED_SOURCES",
    "SPANS",
    "Check",
    "Extent",
    "Verdict",
    "advocate_question",
    "code_checks",
    "decide",
    "ink_colour",
    "is_slip",
    "needs_pairing",
    "same_extent",
]

#: Where each end of a dimension lands (`knowledge/what-dimensions-measure.md`).
SPANS: Final = (
    "top_of_counter",
    "underside_of_counter",
    "top_of_splash",
    "floor",
    "wall_face",
    "cabinet_side",
    "cabinet_end",
    "centre_line",
    "box_above_unit",
    "blocking",
    "other",
)
#: A Wrong item between two drawings: its pairing must be proved.
PAIRED_SOURCES: Final = frozenset({"architect", "brand_prototype", "gv_review"})
#: Code paired these itself: the vendor's own string (one string, no pairing), GV's rule check.
CODE_PAIRED_SOURCES: Final = frozenset({"vendor_arithmetic", "rules"})

type Outcome = Literal["pass", "fail", "unknown"]
type Extent = Literal["same_extent", "different_extent", "cannot_tell"]

ADVOCATE_SYSTEM: Final = (
    "You check one pairing on millwork shop drawings, from the pictures only. Coloured marks are "
    "a reviewer's notes; they are never a dimension. Answer none when no numbered dimension "
    "measures the same thing."
)

_YES_NO: Final[Mapping[str, Any]] = {"type": "string", "enum": ["yes", "no", "unsure"]}
_ENDS: Final[Mapping[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["from", "to"],
    "properties": {
        "from": {"type": "string", "enum": list(SPANS), "description": "Where one end lands"},
        "to": {"type": "string", "enum": list(SPANS), "description": "Where the other end lands"},
    },
}
#: The advocate's reply (structured outputs).
ADVOCATE_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["architect_pick", "vendor_spans", "architect_spans", "vendor_ink_black", "note"],
    "properties": {
        "architect_pick": {
            "type": "string",
            "description": "The numbered architect dimension's id (D10), or none",
        },
        "vendor_spans": {**_ENDS, "description": "Where the marked vendor dimension's ends land"},
        "architect_spans": {**_ENDS, "description": "Where the picked dimension's ends land"},
        "vendor_ink_black": {
            **_YES_NO,
            "description": "The marked vendor value is printed in black or grey ink",
        },
        "note": {"type": "string", "description": "One short line, or empty"},
    },
}


def advocate_question(vendor_label: str, item: str) -> str:
    """The blind question: the vendor's label and the item, never the claimed architect label or
    either value."""
    return (
        f"The first picture is the vendor's drawing; the dimension marked there is {vendor_label} "
        f"({item}). The second picture is the architect's drawing with every printed dimension "
        "numbered. Which numbered dimension measures the same thing as the marked one? Answer "
        "none if none does. Then, for the marked one and the one you picked, say where each end "
        "lands (top_of_counter, underside_of_counter, top_of_splash, floor, wall_face, "
        "cabinet_side, cabinet_end, centre_line, box_above_unit, blocking, other)."
    )


@dataclass(frozen=True, slots=True)
class Check:
    """One code check: its name, pass / fail / unknown, and why in plain words."""

    name: str
    outcome: Outcome
    why: str = ""


@dataclass(slots=True)
class Verdict:
    """The advocate's verdict on one red candidate."""

    result: Literal["passed", "objects"]
    reason: str
    answers: dict[str, str] = field(default_factory=dict)

    def critic(self) -> dict[str, Any]:
        """As the finding's `critic` (the field the screen and the drawing already read)."""
        return {
            "result": self.result,
            "answers": dict(self.answers),
            "note": self.reason,
            "model": "devil's advocate",
        }


def needs_pairing(finding: Mapping[str, Any]) -> bool:
    """A Wrong item between two drawings that code did not pair by itself."""
    if finding.get("paired_by") == "code":
        return False
    return str(finding.get("source") or "") in PAIRED_SOURCES and not finding.get("rule_check_id")


# ---- the code checks -------------------------------------------------------------------------------


def _inches(label: Mapping[str, Any] | None) -> Decimal | None:
    if label is None:
        return None
    try:
        return Decimal(str(label.get("inches")))
    except (InvalidOperation, ValueError):
        return None


def ink_colour(png: bytes) -> tuple[int, float] | None:
    """(ink pixels, the share of them in colour) inside a small crop; `None` with no ink. Ink is
    any pixel clearly darker than paper; colour is a pixel whose channels differ a lot (GV's red,
    blue, green or yellow markup), not black or grey."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as opened:
        picture = opened.convert("RGB")
    ink = coloured = 0
    raw = picture.tobytes()
    for at in range(0, len(raw) - 2, 3):
        red, green, blue = raw[at], raw[at + 1], raw[at + 2]
        if min(red, green, blue) > 200:
            continue
        ink += 1
        if max(red, green, blue) - min(red, green, blue) > 70:
            coloured += 1
    return None if ink == 0 else (ink, coloured / ink)


#: Digits a drawn number is often read as one another (#756): a slip, not a difference.
_CONFUSED: Final = frozenset({("1", "7"), ("3", "8"), ("5", "6"), ("0", "8"), ("6", "8")})


def is_slip(vendor: Decimal, architect: Decimal, *, drawn: bool) -> str | None:
    """Why the difference looks like a reading or unit slip, or `None`: feet read as inches (12x),
    a lost or added digit (10x), or (on a drawn, OCR-read value) one digit that is easy to misread.
    """
    if vendor <= 0 or architect <= 0:
        return None
    for ratio in (Decimal(10), Decimal(12)):
        if vendor == architect * ratio or architect == vendor * ratio:
            return f"one value is {ratio} times the other: a unit or digit slip"
    if drawn:
        a, b = format(vendor.normalize(), "f"), format(architect.normalize(), "f")
        if len(a) == len(b):
            diffs = [(x, y) for x, y in zip(a, b, strict=True) if x != y]
            if len(diffs) == 1 and tuple(sorted(diffs[0])) in _CONFUSED:
                return f"drawn digits {diffs[0][0]} and {diffs[0][1]} are easy to misread"
    return None


def _inside(box: Sequence[float], other: Sequence[float]) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return other[0] <= cx <= other[2] and other[1] <= cy <= other[3]


def _half_of(
    page_map: Mapping[str, Any] | None, file: str | None, page: int, box: Sequence[float]
) -> Mapping[str, Any] | None:
    for entry in (page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or entry.get("page") != page:
            continue
        if file is not None and entry.get("file") not in (None, file):
            continue
        for half in entry.get("halves") or []:
            if isinstance(half, Mapping) and half.get("box") and _inside(box, half["box"]):
                return half
    return None


def _newest_round(
    page_map: Mapping[str, Any] | None, file: str | None, page: int
) -> tuple[int | None, int | None]:
    """(this page's vendor round, the newest vendor round of its area) from the page map."""
    entries = [e for e in (page_map or {}).get("pages") or [] if isinstance(e, Mapping)]
    entry = next((e for e in entries if e.get("page") == page), None)
    if entry is None:
        return None, None
    area = entry.get("area") or entry.get("section")
    own: list[int] = [
        int(h["round"])
        for h in entry.get("halves") or []
        if isinstance(h, Mapping) and h.get("role") == "vendor" and isinstance(h.get("round"), int)
    ]
    newest: list[int] = [
        int(h["round"])
        for e in entries
        if area and (e.get("area") or e.get("section")) == area
        for h in e.get("halves") or []
        if isinstance(h, Mapping) and h.get("role") == "vendor" and isinstance(h.get("round"), int)
    ]
    return (max(own) if own else None), (max(newest) if newest else None)


def code_checks(
    vendor: Mapping[str, Any] | None,
    architect: Mapping[str, Any] | None,
    *,
    annotations: Iterable[Mapping[str, Any]] = (),
    page_map: Mapping[str, Any] | None = None,
    vendor_ink: tuple[int, float] | None = None,
) -> list[Check]:
    """The advocate's code checks (7a, $0) on a candidate's two labels (`architect` None: a
    vendor-only item such as the vendor's own string). `vendor_ink`: `ink_colour` of the vendor
    value's spot, when its picture could be drawn."""
    checks: list[Check] = []
    if vendor is None:
        return [Check("labels", "fail", "the vendor's value has no label code read")]
    box = [float(v) for v in vendor.get("box") or (0, 0, 0, 0)]
    page = int(vendor.get("page") or 0)
    file = vendor.get("file")
    # 1. Ink: the drawing's black or grey, not GV's colour.
    if vendor_ink is None:
        checks.append(Check("ink", "unknown", "the vendor value's ink could not be looked at"))
    else:
        share = vendor_ink[1]
        checks.append(
            Check("ink", "fail", "the vendor value is in colour: GV's markup")
            if share > 0.25
            else Check("ink", "pass")
        )
    # 2. Not inside one of GV's text notes ("STONE TOP by GV").
    notes = [
        a
        for a in annotations
        if a.get("page") == page
        and a.get("kind") == "FreeText"
        and (file is None or a.get("file") in (None, file))
        and isinstance(a.get("box"), Sequence)
        and _inside(box, [float(v) for v in a["box"]])
    ]
    checks.append(
        Check("annotation", "fail", f"inside GV's note {str(notes[0].get('text'))[:60]!r}")
        if notes
        else Check("annotation", "pass")
    )
    # 3. Roles: a vendor half and an architect half, never a brand prototype.
    vendor_side = str(vendor.get("side") or "unknown")
    if vendor_side != "vendor":
        checks.append(Check("roles", "fail", f"the vendor value is on the {vendor_side}'s drawing"))
    elif architect is not None and str(architect.get("side") or "unknown") != "architect":
        checks.append(
            Check("roles", "fail", f"the reference is on the {architect.get('side')}'s drawing")
        )
    else:
        checks.append(Check("roles", "pass"))
    # 4. Axis: both run the same way.
    if architect is not None:
        same = vendor.get("orientation") == architect.get("orientation")
        checks.append(
            Check("axis", "pass")
            if same
            else Check("axis", "fail", "one dimension is a width and the other a height")
        )
    # 5. The newest round that shows the item.
    own, newest = _newest_round(page_map, file if isinstance(file, str) else None, page)
    if own is not None and newest is not None and own < newest:
        checks.append(
            Check("round", "fail", f"round {own}: round {newest} is the vendor's current drawing")
        )
    else:
        checks.append(Check("round", "pass"))
    # 6. A dual label's millimetres and inches agree.
    if any(label.get("units") == "disagrees" for label in (vendor, architect) if label):
        checks.append(Check("units", "fail", "its millimetres and inches disagree"))
    else:
        checks.append(Check("units", "pass"))
    # 7. Not a unit slip or a one-digit misread.
    v, a = _inches(vendor), _inches(architect)
    slip = None
    if v is not None and a is not None:
        drawn = "drawn" in (vendor.get("route"), (architect or {}).get("route"))
        slip = is_slip(v, a, drawn=drawn)
    checks.append(
        Check("slip", "fail", f"check the reading: {slip}") if slip else Check("slip", "pass")
    )
    return checks


# ---- the drawn extents -----------------------------------------------------------------------------

#: Anchors (labels printed once on each drawing with the same value) needed to line them up.
_MIN_ANCHORS: Final = 2
#: How far a line's end may land from where it should (points), at least.
_TOLERANCE_PT: Final = 4.0


def _axis(label: Mapping[str, Any]) -> tuple[float, float] | None:
    """The dimension line's two ends along its own axis (x for a width, y for a height)."""
    line = label.get("dim_line")
    if not line or len(line) != 2:
        return None
    index = 0 if label.get("orientation") == "h" else 1
    ends = sorted(float(point[index]) for point in line)
    return ends[0], ends[1]


def same_extent(
    vendor: Mapping[str, Any],
    architect: Mapping[str, Any],
    vendor_labels: Sequence[Mapping[str, Any]],
    architect_labels: Sequence[Mapping[str, Any]],
) -> tuple[Extent, str]:
    """Whether the two dimension lines span the same drawn extent once the drawings are lined up.

    Code lines the architect's drawing up with the vendor's along the pair's axis on anchors:
    dimensions of the same orientation printed exactly once on each drawing with the same value
    (never the pair itself). A least-squares fit of the anchors' line ends maps the architect's
    line into the vendor's drawing; its ends must land on the vendor line's ends, allowing only
    the claimed difference at the vendor's own scale. Fewer than two anchors, a poor fit, or a
    line with no ends found: `cannot_tell` (so the item stays grey)."""
    if vendor.get("orientation") != architect.get("orientation"):
        return "different_extent", "one is a width and the other a height"
    v_ends, a_ends = _axis(vendor), _axis(architect)
    v_in, a_in = _inches(vendor), _inches(architect)
    if v_ends is None or a_ends is None or not v_in or not a_in:
        return "cannot_tell", "a dimension line's ends were not found"
    orientation = vendor.get("orientation")

    def once(labels: Sequence[Mapping[str, Any]], skip: object) -> dict[Decimal, Mapping[str, Any]]:
        counted: dict[Decimal, list[Mapping[str, Any]]] = {}
        for label in labels:
            value = _inches(label)
            if (
                label.get("id") == skip
                or value is None
                or label.get("orientation") != orientation
                or _axis(label) is None
            ):
                continue
            counted.setdefault(value, []).append(label)
        return {value: found[0] for value, found in counted.items() if len(found) == 1}

    v_once, a_once = once(vendor_labels, vendor.get("id")), once(
        architect_labels, architect.get("id")
    )
    pairs: list[tuple[float, float]] = []
    for value in set(v_once) & set(a_once):
        a_axis, v_axis = _axis(a_once[value]), _axis(v_once[value])
        assert a_axis is not None and v_axis is not None
        pairs += list(zip(a_axis, v_axis, strict=True))
    if len(pairs) < 2 * _MIN_ANCHORS or len({round(a, 1) for a, _v in pairs}) < 2:
        return "cannot_tell", "too few dimensions printed on both drawings to line them up"
    xs, ys = [a for a, _v in pairs], [v for _a, v in pairs]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread <= 0:
        return "cannot_tell", "the shared dimensions do not line the drawings up"
    slope = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / spread
    offset = mean_y - slope * mean_x
    if slope <= 0:
        return "cannot_tell", "the shared dimensions do not line the drawings up"
    residual = statistics.median(abs(slope * x + offset - y) for x, y in pairs)
    tolerance = max(_TOLERANCE_PT, 0.015 * (v_ends[1] - v_ends[0]))
    if residual > tolerance:
        return "cannot_tell", f"the drawings do not line up well enough ({residual:.0f} pt)"
    mapped = (slope * a_ends[0] + offset, slope * a_ends[1] + offset)
    per_inch = (v_ends[1] - v_ends[0]) / float(v_in)
    allowed = abs(float(v_in - a_in)) * per_inch + 2 * tolerance
    error = abs(mapped[0] - v_ends[0]) + abs(mapped[1] - v_ends[1])
    if error <= allowed:
        return "same_extent", f"the ends land together ({error:.0f} of {allowed:.0f} pt allowed)"
    return "different_extent", f"the ends land apart ({error:.0f} pt, {allowed:.0f} allowed)"


# ---- the verdict -------------------------------------------------------------------------------------


def decide(
    *,
    mode: str,
    checks: Sequence[Check],
    printed: bool,
    helper_pick: str | None,
    advocate: Mapping[str, Any] | None,
    extent: tuple[Extent, str] | None,
    code_paired: bool,
) -> Verdict:
    """The verdict on one red candidate under D1 (see the module). `printed`: code read both
    values at their spots. `helper_pick`: the helper's architect label (`p23.D10`). `advocate`: the
    blind pick's reply (`ADVOCATE_SCHEMA`) with `pick` set to the label it names on the
    architect's page (`p23.D10`), or `None` when it was not asked. `extent`: `same_extent`'s."""
    answers: dict[str, str] = {check.name: str(check.outcome) for check in checks}
    failed = [check for check in checks if check.outcome != "pass"]
    if failed:
        first = failed[0]
        why = first.why or f"the {first.name} check could not be made"
        return Verdict("objects", f"Not proved: {why}.", answers)
    if code_paired:
        answers["pairing"] = "code"
        return Verdict("passed", "", answers)
    if mode == "code_only":
        answers["pairing"] = "ai"
        return Verdict(
            "objects",
            "Not proved: only code's own pairings can be clouded (setting code_only).",
            answers,
        )
    if not printed:
        return Verdict(
            "objects", "Not proved: code did not read both values at their spots.", answers
        )
    if helper_pick is None:
        return Verdict("objects", "Not proved: the architect's value has no label.", answers)
    if advocate is None:
        return Verdict("objects", "Not checked: the fixed price ran out before this item.", answers)
    pick = str(advocate.get("pick") or "none")
    answers.update(advocate_pick=pick, helper_pick=helper_pick)
    if advocate.get("vendor_ink_black") == "no":
        return Verdict(
            "objects", "Not proved: the checker sees the vendor value in colour.", answers
        )
    if pick != helper_pick:
        said = "no architect dimension" if pick == "none" else pick
        return Verdict(
            "objects", f"Not proved: a blind second pick chose {said}, not {helper_pick}.", answers
        )
    vendor_spans, architect_spans = advocate.get("vendor_spans"), advocate.get("architect_spans")
    if vendor_spans != architect_spans:
        answers["spans"] = "differ"
        return Verdict(
            "objects",
            "Not proved: the two dimensions' ends land in different places "
            f"({_ends(vendor_spans)} vs {_ends(architect_spans)}).",
            answers,
        )
    answers["spans"] = "agree"
    state, why = extent or ("cannot_tell", "not measured")
    answers["extent"] = state
    if state != "same_extent":
        return Verdict("objects", f"Not proved: {why}.", answers)
    answers["pairing"] = "proved"
    return Verdict("passed", "", answers)


def _ends(spans: object) -> str:
    if isinstance(spans, Mapping):
        return f"{spans.get('from')} to {spans.get('to')}"
    return "unknown"
