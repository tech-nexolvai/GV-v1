"""The sink's centre against the sink base's centre (fix 3, 2026-10-25), $0, no AI: code decides.

A problem missed on a real upload: the reference (the cabinet maker's drawings) draws the sink
centred on its sink base between two cabinets, and GV's drawing prints the cut-out's centre from
the top's two ends 1" off that centre. A helper set one of those offsets against the sink base's
width (a sink centre line against two cabinet sides: `dim_ends` now never compares those), and
nothing compared the centres. (Made-up example: a 20" sink base between two 12" cabinets, a 45"
top 1" past the last cabinet: the base's centre is 22" from the wall end and 23" from the free
end; a vendor printing 22" / 24" from the ends of its 46" top with a 1" field cut is 1" off.)

**What code must prove** (anything unsure is Not compared, never red):

- **The vendor's sink centre** (`vendor_sinks`): two dimensions in a row on one line meeting at the
  sink cut-out's middle (`dim_ends`: `sink_centre`), their outer ends the top's two ends (the
  overall printed with the same two ends, when there is one, must be their sum). An end that is
  GV's field cut (`field_cut_end`) counts its field cut off: the top without it is compared.
- **The reference's sink base** (`reference_bases`): a cabinet tag that names a sink base (SB:
  VSB30, SB36) over its width, with the sink drawn centred on it (a cut-out's middle within
  `CENTRED_IN` of the base's middle); the top printed over the same run (its two ends named by
  `dim_ends`: a cabinet's side at a wall end, the overhang's edge at a free end); and the printed
  sizes from the top's end to the base, one end to the next on one line (code adds them; the
  drawn positions are never measured).
- **The same ends**: the vendor's top and the reference's top must be the same length (the vendor's
  less its field cuts), and the two drawings are read the same way round (or mirrored, a reversed
  unit) by their ends' kinds: a field-cut or wall end faces the reference's wall end, a free end
  its free end. When the base is centred on the top either way round is the same.

The vendor's centre, measured from the top's ends, against the base's centre from the same ends:
the same is **Right**; a difference is a **Likely problem** ("please check and confirm"), never red:
this kind of check has no track record yet (`check_record`, kind `sink_centre`).

The escalation routes an item that pairs a sink-centre dimension with the sink base's width here
(`route`); the full check also checks every paired vendor page (`check_pack`, `add_to_answer`).

Verification: `tests/review/assistant/test_sink_centre.py` (made-up drawings, no client data).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant import dim_ends, likely_problems
from app.review.assistant.ledger import format_inches

__all__ = [
    "CENTRED_IN",
    "CREATED_BY",
    "FIELD",
    "Base",
    "Compared",
    "VendorSink",
    "add_to_answer",
    "check_pack",
    "compare",
    "reference_bases",
    "route",
    "vendor_sinks",
]

_log = logging.getLogger(__name__)

CREATED_BY: Final = "sink_centre"
#: The finding's record of this check.
FIELD: Final = "sink_centre"
#: The reference draws the sink centred on its base: the cut-out's middle this close (inches).
CENTRED_IN: Final = 0.25
#: Two printed values this close are the same (a sixteenth).
TOLERANCE: Final = Fraction(1, 16)
#: The item's words.
ITEM: Final = "Sink centre on the sink base"

type Way = Literal["wall", "free"]


@dataclass(frozen=True, slots=True)
class VendorSink:
    """The vendor's sink centre: two dimensions meeting at the cut-out's middle."""

    first: dim_ends.Span
    second: dim_ends.Span
    low: dim_ends.End
    high: dim_ends.End
    cuts_low: int
    cuts_high: int
    per_end: Fraction

    @property
    def total(self) -> Fraction:
        return self.first.inches + self.second.inches

    @property
    def design(self) -> Fraction:
        return self.total - self.per_end * (self.cuts_low + self.cuts_high)


@dataclass(frozen=True, slots=True)
class Base:
    """The reference's sink base, its top and the base's centre from the top's two ends."""

    base: dim_ends.Span
    tag: str
    top: dim_ends.Span
    top_low: dim_ends.End
    top_high: dim_ends.End
    from_low: Fraction
    """The base's centre from the top's low end (left or top), by the printed sizes."""
    parts_low: tuple[Fraction, ...]
    """The printed sizes from the top's low end to the base."""

    @property
    def from_high(self) -> Fraction:
        return self.top.inches - self.from_low


