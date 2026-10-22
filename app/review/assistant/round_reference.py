"""A match with the vendor's own earlier round is never Right (Anant, 2026-10-22).

"If it is not matched with our rules or the architect's drawing, then it is wrong, no matter how
many rounds it took." A helper comparing a round-2 vendor drawing often takes the round-1 sheet as
its "architect" page and names a label on the vendor's own round-1 drawing there: a made-up
"45 11/16 = 45 11/16" then reads as Right while the architect prints another size. Such a match proves only that the
vendor did not change the value. It is kept as information ("Unchanged since round 1"), never as a
Right, and never counted as decided by itself.

Whose drawing a finding's reference is on (`reference_of`): code's own label side (`dims.json`),
else the page map's drawing holding the reference box. A reference on a vendor drawing is a round
match (`round_only`). **Only when code knows** (#1866): a label code read but could not place
("unknown") is nobody's, and so is a pasted picture with no GV heading, and a drawing on a page
whose sides code could not decide (`sides_unknown`) or only assumed for the full check (no
heading, every drawing "the vendor's"). A set with one round has no earlier round
(`earlier_round`): there a match with the vendor's other drawing is said as such, never as "the
vendor's earlier round".

Pure code, $0, run by the escalation before its free steps (`compare_with_architect`), on every
item whose reference is the vendor's own drawing:

1. **The round match is recorded** on the item (`ROUND_MATCH`): the page, the round and the label
   it was compared with, the value there, and whether code reads the same value (`unchanged`).
2. **The item is compared with the architect instead**, when code links the vendor's value to an
   architect label:
   - `carrier`: another item already compares the same vendor label with the architect's drawing:
     that item carries it (this one is folded into it; the carrier gets the round match);
   - `code_pair`: code paired the vendor label with an architect label by drawn position
     (`pairs.json`): the item is pointed at that label, the pairing proved by code;
   - `earlier_round`: the earlier round's label is the vendor side of another item compared with
     an architect label (or code paired it with one): the item is pointed at that architect label.
     The pairing is the AI's, so a red still needs the devil's advocate's proof.
   Code then compares the two values: equal is Right (code read both); different is a red
   candidate, judged like any other (every code check, the same-ends proof, the blind pick, the
   underside and room-end guards); a vendor repeating a value the architect does not print is still
   wrong on round 2, so its round status is "still wrong".
3. **Anything else stays as it is**: `classification.round_only` keeps it Not compared ("compared
   only with the vendor's earlier round"), with what would settle it.

The brand prototype is never a vendor round: a reference on it stays the prototype's rule (R1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

__all__ = [
    "ROUND_MATCH",
    "LinkResult",
    "applies",
    "compare_with_architect",
    "earlier_round",
    "match_words",
    "reference_of",
    "round_only",
    "set_rounds",
    "unchanged",
]

#: The finding field holding the round match (information only, set by code).
ROUND_MATCH: Final = "round_match"
#: The sources whose reference side must be the architect's (or the brand prototype's, R1).
_COMPARED_SOURCES: Final = frozenset({"architect", "brand_prototype", "gv_review"})
#: Items code made itself with the vendor's own drawing as their reference (a span printed twice,
#: GV's rules, code's own pairs): never round matches.
_CODE_MADE: Final = frozenset(
    {"consistency", "filler_range", "run_fit", "sink_offset", "pairs", "architect_items"}
)
#: Two printed values this close are the same (inches): the drawings' smallest step, halved.
_SAME_IN: Final = Decimal(1) / Decimal(32)

type Role = Literal["vendor", "architect", "brand"]
type Link = Literal["carrier", "code_pair", "earlier_round"]


@dataclass(frozen=True, slots=True)
class Reference:
    """Where a finding's reference side is: whose drawing, its round, its page and label."""

    role: Role
    page: int | None
    round: int | None
    label: Mapping[str, Any] | None


@dataclass(slots=True)
class LinkResult:
    """What `compare_with_architect` did to an answer's findings."""

    changed: list[dict[str, Any]] = field(default_factory=list)
    absorbed: dict[str, str] = field(default_factory=dict)
    """Items folded into the item that compares the same vendor label with the architect."""
    linked: dict[str, Link] = field(default_factory=dict)
    """Per finding id: how code compared it with the architect instead."""
    marked: int = 0
    """Items whose reference is the vendor's own drawing."""


