"""Layer 2 for the round-only items: a cheap pick names the architect's label (#1861).

Anant, 2026-10-22: "go with hybrid, with both, but less cost". An item a helper compared only with
the vendor's own earlier round is never Right (`round_reference`, #1852). Layer 1 is free: the label
tool tells the helpers to give the architect's label, and code links what it can (`carrier`,
`code_pair`, `earlier_round`). This module is layer 2, for the leftovers only:

- **One question per vendor label** (`questions`): the items still compared only with the vendor's
  own drawing, the biggest first (`impact.by_impact`), grouped by the vendor label they print. Each
  question shows the vendor's drawing with that label marked and the architect's drawing of the
  same area with every label numbered, and asks the devil's advocate's blind question (the
  roster's blind-pick machinery, `full_check_advocate.advocate_question`: the item's name without
  its numbers, #1833): "which numbered dimension measures the same thing as the marked one?", or
  none. The model is the cheap one (`CHEAP_MODEL`, `CHEAP_EFFORT`); a pick already decided on the
  same drawings is reused at $0 (`pick_cache`, `blind_picks.json`).
- **The AI only names a label** (`link`). Code does the rest, exactly as for any pair: the label
  must be on the architect's drawing, the pick's own words must not say the two dimensions end in
  different places or that the vendor value is in colour, and every advocate code check must pass
  (GV's markup and notes, the roles, the direction, the newest round, the units, a slip). Then the
  item is compared with that label (`round_reference.point_at`): equal values are Right (code read
  both); a different value is a red candidate, judged by the devil's advocate like any other.
- **A cheap pick never makes a red alone** (`cheap_linked`): for such a candidate the devil's
  advocate always asks the stronger model at high effort (`full_check_advocate.STRONG_PICK`),
  which must name the same label and the same ends, and code's geometry must not show different
  points; the underside / top-face (#1837) and room-end (#1827) guards hold as for any pick. Only
  these would-be reds ever reach the stronger model.
- **Anything else stays Not compared**, with what happened in its reason (`ROUND_PICK` on the
  round match: no label, a label code refused, or not asked within the cap).

The money (a hard cap per run, `GV_ASSISTANT_ROUND_PICK_CAP_USD`, reserved inside the fixed price,
kept picks first, the biggest items first) is the roster's (`full_check_roster`). Pure code here: no
model call, no PDF.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any, Final, Literal

from app.review.assistant.impact import by_impact
from app.review.assistant.round_reference import (
    ROUND_MATCH,
    architect_label,
    point_at,
    round_only,
)

__all__ = [
    "CHEAP_EFFORT",
    "CHEAP_LINK",
    "CHEAP_MODEL",
    "ROUND_PICK",
    "Question",
    "architect_drawing",
    "cheap_linked",
    "crop_dpi",
    "first_spans",
    "link",
    "pick_words",
    "prompt_text",
    "questions",
    "vendor_label",
    "waiting",
]

#: The round match's record of the cheap pick (set by code).
ROUND_PICK: Final = "round_pick"
#: How the item was compared with the architect (`round_match.compared`).
CHEAP_LINK: Final = "round_pick"
#: The cheap model and its effort (the devil's advocate's everyday pick: its kept picks are shared).
CHEAP_MODEL: Final = "sonnet"
CHEAP_EFFORT: Final = "medium"
#: The cheap pick's pictures at most this many pixels on their long edge ("small crops"): the
#: devil's advocate's are up to 2,576 (about 5,500 picture tokens each); the numbered labels are
#: listed as text beside the picture too, so the smaller picture costs about a third.
CROP_EDGE_PX: Final = 1_568
#: The least dpi a crop is drawn at (`set_pack.MIN_CROP_DPI`).
_MIN_DPI: Final = 72
#: Two printed values this close are the same value (inches).
_SAME_IN: Final = Decimal(1) / Decimal(32)

type Outcome = Literal["linked", "none", "refused", "not_asked"]
#: (vendor label, architect label) -> the advocate's code checks (`full_check_advocate.Check`).
type Checks = Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[Any]]


@dataclass(slots=True)
class Question:
    """One cheap question: a vendor label, the architect drawing it is asked against, and the
    findings (biggest first) that print that vendor label."""

    vendor: dict[str, Any]
    architect: dict[str, Any]
    """The architect drawing shown: its file, page and box (`box` is the drawing's own box)."""
    findings: list[str] = field(default_factory=list)
    item: str = ""

    @property
    def ref(self) -> str:
        return f"p{self.vendor.get('page')}.{self.vendor.get('id')}"


# ---- small readers ------------------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _match(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    match = finding.get(ROUND_MATCH)
    return match if isinstance(match, Mapping) else None


def _box(raw: object) -> list[float] | None:
    if isinstance(raw, Mapping):
        raw = [raw.get(key) for key in ("x0", "y0", "x1", "y1")]
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        return [float(v) for v in raw]
    except (TypeError, ValueError):
        return None


def _centre_in(label: Mapping[str, Any], box: Sequence[float]) -> bool:
    own = _box(label.get("box"))
    if own is None:
        return False
    cx, cy = (own[0] + own[2]) / 2, (own[1] + own[3]) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _page_labels(
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    file: str | None,
    page: int,
) -> tuple[str | None, list[Mapping[str, Any]]]:
    """The labels code read on one page (and its file), any file when `file` is not known."""
    for (name, number), found in labels.items():
        if number == page and (file is None or name == file):
            return name, list(found)
    return file, []


def crop_dpi(box: Sequence[float]) -> int:
    """The dpi that draws `box` (points) at most `CROP_EDGE_PX` on its long edge."""
    side = max(float(box[2]) - float(box[0]), float(box[3]) - float(box[1]), 1.0)
    return max(_MIN_DPI, int(CROP_EDGE_PX * 72 // side))


def prompt_text(base: str) -> str:
    """The cheap pick's fixed text in its kept-pick key (`pick_cache.pick_key`): the advocate's
    question and the smaller pictures, so a cheap pick is never mistaken for the advocate's."""
    return f"{base}\nround pick, pictures up to {CROP_EDGE_PX} px"


# ---- which items wait for a pick ----------------------------------------------------------------


def waiting(finding: Mapping[str, Any]) -> bool:
    """An item still compared only with the vendor's own drawing after the free steps, never asked
    yet, whose vendor value is a number: a leftover for the cheap pick. An item whose earlier
    round's own comparison with the architect was not proved (`earlier_item`) is never asked
    again: that pairing gets no second chance to turn red (#1852)."""
    match = _match(finding)
    if match is None or match.get("compared") or match.get("earlier_item"):
        return False
    said = match.get(ROUND_PICK)
    if isinstance(said, Mapping) and said.get("outcome") != "not_asked":
        return False  # decided once: a Continue asks again only what the cap did not reach
    if not round_only(finding):
        return False
    inches = finding.get("inches")
    return isinstance(inches, Mapping) and _decimal(inches.get("vendor")) is not None


def cheap_linked(finding: Mapping[str, Any]) -> bool:
    """The item was compared with the architect through a cheap pick: its red needs the stronger
    pick (and every other proof), never the cheap one alone."""
    match = _match(finding)
    return match is not None and match.get("compared") == CHEAP_LINK


def vendor_label(
    finding: Mapping[str, Any],
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    main_file: str | None,
) -> dict[str, Any] | None:
    """The vendor label code read for the item: its own (`dims.vendor`), else the only label on
    the vendor's drawing inside its box that prints its value. `None`: nothing to mark."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    own = (dims or {}).get("vendor")
    if isinstance(own, Mapping) and own.get("id") and _int(own.get("page")) is not None:
        file, found = _page_labels(labels, own.get("file") or main_file, int(own["page"]))
        for label in found:
            if label.get("id") == own.get("id") and label.get("side") == "vendor":
                return dict(label, file=file, page=int(own["page"]))
        return None
    box = finding.get("vendor_box")
    where = _box(box)
    want = _decimal((finding.get("inches") or {}).get("vendor"))
    if where is None or want is None or not isinstance(box, Mapping):
        return None
    page = _int(box.get("page")) or _int(finding.get("page"))
    if page is None:
        return None
    file, found = _page_labels(labels, box.get("file") or main_file, page)
    hits = [
        label
        for label in found
        if label.get("side") == "vendor"
        and _centre_in(label, where)
        and (value := _decimal(label.get("inches"))) is not None
        and abs(value - want) <= _SAME_IN
    ]
    return dict(hits[0], file=file, page=page) if len(hits) == 1 else None


def _entries(page_map: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return [e for e in (page_map or {}).get("pages") or [] if isinstance(e, Mapping)]


def architect_drawing(
    vendor: Mapping[str, Any],
    finding: Mapping[str, Any],
    *,
    page_map: Mapping[str, Any] | None,
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    main_file: str | None,
) -> dict[str, Any] | None:
    """The architect drawing the question is asked against: an architect drawing of the vendor
    page's area (the page map) holding at least one architect label: first on the page of the
    earlier round the helper compared with (where the helper was looking), then on the vendor's
    page, then the nearest page of the area. `None` when code knows of none."""
    page = _int(vendor.get("page"))
    if page is None or page_map is None:
        return None
    file = str(vendor.get("file") or main_file or "") or None
    resolver = import_module("app.review.assistant.decision_resolver")
    areas: Mapping[int, str] = resolver.page_areas(page_map, file)
    area = areas.get(page)
    pages = sorted({p for p, key in areas.items() if key == area} | {page}) if area else [page]
    match = _match(finding) or {}
    first = [p for p in (_int(match.get("page")), page) if p is not None and p in pages]
    ordered = list(dict.fromkeys([*first, *sorted(pages, key=lambda p: (abs(p - page), p))]))
    for number in ordered:
        for entry in _entries(page_map):
            if entry.get("page") != number:
                continue
            if file is not None and entry.get("file") not in (None, file):
                continue
            name, found = _page_labels(labels, file, number)
            for half in entry.get("halves") or []:
                if not isinstance(half, Mapping) or half.get("role") != "architect":
                    continue
                box = _box(half.get("box"))
                if box is None:
                    continue
                if any(
                    label.get("side") == "architect" and _centre_in(label, box) for label in found
                ):
                    return {
                        "file": name,
                        "page": number,
                        "id": "",
                        "text": "",
                        "box": box,
                        "side": "architect",
                    }
    return None


def questions(
    findings: Sequence[Mapping[str, Any]],
    *,
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    page_map: Mapping[str, Any] | None,
    main_file: str | None,
) -> list[Question]:
    """The cheap questions for the leftovers (`waiting`), biggest first: one per vendor label,
    holding every finding that prints it. An item code can mark no vendor label for, or find no
    architect drawing of its area for, is never asked (it stays Not compared as it is)."""
    out: dict[str, Question] = {}
    for finding in by_impact([f for f in findings if waiting(f)]):
        vendor = vendor_label(finding, labels, main_file)
        if vendor is None:
            continue
        ref = f"p{vendor.get('page')}.{vendor.get('id')}"
        if ref in out:
            out[ref].findings.append(str(finding.get("id")))
            continue
        drawing = architect_drawing(
            vendor, finding, page_map=page_map, labels=labels, main_file=main_file
        )
        if drawing is None:
            continue
        out[ref] = Question(
            vendor=vendor,
            architect=drawing,
            findings=[str(finding.get("id"))],
            item=str(finding.get("item") or ""),
        )
    return list(out.values())


# ---- what code does with the pick ---------------------------------------------------------------


def _record(
    finding: Mapping[str, Any], outcome: Outcome, why: str, said: Mapping[str, Any]
) -> dict[str, Any]:
    out = dict(finding)
    match = dict(_match(finding) or {})
    match[ROUND_PICK] = {"outcome": outcome, "why": why, **said}
    out[ROUND_MATCH] = match
    return out


def link(
    finding: Mapping[str, Any],
    answer: Mapping[str, Any] | None,
    *,
    vendor: Mapping[str, Any],
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    main_file: str | None,
    checks: Checks,
    said: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Outcome]:
    """The item with code's answer to the cheap pick (`answer`: the blind pick's reply, its `pick`
    set to a label ref such as `p4.D12`, or `None` when it was not asked within the cap). `said`:
    what to keep about the pick (model, cost, kept or asked). Returns a copy of the finding and
    the outcome: `linked` (compared with the architect's label: same is Right, different is a red
    candidate for the devil's advocate), or the reason it stays Not compared."""
    kept = dict(said or {})
    if answer is None:
        return _record(finding, "not_asked", "not asked within the round-pick cap", kept), (
            "not_asked"
        )
    pick = str(answer.get("pick") or "none")
    kept["pick"] = pick
    if pick == "none":
        why = "a blind pick found no architect dimension that measures the same thing"
        return _record(finding, "none", why, kept), "none"
    label = architect_label(labels, pick, main_file)
    if label is None:
        why = f"a blind pick named {pick}, which is not a dimension on the architect's drawing"
        return _record(finding, "refused", why, kept), "refused"
    if answer.get("vendor_ink_black") == "no":
        why = f"a blind pick named {pick}, but it sees the vendor value in colour (GV's markup)"
        return _record(finding, "refused", why, kept), "refused"
    vendor_spans, architect_spans = answer.get("vendor_spans"), answer.get("architect_spans")
    if vendor_spans != architect_spans:
        why = (
            f"a blind pick named {pick}, but says their ends land in different places "
            f"({_ends(vendor_spans)} vs {_ends(architect_spans)})"
        )
        return _record(finding, "refused", why, kept), "refused"
    failed = [c for c in checks(vendor, label) if getattr(c, "outcome", None) != "pass"]
    if failed:
        first = failed[0]
        reason = getattr(first, "why", "") or f"the {getattr(first, 'name', 'code')} check failed"
        why = f"a blind pick named {pick}, but {reason}"
        return _record(finding, "refused", why, kept), "refused"
    kept["spans"] = [vendor_spans, architect_spans]
    out = dict(finding)
    dims = dict(out.get("dims") or {}) if isinstance(out.get("dims"), Mapping) else {}
    if not isinstance(dims.get("vendor"), Mapping) or not dims["vendor"].get("id"):
        # The vendor's label code marked for the question becomes the item's own label.
        dims["vendor"] = _provenance(vendor)
        out["dims"] = dims
    point_at(out, label, CHEAP_LINK, f"a blind pick named {pick}", by_code=False)
    match = dict(out[ROUND_MATCH])
    match[ROUND_PICK] = {"outcome": "linked", "why": f"a blind pick named {pick}", **kept}
    out[ROUND_MATCH] = match
    return out, "linked"


def _provenance(label: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(label.get("id")),
        "page": int(label.get("page") or 0),
        "file": label.get("file"),
        "text": str(label.get("text") or ""),
        "inches": str(label.get("inches")),
        "route": "drawn" if label.get("route") == "drawn" else "text",
        "side": label.get("side"),
        "orientation": label.get("orientation"),
        "box": [float(v) for v in label.get("box") or (0, 0, 0, 0)],
    }


def _ends(spans: object) -> str:
    if isinstance(spans, Mapping):
        return f"{spans.get('from')} to {spans.get('to')}"
    return "unknown"


def first_spans(finding: Mapping[str, Any]) -> tuple[object, object] | None:
    """Where the cheap pick said the two dimensions' ends land (vendor's, architect's), for an
    item it compared with the architect: the stronger pick must say the same (`decide`)."""
    match = _match(finding)
    said = match.get(ROUND_PICK) if match is not None else None
    if not cheap_linked(finding) or not isinstance(said, Mapping):
        return None
    spans = said.get("spans")
    if isinstance(spans, Sequence) and not isinstance(spans, str) and len(spans) == 2:
        return (spans[0], spans[1])
    return None


def pick_words(match: Mapping[str, Any]) -> str | None:
    """The Not-compared reason's tail for an item the cheap pick did not compare (`None`: it was
    never asked, or it was compared)."""
    said = match.get(ROUND_PICK)
    if not isinstance(said, Mapping) or said.get("outcome") == "linked":
        return None
    why = str(said.get("why") or "").strip()
    return why or None
