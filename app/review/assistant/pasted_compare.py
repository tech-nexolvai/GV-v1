"""What the reference's pasted views settle, like for like (code, $0, no AI; `pasted_views`).

The escalation's step (`escalation._pasted_step`) runs this on every open item of a vendor page
paired with a reference sheet that holds pasted drawings (`pasted_views.View`). Three kinds of item:

- **The sink model** (the item names a sink and the vendor's side one product tag, `Q-1234`). The
  vendor's model is the one tag of that family its page prints in text (every print the same, the
  item's own among them); the reference's is the one tag of that family its sheet prints beside a
  sink word, in text or in a pasted view's agreed words. Both read by code: the same tag is
  **Right** ("same sink model"); another tag is a **Likely problem** ("please check and confirm"),
  never red (the tag kind has no clean track record); two models on either side, or one not read,
  changes nothing. An open copy of the same difference on the same vendor page folds into the item
  that already says it (the whole-drawing look's, when there is one).
- **"The architect draws no splash"**: the whole-drawing look's claim that the vendor adds a splash
  (back, side or both) the reference does not draw (its Likely problem, or the splash step's
  "not confirmed by code"), and a reading whose reference side says no splash is drawn. When a
  pasted section or elevation of the sheet draws the splash standing on the top at the wall
  (`pasted_views.drawn_splashes`) or names it in agreed words (`SOLID SURFACE SPLASH`), the
  reference does show a splash: the look's claim is **withdrawn** (its stated reason, that the
  sections show none, is not so; like the look's other claims code finds false, it is dropped
  with that reason); a reading's item keeps its place, its reason saying what the section draws.
  The look's claim about the splash's height that rests on the reference showing none, or a lower
  one with no printed height (read by eye, or off the snapshot's printed scale), goes the same way
  as a reading's (Anant, 2026-10-27): Not compared, its Likely-problem mark taken off. A claim that
  the reference's splash is taller, and any item a view prints a splash size for, stay as they are.
  Sizes are never compared here: a section prints no splash size, and a back splash is never
  compared with a side splash (`splash_views` does sizes, like for like).
- **The countertop depth** (the item is the top's depth): a pasted section's agreed
  `2'-0" COUNTERTOP` is the top's depth. The same as the vendor's value read by code is **Right**,
  compared with that label instead of a plan size (a plan's depth may be the cabinet and its door,
  never the top); a different one is a **Likely problem**. Two sections printing two depths, or a
  depth the engines read differently (a stacked fraction), changes no outcome: the item says so.

Every change keeps what code read in the finding's `pasted_views` record. Pure: the views are
passed in. Verification: `tests/review/assistant/test_pasted_views.py`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from typing import Any, Final, Literal

from app.review.assistant import likely_problems
from app.review.assistant.pasted_views import (
    Line,
    View,
    countertop_depths,
    model_tags,
    splash_words,
)

__all__ = ["FIELD", "METHOD", "Change", "changes", "reference_sheet"]

#: The finding's field with what this step read and did.
FIELD: Final = "pasted_views"
#: How an item this step settled says so.
METHOD: Final = "pasted_views"

type Outcome = Literal["right", "likely", "withdrawn", "folded", "noted"]
type Views = Callable[[str, int], Sequence[View]]
type Phrases = Mapping[tuple[str, int], Sequence[Mapping[str, Any]]]

_SINK: Final = re.compile(r"\bsinks?\b", re.IGNORECASE)
_SINK_WORD: Final = re.compile(r"\bSINKS?\b")
#: A tag printed with these words is another product's (a faucet's, a drain's), never the sink's.
_NOT_SINK: Final = re.compile(r"\b(?:FAUCETS?|DRAINS?|TRAPS?|PULLS?|HANDLES?|STRAINERS?)\b")
_TOP_DEPTH: Final = re.compile(
    r"\b(?:counter\s*)?top\s+depth\b|\bdepth\s+of\s+(?:the\s+)?(?:counter\s*)?top\b", re.IGNORECASE
)
_NOT_TOP: Final = re.compile(r"\bcabinet\b|\bsplash\b|\bsink\b|\bcut-?out\b", re.IGNORECASE)
#: A reading's reference side that says no splash is drawn.
_NO_SPLASH: Final = re.compile(
    r"\bno\b[^.;]*\bsplash|\bsplash\w*\b[^.;]*\b(?:not\s+(?:shown|drawn)|none)\b", re.IGNORECASE
)
#: A look's reference side that says the reference's splash is lower than the vendor's...
_LOWER: Final = re.compile(
    r"\blow(?:er)?\b|\bshort(?:er)?\b|\bupstand\b|\bsmaller\b", re.IGNORECASE
)
#: ...and that its height is not printed (the look read it by eye or off the snapshot's scale).
_UNPRINTED: Final = re.compile(
    r"\bnot\s+(?:printed|dimensioned|sized|given)\b|\bno\s+(?:size|height|dimension)\b"
    r"|\bby\s+scale\b|\bscaled\b|\babout\b|\bapprox",
    re.IGNORECASE,
)
#: A look's reference side that says the reference's splash is taller: never this step's.
_TALLER: Final = re.compile(r"\bfull\b|\btall(?:er)?\b|\bhigher\b|\bbigger\b", re.IGNORECASE)
#: A size printed in a view's agreed words (`4" SOLID SURFACE SPLASH`).
_SIZED: Final = re.compile(r"\d\s*(?:\"|''|”|″)")
_OPEN: Final = frozenset({"same", "different", "check"})


@dataclass(slots=True)
class Change:
    """What this step does to one item."""

    outcome: Outcome
    why: str
    record: dict[str, Any] = field(default_factory=dict)
    updates: dict[str, Any] = field(default_factory=dict)
    """Fields set on the item (its reference side, its tag reading, its Likely-problem mark)."""
    drop: tuple[str, ...] = ()
    """Fields taken off the item."""
    into: str | None = None
    """The item a folded copy goes into."""


# ---- where to look ------------------------------------------------------------------------------


def _box(raw: object) -> tuple[float, float, float, float] | None:
    if isinstance(raw, Mapping):
        try:
            return tuple(float(raw[k]) for k in ("x0", "y0", "x1", "y1"))  # type: ignore[return-value]
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _roles(page_map: Mapping[str, Any] | None) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for entry in (page_map or {}).get("pages") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            out[(str(entry.get("file")), int(entry["page"]))] = str(entry.get("role") or "")
    return out


def reference_sheet(
    finding: Mapping[str, Any], page_map: Mapping[str, Any] | None, main_file: str | None
) -> tuple[str, int] | None:
    """The reference sheet the item was compared with: its architect spot's page, when that page
    is the architect's in the page map (never the vendor's own file, a brand prototype or a page
    of no known role)."""
    spot = finding.get("arch_box")
    if not isinstance(spot, Mapping) or not isinstance(spot.get("page"), int):
        return None
    file = str(spot.get("file") or "")
    if not file or file == main_file:
        return None
    key = (file, int(spot["page"]))
    return key if _roles(page_map).get(key) == "architect" else None


def _vendor_page(finding: Mapping[str, Any], main_file: str | None) -> tuple[str, int] | None:
    spot = finding.get("vendor_box")
    page = finding.get("page")
    file = str(spot.get("file") or "") if isinstance(spot, Mapping) else ""
    file = file or str(main_file or "")
    return (file, int(page)) if file and isinstance(page, int) else None


def _flat(text: object) -> str:
    return " ".join(str(text or "").split())


# ---- the sink model -----------------------------------------------------------------------------


def _tags(text: object) -> list[str]:
    return list(import_module("app.review.assistant.tag_reader").tag_keys(text))


def _family(key: str) -> str:
    return str(import_module("app.review.assistant.tag_reader").family(key))


def sink_item(finding: Mapping[str, Any]) -> str | None:
    """The vendor's claimed sink model (`Q-1234`) when the item names a sink and the vendor's side
    one product tag, else `None`."""
    if not _SINK.search(_flat(finding.get("item"))):
        return None
    claimed = _tags(finding.get("vendor"))
    return claimed[0] if len(claimed) == 1 else None


def _vendor_model(
    finding: Mapping[str, Any], claimed: str, phrases: Phrases, main_file: str | None
) -> tuple[str | None, str]:
    """The vendor's model read by code: the one tag of the claim's family its page prints in
    text; (None, why) otherwise."""
    where = _vendor_page(finding, main_file)
    if where is None:
        return None, "the vendor's page is not known"
    family = _family(claimed)
    found: list[str] = []
    for phrase in phrases.get(where, ()):
        if phrase.get("side") not in (None, "vendor"):
            continue
        found += [k for k in _tags(phrase.get("text")) if _family(k) == family]
    models = sorted(set(found))
    if models == [claimed]:
        prints = len(found)
        return claimed, f"the vendor's page {where[1]} prints {claimed} ({prints}x, its text)"
    if not models:
        return None, f"the vendor's page {where[1]} prints no {family}- tag in its text"
    return None, f"the vendor's page {where[1]} prints {', '.join(models)}"


def _near_sink(phrase: Mapping[str, Any], others: Sequence[Mapping[str, Any]]) -> bool:
    if _SINK_WORD.search(_flat(phrase.get("text")).upper()):
        return True
    box = phrase.get("box")
    if not isinstance(box, Sequence) or len(box) != 4:
        return False
    for other in others:
        at = other.get("box")
        if (
            other is not phrase
            and isinstance(at, Sequence)
            and len(at) == 4
            and _SINK_WORD.search(_flat(other.get("text")).upper())
            and abs(float(at[1]) - float(box[1])) <= 20
            and float(at[0]) < float(box[2]) + 20
            and float(box[0]) < float(at[2]) + 20
        ):
            return True
    return False


def _reference_models(
    sheet: tuple[str, int], family: str, phrases: Phrases, views: Sequence[View]
) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Each model tag of `family` the reference sheet prints beside a sink word: (tag, where in
    words, its spot)."""
    out: list[tuple[str, str, Mapping[str, Any]]] = []
    own = [p for p in phrases.get(sheet, ()) if p.get("side") in (None, "architect")]
    for phrase in own:
        if not _near_sink(phrase, own) or _NOT_SINK.search(_flat(phrase.get("text")).upper()):
            continue
        box = phrase.get("box")
        for key in _tags(phrase.get("text")):
            if _family(key) == family and isinstance(box, Sequence) and len(box) == 4:
                spot = _spot(sheet, tuple(float(v) for v in box))
                out.append((key, f"the reference's page {sheet[1]} prints {key} (its text)", spot))
    for view in views:
        for key, line in model_tags(view, _SINK_WORD):
            if _family(key) == family and not _NOT_SINK.search(line.text):
                how = (
                    f"the reference's {view.name} (page {view.page}) prints {line.text} "
                    "(pen strokes, read the same by two OCR engines)"
                )
                out.append((key, how, _spot(sheet, line.box)))
    return out