# ---- small readers ------------------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _number(value: Decimal) -> str:
    """Inches as code's other fields write them (`4.375`, never `4.3750` or `-0`)."""
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ref(label: object) -> str | None:
    if not isinstance(label, Mapping) or not label.get("id"):
        return None
    return f"p{label.get('page')}.{label.get('id')}"


def _dims(finding: Mapping[str, Any]) -> Mapping[str, Any]:
    dims = finding.get("dims")
    return dims if isinstance(dims, Mapping) else {}


def _box(raw: object) -> tuple[str | None, int | None, list[float]] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        box = [float(raw[key]) for key in ("x0", "y0", "x1", "y1")]
    except (KeyError, TypeError, ValueError):
        return None
    name = raw.get("file")
    return (None if name is None else str(name)), _int(raw.get("page")), box


def _role(raw: object) -> Role | None:
    word = str(raw or "")
    if word == "brand_prototype":
        return "brand"
    return word if word in ("vendor", "architect", "brand") else None  # type: ignore[return-value]


def _entry_half(
    page_map: Mapping[str, Any] | None, file: str | None, page: int, box: Sequence[float]
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """The page map's page and its drawing (`halves`) holding the centre of `box`."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for entry in (page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or entry.get("page") != page:
            continue
        if file is not None and entry.get("file") not in (None, file):
            continue
        for half in entry.get("halves") or []:
            where = half.get("box") if isinstance(half, Mapping) else None
            if isinstance(where, Sequence) and len(where) == 4:
                x0, y0, x1, y1 = (float(v) for v in where)
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    return (entry, half) if isinstance(half, Mapping) else None
    return None


def _half(
    page_map: Mapping[str, Any] | None, file: str | None, page: int, box: Sequence[float]
) -> Mapping[str, Any] | None:
    """The page map's drawing (`halves`) holding the centre of `box`."""
    found = _entry_half(page_map, file, page, box)
    return None if found is None else found[1]


def _sides_known(entry: Mapping[str, Any]) -> bool:
    """Whether code knows whose drawing is whose on the page (#1866): never on a page whose sides
    code could not decide (`sides_unknown`: the content reader timed out or could not read it),
    nor on one where every drawing's role is only code's own assumption for the full check (a
    page with no heading, "assumed the vendor's drawing, checked": the plan's guess that the page
    is to be checked, not a reading of which drawing is the architect's)."""
    if entry.get("sides_unknown"):
        return False
    halves = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
    return not halves or any(h.get("heading_by") != "code" for h in halves)


def _in_picture(entry: Mapping[str, Any], box: Sequence[float]) -> bool:
    """Whether the centre of `box` lies in one of the page's pasted pictures."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for picture in entry.get("pictures") or []:
        where = picture.get("box") if isinstance(picture, Mapping) else picture
        if isinstance(where, Sequence) and len(where) == 4:
            try:
                x0, y0, x1, y1 = (float(v) for v in where)
            except (TypeError, ValueError):
                continue
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return True
    return False


def set_rounds(page_map: Mapping[str, Any] | None) -> int:
    """How many vendor rounds the set has (the distinct rounds of its vendor drawings; a set
    whose drawings say no round has one)."""
    rounds = {
        half.get("round")
        for entry in (page_map or {}).get("pages") or []
        if isinstance(entry, Mapping)
        for half in entry.get("halves") or []
        if isinstance(half, Mapping)
        and half.get("role") == "vendor"
        and isinstance(half.get("round"), int)
    }
    return max(len(rounds), 1)


def earlier_round(match: Mapping[str, Any]) -> bool:
    """Whether the round match is with an earlier round of the vendor's drawings: never in a set
    with one round (#1866: one round has no earlier round, by definition); else when the two
    drawings' rounds say so, or (a match kept before `set_rounds` was recorded) as before."""
    rounds = _int(match.get("set_rounds"))
    if rounds is not None and rounds < 2:
        return False
    earlier, own = _int(match.get("round")), _int(match.get("vendor_round"))
    if earlier is not None and own is not None:
        return earlier < own
    return True


def _round_at(
    page_map: Mapping[str, Any] | None, file: str | None, page: int | None, box: object
) -> int | None:
    if page is None or not isinstance(box, Sequence) or len(box) != 4:
        return None
    half = _half(page_map, file, page, [float(v) for v in box])
    return _int(half.get("round")) if half is not None else None


# ---- whose drawing -----------------------------------------------------------------------------


def applies(finding: Mapping[str, Any]) -> bool:
    """A comparison between two drawings whose reference must be the architect's: a helper's or
    GV's recorded item, not one code made with the vendor's own drawing as its reference."""
    return (
        str(finding.get("source") or "") in _COMPARED_SOURCES
        and finding.get("status") in ("same", "different", "check")
        and finding.get("paired_by") != "code"
        and str(finding.get("created_by") or "") not in _CODE_MADE
    )


def reference_of(
    finding: Mapping[str, Any],
    page_map: Mapping[str, Any] | None = None,
    main_file: str | None = None,
) -> Reference | None:
    """Whose drawing the finding's reference side is on: code's label side when the reference is a
    label, else the page map's drawing holding the reference box (`None` when neither says)."""
    label = _dims(finding).get("architect")
    if isinstance(label, Mapping) and _role(label.get("side")) is not None:
        page = _int(label.get("page"))
        file = label.get("file") or main_file
        return Reference(
            _role(label.get("side")),  # type: ignore[arg-type]
            page,
            _round_at(page_map, None if file is None else str(file), page, label.get("box")),
            label,
        )
    if isinstance(label, Mapping) and label.get("side"):
        # Code read the label and could not tell whose drawing it is on (a label inside a
        # pasted picture, or inside both drawings): the drawing around it says no more (#1866).
        return None
    where = _box(finding.get("arch_box"))
    if where is None or page_map is None:
        return None
    name, page, box = where
    page = page if page is not None else _int(finding.get("vs_page"))
    if page is None:
        return None
    found = _entry_half(page_map, name or main_file, page, box)
    if found is None or not _sides_known(found[0]):
        return None
    half = found[1]
    if not half.get("picture") and _in_picture(found[0], box):
        # A pasted picture with no GV heading is nobody's (`set_pack`), whatever drawing the
        # page map puts around it (#1866: the architect's picture on a vendor's whole page).
        return None
    role = _role(half.get("role"))
    if role is None:
        return None
    return Reference(role, page, _int(half.get("round")), None)


def round_only(finding: Mapping[str, Any]) -> bool:
    """Whether the item was compared only with the vendor's own drawing (an earlier round, or
    another vendor drawing), never with the architect's: never Right (`classification`). Code's
    label side says so, or the round match code recorded while no architect label was found."""
    if not applies(finding):
        return False
    label = _dims(finding).get("architect")
    if isinstance(label, Mapping) and label.get("side"):
        return _role(label.get("side")) == "vendor"
    match = finding.get(ROUND_MATCH)
    return isinstance(match, Mapping) and not match.get("compared")


def unchanged(finding: Mapping[str, Any]) -> bool:
    """The item carries a match with an earlier round whose value code reads the same
    (information only; never in a set with one round, `earlier_round`)."""
    match = finding.get(ROUND_MATCH)
    return isinstance(match, Mapping) and match.get("unchanged") is True and earlier_round(match)


def match_words(match: Mapping[str, Any]) -> str:
    """The round match as the screen and the tracker say it: "Unchanged since round 1", "Changed
    since round 1 (was 30")", or the vendor's other drawing on the same round."""
    earlier, own = _int(match.get("round")), _int(match.get("vendor_round"))
    page = match.get("page")
    if earlier is not None and own is not None and earlier < own and earlier_round(match):
        if match.get("unchanged"):
            return f"Unchanged since round {earlier}"
        return f"Changed since round {earlier} (was {match.get('value')})"
    where = f" on page {page}" if page is not None else ""
    if match.get("unchanged"):
        return f"Same as the vendor's other drawing{where}"
    return f"Differs from the vendor's other drawing{where} ({match.get('value')})"


# ---- comparing with the architect instead -------------------------------------------------------


def _record(
    finding: Mapping[str, Any],
    reference: Reference,
    page_map: Mapping[str, Any] | None,
    main_file: str | None,
) -> dict[str, Any]:
    """The round match of an item whose reference is the vendor's own drawing."""
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
    vendor, earlier = (
        _decimal((inches or {}).get("vendor")),
        _decimal((inches or {}).get("architect")),
    )
    if reference.label is not None:
        earlier = _decimal(reference.label.get("inches")) or earlier
    same = vendor is not None and earlier is not None and abs(vendor - earlier) <= _SAME_IN
    if vendor is None and earlier is None:
        # A tag or a note (no number): the helper's own "same" is all there is.
        same = finding.get("status") == "same"
    label = _dims(finding).get("vendor")
    vendor_box = finding.get("vendor_box")
    if isinstance(label, Mapping):
        file = label.get("file") or main_file
        vendor_round = _round_at(
            page_map, None if file is None else str(file), _int(label.get("page")), label.get("box")
        )
    else:
        where = _box(vendor_box)
        vendor_round = (
            None
            if where is None
            else _round_at(
                page_map, where[0] or main_file, where[1] or _int(finding.get("page")), where[2]
            )
        )
    match: dict[str, Any] = {
        "page": reference.page,
        "round": reference.round,
        "vendor_round": vendor_round,
        "label": None if reference.label is None else reference.label.get("id"),
        "value": (
            str(reference.label.get("text"))
            if reference.label is not None and reference.label.get("text")
            else str(finding.get("architect") or "")
        ),
        "unchanged": same,
        "compared": None,
        "set_rounds": set_rounds(page_map),
    }
    match["words"] = match_words(match)
    return match


def _architect_label(
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    ref: str | None,
    main_file: str | None,
) -> dict[str, Any] | None:
    """The set pack's architect label `p57.D21` names (`None` unless it is on the architect's
    drawing)."""
    if not ref or not ref.startswith("p") or ".D" not in ref:
        return None
    page_text, label_id = ref[1:].split(".", 1)
    try:
        page = int(page_text)
    except ValueError:
        return None
    for (file, number), page_labels in labels.items():
        if number != page or (main_file is not None and file != main_file):
            continue
        for label in page_labels:
            if label.get("id") == label_id and label.get("side") == "architect":
                return dict(label, page=page, file=file)
    return None


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
        **({"origin": label["origin"]} if label.get("origin") else {}),
    }


def _point_at(
    finding: dict[str, Any], label: Mapping[str, Any], how: Link, via: str, *, by_code: bool
) -> None:
    """The item compared with the architect's `label` instead of the vendor's earlier round: its
    architect side becomes the label (code's reading of it), and code compares the two values."""
    from app.review.assistant.dim_findings import label_checks

    match = dict(finding.get(ROUND_MATCH) or {})
    match.update(compared=how, via=via, architect=_ref(label))
    x0, y0, x1, y1 = (float(v) for v in label.get("box") or (0, 0, 0, 0))
    finding["architect"] = str(label.get("text") or "")
    finding["arch_box"] = {
        "page": int(label.get("page") or 0),
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "file": label.get("file"),
    }
    finding["vs_page"] = int(label.get("page") or 0)
    finding["arch_picture_id"] = None  # the crop was of the vendor's earlier round
    dims = dict(_dims(finding))
    dims["architect"] = _provenance(label)
    finding["dims"] = dims
    inches = dict(finding.get("inches") or {})
    architect, vendor = _decimal(label.get("inches")), _decimal(inches.get("vendor"))
    inches["architect"] = None if architect is None else str(architect)
    inches["difference"] = (
        None if architect is None or vendor is None else _number(vendor - architect)
    )
    finding["inches"] = inches
    checks = {
        key: value
        for key, value in dict(finding.get("checks") or {}).items()
        if key not in ("verify_architect", "printed_architect", "critic", "second_read")
    }
    checks["second_read"] = "not_run"
    finding["checks"] = label_checks(finding, checks)
    for stale in ("critic", "pairing", "settled", "keep_reason", "question", "compared"):
        finding.pop(stale, None)
    same = vendor is not None and architect is not None and abs(vendor - architect) <= _SAME_IN
    finding["status"] = "same" if same else "different"
    if by_code:
        finding["paired_by"] = "code"
    if not same and match.get("unchanged") and earlier_round(match):
        # The vendor repeats, on its newest round, a value the architect does not print.
        finding["round_status"] = "still_wrong"
    match["words"] = match_words(match)
    finding[ROUND_MATCH] = match


def _against_architect(finding: Mapping[str, Any], page_map: Any, main_file: str | None) -> bool:
    """The item's reference is a label on the architect's drawing (not a round match itself)."""
    reference = reference_of(finding, page_map, main_file)
    return (
        reference is not None
        and reference.role == "architect"
        and reference.label is not None
        and finding.get("status") in ("same", "different", "check")
    )


def compare_with_architect(
    findings: Sequence[Mapping[str, Any]],
    *,
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    page_map: Mapping[str, Any] | None,
    pairs: Mapping[str, str],
    main_file: str | None,
) -> LinkResult:
    """Every item whose reference is the vendor's own drawing (see the module): its round match
    recorded, and compared with the architect where code links it to an architect label. Pure:
    returns what changed (the findings in it are copies)."""
    result = LinkResult()
    live = [dict(f) for f in findings]
    by_vendor: dict[str, list[dict[str, Any]]] = {}
    for other in live:
        ref = _ref(_dims(other).get("vendor"))
        if ref is not None and _against_architect(other, page_map, main_file):
            by_vendor.setdefault(ref, []).append(other)
    carriers_changed: dict[str, dict[str, Any]] = {}
    for finding in live:
        if not applies(finding) or str(finding.get("id")) in result.absorbed:
            continue
        known = finding.get(ROUND_MATCH)
        if isinstance(known, Mapping) and known.get("compared"):
            continue  # compared with the architect already (a second escalation pass)
        reference = reference_of(finding, page_map, main_file)
        if reference is None or reference.role != "vendor":
            continue
        own = _dims(finding).get("vendor")
        if isinstance(own, Mapping) and own.get("side") not in (None, "", "vendor"):
            continue  # the "vendor" value is not on a vendor drawing: not a round match
        result.marked += 1
        match = (
            dict(known)
            if isinstance(known, Mapping)
            else _record(finding, reference, page_map, main_file)
        )
        changed = known != match
        finding[ROUND_MATCH] = match
        vendor_ref = _ref(_dims(finding).get("vendor"))
        earlier_ref = _ref(reference.label)
        carrier = next(
            (
                other
                for other in by_vendor.get(vendor_ref or "", [])
                if other.get("id") != finding.get("id")
                and str(other.get("id")) not in result.absorbed
            ),
            None,
        )
        if carrier is not None:
            result.absorbed[str(finding["id"])] = (
                f"Same vendor dimension as {carrier.get('id')} (page {carrier.get('page')}), "
                "which compares it with the architect's drawing; this one was compared only with "
                f"the vendor's own drawing on page {match.get('page')} ({match_words(match)})."
            )
            if not isinstance(carrier.get(ROUND_MATCH), Mapping):
                carrier[ROUND_MATCH] = {
                    **match,
                    "compared": "carrier",
                    "via": str(finding.get("id")),
                    "words": match_words(match),
                }
                carriers_changed[str(carrier["id"])] = carrier
            result.linked[str(finding["id"])] = "carrier"
            continue
        paired = _architect_label(labels, pairs.get(vendor_ref or ""), main_file)
        if paired is not None:
            _point_at(finding, paired, "code_pair", f"code paired {vendor_ref}", by_code=True)
            result.linked[str(finding["id"])] = "code_pair"
            result.changed.append(finding)
            continue
        earlier = next(
            (
                other
                for other in by_vendor.get(earlier_ref or "", [])
                if other.get("id") != finding.get("id")
            ),
            None,
        )
        label = (
            _architect_label(labels, _ref(_dims(earlier).get("architect")), main_file)
            if earlier is not None
            else _architect_label(labels, pairs.get(earlier_ref or ""), main_file)
        )
        if earlier is not None and label is not None:
            from app.review.assistant.proof import proof_of

            settled = proof_of(earlier)
            if settled.get("group") not in ("right", "clouded"):
                # The earlier round's own comparison with the architect is not proved (a pairing
                # code could not prove, a pick that disagreed): the item says so, and is never
                # given a second chance to turn red on the same unproved pairing.
                match["earlier_item"] = str(earlier.get("id"))
                match["earlier_reason"] = settled.get("reason")
                if match != known:
                    finding[ROUND_MATCH] = match
                    result.changed.append(finding)
                continue
        if label is not None:
            via = (
                f"{earlier.get('id')} compares {earlier_ref} with {_ref(label)}"
                if earlier is not None
                else f"code paired {earlier_ref} with {_ref(label)}"
            )
            _point_at(finding, label, "earlier_round", via, by_code=False)
            result.linked[str(finding["id"])] = "earlier_round"
            result.changed.append(finding)
            continue
        if changed:
            result.changed.append(finding)
    result.changed.extend(carriers_changed.values())
    return result