@dataclass(frozen=True, slots=True)
class Compared:
    """One vendor sink against one reference base."""

    outcome: Literal["same", "different", "unsure"]
    words: str
    vendor: str
    architect: str
    why: str
    vendor_in: Fraction | None = None
    architect_in: Fraction | None = None


def _way(end: dim_ends.End) -> Way | None:
    """A wall end (GV's field cut is added there, or a wall's face) or a free end (the top's edge
    past the cabinets); a cabinet's side alone says neither (a top may stop flush at a free end)."""
    if end.kinds & {"field_cut_end", "wall"} and "overhang_end" not in end.kinds:
        return "wall"
    if "overhang_end" in end.kinds:
        return "free"
    return None


def _inches(value: Fraction) -> str:
    return format_inches(value)


def vendor_sinks(page: dim_ends.Page) -> list[VendorSink]:
    """The page's sink centres printed as two dimensions meeting at a cut-out's middle (see the
    module), each with its field cuts."""
    out: list[VendorSink] = []
    spans = list(page.spans.values())
    for first in spans:
        ends_first = page.ends(first.id)
        if ends_first is None or "sink_centre" not in ends_first.high.kinds:
            continue
        for second in spans:
            if second is first or second.axis != first.axis:
                continue
            if abs(second.row - first.row) > dim_ends.SAME_PT:
                continue
            if abs(second.a - first.b) > dim_ends.SAME_PT:
                continue
            ends_second = page.ends(second.id)
            if ends_second is None or "sink_centre" not in ends_second.low.kinds:
                continue
            if ends_first.low.kinds & {"sink_centre", "cutout_edge"}:
                continue
            if ends_second.high.kinds & {"sink_centre", "cutout_edge"}:
                continue
            total = first.inches + second.inches
            overalls = [
                s
                for s in spans
                if s.axis == first.axis
                and abs(s.a - first.a) <= dim_ends.SAME_PT
                and abs(s.b - second.b) <= dim_ends.SAME_PT
            ]
            if any(s.inches != total for s in overalls):
                continue  # the overall printed over the same ends is another size: unsure
            per_end = Fraction(1)
            cuts = getattr(page, "field_cuts", None)
            if cuts is not None:
                per_end = Fraction(getattr(cuts, "per_end", 1))
            out.append(
                VendorSink(
                    first,
                    second,
                    ends_first.low,
                    ends_second.high,
                    int("field_cut_end" in ends_first.low.kinds),
                    int("field_cut_end" in ends_second.high.kinds),
                    per_end,
                )
            )
    return out


def _chain(
    page: dim_ends.Page, axis: str, start: float, stop: float, rows: Sequence[float]
) -> tuple[Fraction, ...] | None:
    """The printed sizes from `start` to `stop` along `axis`, one dimension's end the next one's
    start, on rows of the stack (`rows`). `()` when the two are one place; `None`: no such chain."""
    if abs(stop - start) <= dim_ends.SAME_PT:
        return ()
    candidates = [
        s
        for s in page.spans.values()
        if s.axis == axis and any(abs(s.row - r) <= dim_ends.STACK_PT for r in rows)
    ]
    best: tuple[Fraction, ...] | None = None
    for row in sorted({round(s.row, 1) for s in candidates}):
        on_row = [s for s in candidates if abs(s.row - row) <= dim_ends.SAME_PT]
        place, parts = start, []
        for _step in range(len(on_row) + 1):
            if abs(place - stop) <= dim_ends.SAME_PT:
                break
            nxt = [s for s in on_row if abs(s.a - place) <= dim_ends.SAME_PT and s.b <= stop + 1]
            if len(nxt) != 1 or nxt[0].b <= place + dim_ends.SAME_PT:
                break  # two dimensions start here, or one too short to step past: unsure
            parts.append(nxt[0].inches)
            place = nxt[0].b
        if abs(place - stop) <= dim_ends.SAME_PT:
            found = tuple(parts)
            if best is not None and sum(best) != sum(found):
                return None  # two rows disagree: unsure
            best = found
    return best