def _spot(sheet: tuple[str, int], box: Sequence[float]) -> dict[str, Any]:
    return {
        "x0": float(box[0]),
        "y0": float(box[1]),
        "x1": float(box[2]),
        "y1": float(box[3]),
        "file": sheet[0],
        "page": sheet[1],
    }


def _sink_change(
    finding: Mapping[str, Any],
    sheet: tuple[str, int],
    views: Sequence[View],
    phrases: Phrases,
    main_file: str | None,
) -> Change | None:
    claimed = sink_item(finding)
    if claimed is None:
        return None
    vendor, vendor_how = _vendor_model(finding, claimed, phrases, main_file)
    if vendor is None:
        return None
    found = _reference_models(sheet, _family(claimed), phrases, views)
    models = sorted({key for key, _how, _spot in found})
    if len(models) != 1:
        return None  # none read, or two models on the sheet: which is this sink's is not known
    reference, how, spot = found[0]
    item = _flat(finding.get("item")) or "Sink model"
    record: dict[str, Any] = {
        "kind": "sink_model",
        "vendor": vendor,
        "vendor_how": vendor_how,
        "reference": reference,
        "reference_how": how,
        "sheet": {"file": sheet[0], "page": sheet[1]},
    }
    said = f"{vendor_how}; {how}"
    if reference == vendor:
        why = f"same sink model: {said}"
        record["outcome"] = "right"
        return Change(
            "right",
            why,
            record,
            updates={
                "tag_read": {"outcome": "right", "why": why, "by": METHOD},
                "architect": f"{reference} ({how.split(' prints ')[0].removeprefix('the ')})",
                "arch_box": spot,
                "vs_page": sheet[1],
            },
        )
    why = f"another sink model: {said}"
    record["outcome"] = "likely_problem"
    mark = finding.get(likely_problems.FIELD)
    keep_mark = isinstance(mark, Mapping) and mark.get("by") not in (None, METHOD)
    updates: dict[str, Any] = {
        "status": "check",
        "arch_box": spot,
        "vs_page": sheet[1],
        "tag_read": {
            "outcome": "not_compared",
            "reason": "tags_differ",
            "why": f"tags differ: the reference's {reference}, the vendor's {vendor} (both read "
            "by code): please check and confirm which sink is meant",
            "settle": f"the vendor's confirmation that {vendor} is the sink the reference names "
            f"({reference})",
            "by": METHOD,
        },
    }
    if not keep_mark:
        updates[likely_problems.FIELD] = {
            "by": METHOD,
            "kind": "size",
            "proof": "likely_only",
            "sure": "likely",
            "why": why,
            "words": (
                f"{likely_problems.LETTER_LEAD}: {item}: the reference names the sink "
                f"{reference}; the vendor's drawing names {vendor} (its cut-out may not fit "
                "the sink the reference names)."
            ),
        }
    record["keeps_look"] = keep_mark
    return Change("likely", why, record, updates=updates)