def _across(span: dim_ends.Span, box: tuple[float, float, float, float]) -> float:
    """How far across from the dimension's line a box lies (0: the line runs through it)."""
    near, far = (box[1], box[3]) if span.axis == "h" else (box[0], box[2])
    return 0.0 if near <= span.row <= far else min(abs(span.row - near), abs(span.row - far))


def reference_bases(page: dim_ends.Page) -> list[Base]:
    """The page's sink bases drawn with the sink centred, each with its top (see the module)."""
    out: list[Base] = []
    for key, cabinet in page.cabinets.items():
        if not cabinet.sink_base:
            continue
        base = page.spans[key]
        centre = (base.a + base.b) / 2
        if page.ppi <= 0 or not any(
            abs(((c[0] + c[2]) / 2 if base.axis == "h" else (c[1] + c[3]) / 2) - centre)
            <= CENTRED_IN * page.ppi
            and _across(base, c) <= dim_ends.REACH_IN * page.ppi
            for c in page.cutouts
        ):
            continue  # the sink is not drawn centred on it (or not drawn): nothing to compare
        rows = [base.row] + [
            page.spans[k].row
            for k in page.cabinets
            if abs(page.spans[k].a - base.a) <= dim_ends.SAME_PT
            and abs(page.spans[k].b - base.b) <= dim_ends.SAME_PT
        ]
        tops: list[Base] = []
        for top in page.spans.values():
            if top.axis != base.axis or top.id in page.cabinets or top.inches <= base.inches:
                continue
            if not any(abs(top.row - r) <= dim_ends.STACK_PT for r in rows):
                continue
            if top.a > base.a + dim_ends.SAME_PT or top.b < base.b - dim_ends.SAME_PT:
                continue
            ends = page.ends(top.id)
            if ends is None or not (ends.low.kinds and ends.high.kinds):
                continue  # the top's two ends must both be named by code
            parts = _chain(page, base.axis, top.a, base.a, rows)
            if parts is None:
                continue
            from_low = sum(parts, Fraction(0)) + base.inches / 2
            high_parts = _chain(page, base.axis, base.b, top.b, rows)
            if high_parts is not None and sum(high_parts, Fraction(0)) + base.inches / 2 != (
                top.inches - from_low
            ):
                continue  # the printed sizes disagree with the top's: unsure
            tops.append(Base(base, cabinet.tag, top, ends.low, ends.high, from_low, parts))
        if len({(t.top.inches, t.from_low) for t in tops}) != 1:
            continue
        found = tops[0]
        if any(
            abs(other.base.a - base.a) <= dim_ends.SAME_PT
            and abs(other.base.b - base.b) <= dim_ends.SAME_PT
            and other.top.inches == found.top.inches
            and other.from_low == found.from_low
            for other in out
        ):
            continue  # the same base drawn again (its elevation and its plan)
        out.append(found)
    return out


def _side_words(span: dim_ends.Span, low: bool) -> str:
    if span.axis == "h":
        return "left" if low else "right"
    return "top" if low else "bottom"


def compare(vendor: VendorSink, ref: Base) -> Compared:
    """The vendor's sink centre against the reference's sink base centre (see the module)."""
    v_low, v_high = _way(vendor.low), _way(vendor.high)
    r_low, r_high = _way(ref.top_low), _way(ref.top_high)
    centred = ref.from_low == ref.from_high

    def fits(a: Way | None, b: Way | None) -> bool:
        return a is None or b is None or a == b

    def told(a: Way | None, b: Way | None) -> bool:
        return a is not None and a == b

    direct = fits(v_low, r_low) and fits(v_high, r_high)
    mirror = fits(v_low, r_high) and fits(v_high, r_low)
    direct_told = told(v_low, r_low) or told(v_high, r_high)
    mirror_told = told(v_low, r_high) or told(v_high, r_low)
    base = _inches(ref.base.inches)
    parts = " + ".join(_inches(p) for p in ref.parts_low)
    sum_words = f"{parts} + {base}/2" if parts else f"{base}/2"
    cut = vendor.per_end
    cut_ends = " and ".join(
        _side_words(vendor.first, low)
        for low, n in ((True, vendor.cuts_low), (False, vendor.cuts_high))
        if n
    )
    v_words = (
        f"sink centre {_inches(vendor.first.inches)} from the top's "
        f"{_side_words(vendor.first, True)} end and {_inches(vendor.second.inches)} from its "
        f"{_side_words(vendor.first, False)} end"
        + (f" (with the {_inches(cut)} field cut at its {cut_ends} end)" if cut_ends else "")
    )
    r_words = (
        f"sink drawn centred on the {base} sink base ({ref.tag}): its centre is "
        f"{_inches(ref.from_low)} from the top's {_side_words(ref.top, True)} end ({sum_words}) "
        f"and {_inches(ref.from_high)} from its {_side_words(ref.top, False)} end"
    )
    if vendor.design != ref.top.inches:
        return Compared(
            "unsure",
            f"Not compared: the tops are not the same length (the vendor's "
            f"{_inches(vendor.design)} without its field cut, the reference's "
            f"{_inches(ref.top.inches)}), so code cannot line the two sinks up",
            v_words,
            r_words,
            "the tops differ in length",
        )
    v_from_low = vendor.first.inches - cut * vendor.cuts_low

    def same_way(mirrored: bool) -> bool:
        return abs(v_from_low - (ref.from_high if mirrored else ref.from_low)) <= TOLERANCE

    if direct and mirror and not centred:
        # Both ways round fit what code knows of the ends: one is chosen only when the ends tell
        # it alone, or when both ways give the same answer; else nothing is claimed.
        if direct_told and not mirror_told:
            mirror = False
        elif mirror_told and not direct_told:
            direct = False
        elif same_way(False) != same_way(True):
            direct = mirror = False
    if direct:
        mirrored = False
    elif mirror:
        mirrored = True
    else:
        return Compared(
            "unsure",
            "Not compared: code could not tell which end of the vendor's top is which end of the "
            "reference's (a wall end or a free end)",
            v_words,
            r_words,
            "the ends are not known",
        )
    v_from_high = vendor.second.inches - cut * vendor.cuts_high
    r_for_low = ref.from_high if mirrored else ref.from_low
    r_for_high = ref.from_low if mirrored else ref.from_high
    difference = v_from_low - r_for_low
    # Said from the end that has no field cut (the same kind of end on both drawings).
    from_high = vendor.cuts_high == 0
    v_said = (v_from_high + cut * vendor.cuts_high) if from_high else vendor.first.inches
    r_said = (
        (r_for_high + cut * vendor.cuts_high) if from_high else (r_for_low + cut * vendor.cuts_low)
    )
    end_words = f"the top's {_side_words(vendor.first, not from_high)} end"
    flip = " (the vendor draws the unit reversed)" if mirrored else ""
    if abs(difference) <= TOLERANCE:
        why = (
            f"The sink is centred on the sink base: from {end_words}, the vendor's cut-out centre "
            f"is {_inches(v_said)} and the reference's sink base centre is {_inches(r_said)}"
            f"{flip}."
        )
        return Compared("same", why, v_words, r_words, why, v_said, r_said)
    off = _inches(abs(difference))
    why = (
        f"The vendor's sink centre is {off} off the sink base's centre: from {end_words}, the "
        f"vendor's cut-out centre is {_inches(v_said)}, the reference's sink base centre "
        f"{_inches(r_said)}{flip}."
    )
    return Compared("different", why, v_words, r_words, why, v_said, r_said)


# ---- one item, and the set ------------------------------------------------------------------------


def _box_of(span: dim_ends.Span, labels: Sequence[Mapping[str, Any]]) -> list[float] | None:
    for label in labels:
        if str(label.get("id")) == span.id and isinstance(label.get("box"), Sequence):
            return [float(v) for v in label["box"]]
    return None


def _record(
    vendor: VendorSink, ref: Base, found: Compared, vendor_page: int, ref_page: int
) -> dict[str, Any]:
    return {
        "outcome": found.outcome,
        "why": found.why,
        "vendor_page": vendor_page,
        "vendor_labels": [vendor.first.id, vendor.second.id],
        "reference_page": ref_page,
        "base": ref.base.id,
        "top": ref.top.id,
        "vendor": found.vendor,
        "reference": found.architect,
    }