# ---- the splash ---------------------------------------------------------------------------------


def _splash_part(finding: Mapping[str, Any]) -> Any:
    splash = import_module("app.review.assistant.splash_views")
    return splash.subject_of(finding.get("item")) or splash.subject_of(finding.get("vendor"))


def _look_reads_none(architect: str) -> bool:
    """A look's reference side that rests on the reference not showing the splash, or showing it
    lower with no printed height (read by eye, or off a pasted snapshot's printed scale, which is
    wrong wherever the snapshot was resized): "no 6" splash on the elevation; the section shows
    a short upstand, about 2" by scale". Never one that says the reference's splash is
    taller or full height (the vendor's may be too low: that claim stands)."""
    if _TALLER.search(architect):
        return False
    return bool(_NO_SPLASH.search(architect)) or bool(
        _LOWER.search(architect) and _UNPRINTED.search(architect)
    )


def _absence(finding: Mapping[str, Any]) -> str | None:
    """`look` (the whole-drawing look's claim that the vendor adds a splash the reference does
    not draw), `reading` (a reading's reference side says no splash is drawn; or a look's claim
    about the splash's height that rests on the reference showing none, or a lower one with no
    printed height: Anant 2026-10-27, the same section rule as a reading's), or `None`. A look's
    claim that the vendor leaves a splash out, or that the reference's splash is taller, is never
    one: a section drawing the splash bears it out. The vendor's side must draw the splash (a
    size, no words of absence)."""
    classification = import_module("app.review.assistant.classification")
    record = finding.get("splash_views")
    mark = finding.get(likely_problems.FIELD)
    vendor = _flat(finding.get("vendor"))
    vendor_draws = bool(re.search(r"\d", vendor)) and not classification.vendor_absent(vendor)
    by_look = finding.get("created_by") == "whole_look" or (
        isinstance(mark, Mapping) and mark.get("by") in ("whole_drawing_look", "whole_look")
    )
    if by_look:
        if isinstance(mark, Mapping):
            added = mark.get("kind") == "extra_piece"
        else:
            added = (
                isinstance(record, Mapping)
                and bool(record.get("absence_not_confirmed"))
                and record.get("look_kind") in (None, "extra_piece")
            )
        if added:
            return "look" if vendor_draws else None
        if (
            isinstance(mark, Mapping)
            and mark.get("kind") != "missing_piece"
            and vendor_draws
            and _look_reads_none(_flat(finding.get("architect")))
        ):
            return "reading"
        return None
    if vendor_draws and _NO_SPLASH.search(_flat(finding.get("architect"))):
        return "reading"
    return None


#: An item that names a side splash (a section never shows one).
_SIDE_WORDS: Final = re.compile(r"\bside\s*splash|\bend\s*splash", re.IGNORECASE)


def _splash_seen(views: Sequence[View]) -> list[tuple[View, str]]:
    """Each pasted section or elevation that draws or names a splash, and how."""
    out: list[tuple[View, str]] = []
    for view in views:
        if view.kind not in ("section", "elevation"):
            continue
        named = splash_words(view)
        if view.splashes:
            out.append((view, "draws a splash on the top, against the wall"))
        elif named:
            out.append((view, f"names a splash ({named[0].text})"))
    return out


def _splash_change(finding: Mapping[str, Any], views: Sequence[View]) -> Change | None:
    absence = _absence(finding)
    if absence is None or _splash_part(finding) is None:
        return None
    seen = _splash_seen(views)
    if not seen:
        return None
    view, how = seen[0]
    shown = f"the reference's {view.name} (page {view.page}) {how}"
    record = {
        "kind": "splash",
        "seen": [{"view": v.words, "how": h, "box": list(v.box)} for v, h in seen],
    }
    if absence == "look":
        why = (
            "withdrawn: the whole-drawing look said the reference draws no splash, but "
            f"{shown}: the reference does show the splash, so the claim is not so (no splash "
            "size is printed there, so none is compared)"
        )
        record["outcome"] = "withdrawn"
        return Change("withdrawn", why, record)
    if any(_SIZED.search(line.text) for v, _ in seen for line in splash_words(v)):
        # A view prints a size beside the splash's name: "it prints no size" would not be so,
        # and the item may be a real difference the section proves. It stays as it is.
        return None
    why = f"{shown}, so the reference does show a splash. It prints no size for it, so no size is compared."
    if _SIDE_WORDS.search(" ".join(_flat(finding.get(key)) for key in ("item", "vendor"))):
        why += " A section shows only the back splash, so the vendor's side splashes are not checked there."
    record["outcome"] = "not_compared"
    # A look's claim about the splash's height (it read the reference's splash as missing or lower,
    # by eye) is held to the same rule as a reading's: Not compared, never a Likely problem.
    mark = finding.get(likely_problems.FIELD)
    by_look = isinstance(mark, Mapping) and mark.get("by") in ("whole_drawing_look", "whole_look")
    drop = (likely_problems.FIELD,) if by_look else ()
    updates: dict[str, Any] = {
        "architect": f"splash drawn in the reference's {view.name}, page {view.page} (no size)",
        "splash_views": {
            **(
                dict(finding["splash_views"])
                if isinstance(finding.get("splash_views"), Mapping)
                else {}
            ),
            "why": why,
            "outcome": "not_compared",
            "by": METHOD,
        },
    }
    return Change("noted", why, record, updates=updates, drop=drop)