def apply(finding: Mapping[str, Any], found: Compared, record: dict[str, Any]) -> dict[str, Any]:
    """The finding as this check says it (Right, a Likely problem, or Not compared with why)."""
    out = dict(finding)
    out.update(
        item=ITEM,
        vendor=found.vendor,
        architect=found.architect,
        source="architect",
        why=found.why,
        created_by=CREATED_BY,
    )
    out[FIELD] = record
    out.pop("question", None)
    out.pop(likely_problems.FIELD, None)
    out.pop(dim_ends.FIELD, None)
    if found.vendor_in is not None and found.architect_in is not None:
        out["inches"] = {
            "vendor": str(float(found.vendor_in)),
            "architect": str(float(found.architect_in)),
            "difference": str(float(abs(found.vendor_in - found.architect_in))),
        }
    else:
        out["inches"] = {"vendor": None, "architect": None, "difference": None}
    if found.outcome == "same":
        out["status"] = "same"
        out["settled"] = {
            "outcome": "right",
            "method": CREATED_BY,
            "how": found.why,
            "was": finding.get("status"),
        }
        out["compared"] = True
        out.pop("keep_reason", None)
        return out
    out["status"] = "check"
    out.pop("settled", None)
    if found.outcome == "different":
        out["keep_reason"] = "architect_prints_nothing"
        out[likely_problems.FIELD] = {
            "by": CREATED_BY,
            "kind": "size",
            "proof": "likely_only",
            "sure": "likely",
            "why": found.why,
            "words": f"{likely_problems.LETTER_LEAD}: {found.why}",
        }
    else:
        out["keep_reason"] = "other"
        out["gv_note"] = found.words
    return out


def _pairs(
    vendor_page: dim_ends.Page, ref_page: dim_ends.Page
) -> list[tuple[VendorSink, Base, Compared]]:
    sinks = vendor_sinks(vendor_page)
    bases = reference_bases(ref_page)
    if len(sinks) != 1 or len(bases) != 1:
        return []  # one sink each side only: which goes with which is otherwise a guess
    sink, base = sinks[0], bases[0]
    if sink.first.axis != base.base.axis:
        return []
    return [(sink, base, compare(sink, base))]


def route(
    finding: Mapping[str, Any], vendor_page: dim_ends.Page, ref_page: dim_ends.Page
) -> dict[str, Any] | None:
    """An item that pairs the vendor's sink-centre dimension with the reference's sink base (the
    escalation's like-for-like step found their ends unlike): the item this check makes of it, or
    `None` when the check has nothing to say (another item then speaks as before)."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    vendor_id = str(((dims or {}).get("vendor") or {}).get("id") or "")
    for sink, base, found in _pairs(vendor_page, ref_page):
        if vendor_id not in (sink.first.id, sink.second.id):
            continue
        return apply(finding, found, _record(sink, base, found, vendor_page.page, ref_page.page))
    return None


def same_sink(finding: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """Two items of this check about the same vendor sink."""
    one, two = finding.get(FIELD), other.get(FIELD)
    if not isinstance(one, Mapping) or not isinstance(two, Mapping):
        return False
    return one.get("vendor_page") == two.get("vendor_page") and set(
        one.get("vendor_labels") or ()
    ) == set(two.get("vendor_labels") or ())


def check_pack(
    folder: Path | None,
    uploads: Path | None,
    main_file: str | None,
    field_cuts: Callable[[str, int], Any] | None = None,
    category: str = "countertop",
    *,
    stop: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Every paired vendor drawing's sink centre against its reference drawing's sink base (the
    work list's pairs, `dims.json`, the uploads' line work): one item each where code can say."""
    if folder is None:
        return []
    worklist = import_module("app.review.assistant.simple_check.worklist")
    consistency = import_module("app.review.assistant.vendor_consistency")
    escalation = import_module("app.review.assistant.escalation")
    try:
        work = worklist.build_worklist(Path(folder), main_file)
    except (FileNotFoundError, ValueError):
        return []
    ctx = escalation.context_from_pack(Path(folder), main_file, field_cuts=field_cuts)
    segments = (
        consistency.line_work(uploads, Path(folder))
        if uploads is not None
        else dim_ends.kept_line_work(folder)
    )
    read = dim_ends.pages_reader(ctx.labels, ctx.phrases, segments, field_cuts, main_file)
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[str, ...]]] = set()
    for item in work.items:
        if stop is not None and stop():
            return []
        try:
            vendor_page = read(str(item.vendor.file), int(item.vendor.own_page))
            if vendor_page is None or not vendor_sinks(vendor_page):
                continue
            for other in item.architect:
                ref_page = read(str(other.file), int(other.own_page))
                if ref_page is None:
                    continue
                for sink, base, found in _pairs(vendor_page, ref_page):
                    key = (vendor_page.page, (sink.first.id, sink.second.id))
                    if key in seen or found.outcome == "unsure":
                        continue
                    seen.add(key)
                    labels = ctx.labels.get((vendor_page.file, vendor_page.page), ())
                    ref_labels = ctx.labels.get((ref_page.file, ref_page.page), ())
                    out.append(
                        _finding(
                            sink, base, found, vendor_page, ref_page, labels, ref_labels, category
                        )
                    )
        except Exception:  # noqa: BLE001 - a drawing code cannot read gives no finding
            _log.exception("sink centre: work item %s could not be checked", item.id)
    return out