# ---- the countertop depth -----------------------------------------------------------------------


def _vendor_read(finding: Mapping[str, Any]) -> Decimal | None:
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    label = (dims or {}).get("vendor")
    checks = finding.get("checks") if isinstance(finding.get("checks"), Mapping) else {}
    read = isinstance(label, Mapping) and str(label.get("route") or "text") == "text"
    if not (read or (checks or {}).get("printed_vendor") == "match"):
        return None
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
    try:
        return Decimal(str((inches or {}).get("vendor")))
    except (ArithmeticError, ValueError, TypeError):
        return None


def _inch_words(value: Decimal) -> str:
    return str(import_module("app.review.assistant.chat_trust").inch_words(value))


def _depth_item(finding: Mapping[str, Any]) -> bool:
    item = _flat(finding.get("item"))
    return bool(_TOP_DEPTH.search(item)) and not _NOT_TOP.search(item)


def _depth_change(
    finding: Mapping[str, Any], sheet: tuple[str, int], views: Sequence[View]
) -> Change | None:
    if not _depth_item(finding):
        return None
    item = _flat(finding.get("item"))
    sections = [v for v in views if v.kind == "section"]
    read: list[tuple[Decimal, Line, View]] = []
    unread: list[tuple[str, str, View]] = []
    for view in sections:
        printed, garbled = countertop_depths(view)
        read += [(value, line, view) for value, line in printed]
        unread += [(a, b, view) for a, b, _box in garbled]
    if not read and not unread:
        return None
    vendor = _vendor_read(finding)
    record: dict[str, Any] = {
        "kind": "countertop_depth",
        "read": [{"text": line.text, "view": view.words} for _v, line, view in read],
        "unread": [{"readings": [a, b], "view": view.words} for a, b, view in unread],
    }
    values = {value for value, _line, _view in read}
    if not read or len(values) != 1 or vendor is None:
        if len(values) > 1:
            why = (
                "the reference's sections print the countertop depth two ways ("
                + ", ".join(sorted(_inch_words(v) for v in values))
                + "): not compared"
            )
        elif not read:
            a, b, view = unread[0]
            why = (
                f"the reference's {view.name} (page {view.page}) prints the countertop depth, but "
                "code's two OCR engines read it "
                f"differently ({a} / {b}): not read yet"
            )
        else:
            why = "the vendor's value was not read by code at its spot"
        record["outcome"] = "noted"
        return Change("noted", why, record)
    value, line, view = read[0]
    label = {
        "id": "pasted",
        "origin": "pasted_view",
        "text": line.text,
        "inches": str(value),
        "box": list(line.box),
        "file": sheet[0],
        "page": sheet[1],
        "side": "architect",
        # The picture route's trust class (dim_labels): route "drawn", origin says OCR.
        "route": "drawn",
    }
    read_how = (
        f"code read {_inch_words(vendor)} at the vendor's spot and {line.text} in the "
        f"reference's {view.name} (page {view.page}) "
        "(pen strokes, read the same by two OCR engines)"
    )
    dims = dict(finding.get("dims") or {}) if isinstance(finding.get("dims"), Mapping) else {}
    dims["architect"] = label
    inches = dict(finding.get("inches") or {}) if isinstance(finding.get("inches"), Mapping) else {}
    inches.update(architect=str(value), vendor=str(vendor), difference=str(vendor - value))
    checks = dict(finding.get("checks") or {}) if isinstance(finding.get("checks"), Mapping) else {}
    checks["printed_architect"] = "match"
    checks.pop("verify_architect", None)
    updates: dict[str, Any] = {
        "architect": line.text,
        "arch_box": _spot(sheet, line.box),
        "vs_page": sheet[1],
        "dims": dims,
        "inches": inches,
        "checks": checks,
    }
    record["compared"] = {"text": line.text, "view": view.words}
    if value == vendor:
        record["outcome"] = "right"
        why = f"{read_how}: the countertop's depth on both, the same"
        return Change("right", why, record, updates=updates, drop=("paired_by",))
    why = f"{read_how}: they differ"
    updates["status"] = "check"
    updates[likely_problems.FIELD] = {
        "by": METHOD,
        "kind": "size",
        "proof": "likely_only",
        "sure": "likely",
        "why": why,
        "words": (
            f"{likely_problems.LETTER_LEAD}: {item or 'Countertop depth'}: the reference's "
            f"{view.name} (page {view.page}) shows {line.text}; the vendor's drawing shows "
            f"{_inch_words(vendor)}."
        ),
    }
    record["outcome"] = "likely_problem"
    return Change("likely", why, record, updates=updates, drop=("paired_by",))


# ---- every item ---------------------------------------------------------------------------------


def _open(finding: Mapping[str, Any]) -> bool:
    proof = import_module("app.review.assistant.proof")
    classification = import_module("app.review.assistant.classification")
    proved = finding.get("status") == "different" and proof.code_proved(finding)
    return (
        finding.get("status") in _OPEN
        and not proved
        and not finding.get(FIELD)
        and not proof.reviewer_decided(finding)
        and not classification.internal(finding)
        and finding.get("created_by") not in ("architect_items", "consistency", "ledger")
        and proof.proof_group(finding) in proof.UNDECIDED
    )


def changes(
    live: Iterable[Mapping[str, Any]],
    views_of: Views,
    phrases: Phrases,
    page_map: Mapping[str, Any] | None,
    main_file: str | None,
) -> list[tuple[str, Change]]:
    """What the reference's pasted views settle, item by item (see the module), with each open
    copy of a sink-model difference folded into the item that already says it."""
    out: list[tuple[str, Change]] = []
    for finding in live:
        if not _open(finding):
            continue
        sink = sink_item(finding) is not None
        splash = not sink and _absence(finding) is not None and _splash_part(finding) is not None
        depth = not sink and not splash and _depth_item(finding)
        if not (sink or splash or depth):
            continue  # never reads a page for an item this step does not settle
        sheet = reference_sheet(finding, page_map, main_file)
        if sheet is None:
            continue
        views = views_of(*sheet)
        if sink:
            change = _sink_change(finding, sheet, views, phrases, main_file)
        elif splash:
            change = _splash_change(finding, views)
        else:
            change = _depth_change(finding, sheet, views) if views else None
        if change is not None:
            out.append((str(finding["id"]), change))
    return _fold_sink_copies(live, out)