def _finding(
    sink: VendorSink,
    base: Base,
    found: Compared,
    vendor_page: dim_ends.Page,
    ref_page: dim_ends.Page,
    labels: Sequence[Mapping[str, Any]],
    ref_labels: Sequence[Mapping[str, Any]],
    category: str,
) -> dict[str, Any]:
    escalation = import_module("app.review.assistant.escalation")
    said = sink.second if sink.cuts_high == 0 else sink.first
    mine = next((dict(l) for l in labels if str(l.get("id")) == said.id), {})
    theirs = next((dict(l) for l in ref_labels if str(l.get("id")) == base.base.id), {})
    box = _box_of(said, labels) or [0.0, 0.0, 0.0, 0.0]
    ref_box = _box_of(base.base, ref_labels) or [0.0, 0.0, 0.0, 0.0]
    finding: dict[str, Any] = {
        "page": vendor_page.page,
        "vs_page": ref_page.page,
        "category": category,
        "verified": False,
        "vendor_box": {
            "page": vendor_page.page,
            "x0": box[0],
            "y0": box[1],
            "x1": box[2],
            "y1": box[3],
            "file": vendor_page.file,
        },
        "arch_box": {
            "page": ref_page.page,
            "x0": ref_box[0],
            "y0": ref_box[1],
            "x1": ref_box[2],
            "y1": ref_box[3],
            "file": ref_page.file,
        },
        "dims": {
            "vendor": escalation.label_provenance(
                dict(mine, page=vendor_page.page, file=vendor_page.file)
            ),
            "architect": escalation.label_provenance(
                dict(theirs, page=ref_page.page, file=ref_page.file)
            ),
        },
        "ledger_ids": [said.id, base.base.id],
    }
    return apply(finding, found, _record(sink, base, found, vendor_page.page, ref_page.page))


def add_to_answer(engine: Any) -> dict[str, int] | None:
    """The engine's hook (a full check, at its finish, $0): each sink-centre item added through the
    finding book, unless an item of this check already speaks for that sink. Returns how many."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    book = engine.book
    found = check_pack(
        workspace.set_pack,
        getattr(workspace, "uploads", None),
        getattr(book, "main_file", None),
        getattr(book, "field_cuts", None),
        stop=getattr(getattr(engine, "cancel", None), "is_set", None),
    )
    if not found:
        return None
    trust = import_module("app.review.assistant.chat_trust")
    added_count = 0
    with engine.finding_lock:
        for finding in found:
            if any(same_sink(finding, other) for other in book.live()):
                continue
            added = book.add_code_finding(finding, created_by=CREATED_BY)
            if added is None:
                continue
            extra = {
                k: finding[k]
                for k in (
                    FIELD,
                    likely_problems.FIELD,
                    "settled",
                    "gv_note",
                    "keep_reason",
                    "inches",
                    "dims",
                )
                if k in finding
            }
            updated = trust.judged({**added, **extra})
            if book.replace(updated):
                added = updated
            engine._show_finding(added)
            added_count += 1
    return {"added": added_count}