def _fold_sink_copies(
    live: Iterable[Mapping[str, Any]], found: list[tuple[str, Change]]
) -> list[tuple[str, Change]]:
    """One card per sink-model difference on a vendor page: the whole-drawing look's item keeps
    it when there is one (else the first), and the other copies fold into it."""
    by_id = {str(f["id"]): f for f in live}
    groups: dict[tuple[Any, ...], list[str]] = {}
    for fid, change in found:
        if change.outcome != "likely" or change.record.get("kind") != "sink_model":
            continue
        finding = by_id[fid]
        key = (finding.get("page"), change.record["vendor"], change.record["reference"])
        groups.setdefault(key, []).append(fid)
    folded: dict[str, str] = {}
    for ids in groups.values():
        if len(ids) < 2:
            continue
        keeper = next(
            (i for i in ids if by_id[i].get("created_by") == "whole_look"),
            ids[0],
        )
        for fid in ids:
            if fid != keeper:
                folded[fid] = keeper
    result: list[tuple[str, Change]] = []
    for fid, change in found:
        if fid in folded:
            keeper = folded[fid]
            why = (
                f"the same sink model difference as {keeper} on page {by_id[fid].get('page')} "
                f"({change.record['vendor']} against the reference's {change.record['reference']})"
            )
            change = Change("folded", why, {**change.record, "outcome": "folded"}, into=keeper)
        result.append((fid, change))
    return result
