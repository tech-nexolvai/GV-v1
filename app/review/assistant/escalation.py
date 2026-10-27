"""Escalation (#1619): every item code could not compare at first is tried again, cheapest first.

The architect's drawings are the source of truth and nobody at GV is asked to decide anything
(Anant, 2026-10-18; the rules are `classification.py`). Before an item falls to "Not compared",
inside the full check and its fixed price, it climbs a ladder, the biggest items first
(`impact.by_impact`), and stops at the first step that settles it:

0. **The reference is the architect's** (Anant, 2026-10-22; `round_reference`, $0): an item
   compared only with the vendor's own drawing (its round 1) is never Right; code records the
   round match as information ("Unchanged since round 1") and compares the item with the architect
   instead where it links the vendor's label to an architect label (another item comparing the
   same label, code's pairs, the earlier round's own item); else it stays Not compared.
1. **Free code steps** (this module, $0):
   - `labels`: the drawn-label reader (`dims.json`, set pack v3): code's numbered label inside the
     item's box that prints the item's value is a code reading of it (`printed_<side>`), and a
     vendor value nobody could read becomes the label's value; a difference code reads at both
     labels that is the vendor's overall with GV's field cut in it (`field_cut.vet`, settled: 1"
     per wall end) is Right, as when the item is recorded;
   - `under_markup`: the drawing's own text under GV's markup (`under_markup.read_under_markup`),
     for a vendor value hidden by GV's boxes;
   - `same_item`: the same item on the vendor's other views or rounds: a copy that is already a
     proven Wrong carries it, and several open copies become one item;
   - `vendor_sums`: a string of the vendor's own that code already proved does not add up
     (`vendor_consistency`) carries the item;
   - `pairs`: code's own pairing of the two labels by drawn position (`pairs.json`): a proof that
     both numbers measure the same thing (`paired_by: code`);
   - `chain_sums` (fix 4, 2026-10-25): the architect's label is one part of a chain code proves
     (`dim_ends.chain_of`: the reference's `1"` beside its `35"`, end to end on one dimension
     line, no overall printed): its sum is compared like a printed overall, from the same kinds of
     ends only (`dim_ends.alike`, never unlike). The same as the vendor's is Right; a different
     sum changes nothing (code did not pair the vendor's dimension with the whole run). A
     splash's size is never settled against the top's run, except a back splash's length against
     a run code sees go from wall to wall (`_splash_wall_run`, Anant 2026-10-25; the printed
     compare applies it too);
   - `vendor_search` (accuracy step 1): an item the AI says the vendor's drawing does not show
     (`classification.missing_on_vendor`): code searches every vendor drawing of its area (the
     page map) for the architect's printed value (`spot_locate.search_area`) and records what it
     found (`vendor_search`), so the Not-compared reason says it. A dimension the vendor does
     not print does not by itself prove the item is left out (code does not recognise every
     symbol, #1648), and the same value printed elsewhere does not prove it is the same
     dimension. Only a **millwork item** code proves missing is red (Anant, 2026-10-22,
     `missing_items.judge`: the architect's code-read value, millwork by its words and not a room
     feature, every vendor drawing of the area read and none printing it or naming the item);
     every other item keeps its outcome, its reason saying why it is not red. Copies of the same
     architect label fold into the red.
   Missed, changed and added finishes (R4) are code's own finish check (`architect_items`, #1623),
   which runs at the finish of every full check; its findings are proved there, never here.
   Before the ladder, on every item whatever its outcome (#1859):
   - `ends` (like for like, fix 3, 2026-10-25; `dim_ends`): what each of the item's two code-read
     labels measures from and to; unlike ends are never compared: the item goes to the GV rule
     that relates them (the sink centre, `sink_centre`; the field cut) or is Not compared with the
     plain reason. A notch claim whose step code sees is dropped (GV: notches are cut on site), and
     an "L-shaped top" claim where the reference's top sizes stop at a wall drawn beyond them;
   - `gv_sticker`: a vendor value inside one of GV's stickers (`gv_stickers`: GV's coloured note
     baked into the pasted drawing) that prints the same value is GV's, never the vendor's: Not
     compared, with what code reads of the vendor's own ink under the sticker;
   - `tags`: an item whose two sides name finish, material or product tags is read and compared by
     code (`tag_reader`): exactly the same tags at both spots is Right, a change code's finish check
     proved is Wrong, anything else Not compared with its true reason; a note in words that would
     otherwise be "Illegible" gets its true reason (a note in words, or words drawn as strokes).
2. **One deeper AI look** per item that is left (the roster, `full_check_roster`): a sharper crop
   through the existing verifier and devil's advocate, under the pairing-proof setting, and only
   with the money left under the fixed price.
3. **Whatever is still open** is Not compared, with its reason and what would settle it
   (`classification`), listed in the separate Not-compared PDF, never on the cloud drawing. An
   open item GV's own rule sets (the countertop's depth, the sink cut-out's size) whose job values
   nobody entered is named with the rule and the values to enter (`job_value_rules`).

After the ladder, on every Right (`row_places`, 2026-10-27): a piece of a row of sizes is Right only
against the reference piece that starts at the same place along its row (the vendor's range gap is
not the reference's other piece of that size further up the counter): code points the item at that
piece, or, with none, the item is Not compared. Rows code cannot measure leave the item as it was.
Then each Right the AI paired needs code's proof that both dimensions measure the same thing
(`right_proof`, 2026-10-27): the same ends by code's geometry, or the only such size on both
drawings; without it the item is Not compared.

Code decides every outcome, with the same witness rules as the red clouds: a step only adds code's
readings (a label, the text under the markup, code's pairing) or folds a copy into
the item that already carries it; a Right needs both values read by code and equal; a red cloud
still needs `proof.code_proved`. Every step tried is kept on the finding (`escalation`), so the
reviewer and the replay can see what each item went through.

`escalate` is pure (its readers are passed in); `escalate_answer(engine)` is the engine's hook.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant import (
    classification,
    dim_ends,
    drafting_notes,
    ends_proof,
    formulas,
    job_value_rules,
    likely_problems,
    missing_items,
    proof,
    reading_routes,
    right_proof,
    row_places,
    sink_centre,
)
from app.review.assistant.chat_trust import inch_words, judged, round_item_key
from app.review.assistant.full_check_advocate import code_pairs
from app.review.assistant.impact import by_impact
from app.review.assistant.ledger import format_inches
from app.review.assistant.round_reference import compare_with_architect
from app.review.assistant.spot_locate import VENDOR_SEARCH, search_area, search_words

__all__ = [
    "FREE_STEPS",
    "PRECISION",
    "Escalation",
    "EscalationContext",
    "context_from_pack",
    "escalate",
    "escalate_answer",
    "label_provenance",
    "undecided",
]

_log = logging.getLogger(__name__)

type StepName = Literal[
    "labels",
    "under_markup",
    "same_item",
    "vendor_sums",
    "pairs",
    "chain_sums",
    "equal_parts",
    "read_number",
    "vendor_search",
    "gv_sticker",
    "tags",
    "ends",
    "pasted_views",
    "claim_rules",
]
#: The free code steps, cheapest first.
FREE_STEPS: Final[tuple[StepName, ...]] = (
    "labels",
    "under_markup",
    "same_item",
    "vendor_sums",
    "pairs",
    "chain_sums",
    "vendor_search",
)
#: Two printed values this close are the same (the drawings' smallest step, as the resolver).
PRECISION: Final = Decimal("0.0625")
#: How far round a finding's box a label may sit and still be read as the box's value (points).
_SLACK_PT: Final = 8.0
#: A tag code as drawings print them (CT-001, ST-002-CUST, WD-01).

#: (file, page, box) -> the drawing's own words under GV's markup in that box (text, colour).
type UnderReader = Callable[[str, int, tuple[float, float, float, float]], list[dict[str, Any]]]
#: The read-number tool bound to the set pack (`read_number.NumberReader`): a `Request` in, a
#: `Reading` out.
type NumberReader = Callable[[Any], Any]
#: A side that prints only equal-parts marks (EQ / EQ), no number.
_EQ: Final = re.compile(r"\b(?:EQ|EQUAL)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EscalationContext:
    """What the free steps read: the set pack's facts and the drawing's text under the markup."""

    main_file: str | None
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    page_map: Mapping[str, Any] | None = None
    pairs: Mapping[str, str] = field(default_factory=dict)
    under: UnderReader | None = None
    reread: NumberReader | None = None
    """The read-number tool, for a vendor value that has no spot (#1746)."""
    phrases: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    """Each page's text-layer phrases (`dims.json`), for a missing item's words and search."""
    stickers: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    """GV's stickers (`gv_stickers`), by (file, page)."""
    strokes: Callable[[str, int, tuple[float, float, float, float]], Any] | None = None
    """The tag reader's stroke route (`tag_reader.StrokeReader`)."""
    changes: Sequence[Mapping[str, Any]] = ()
    """Code's finish check's proved "changed" findings (`architect_items`), for the tag reader."""
    page_texts: Mapping[tuple[str, int], str] = field(default_factory=dict)
    """Each page's whole text (`page_texts.json`)."""
    pictures: Mapping[tuple[str, int], float] = field(default_factory=dict)
    """Each page's share covered by pictures (`health.json`)."""
    annotations: Sequence[Mapping[str, Any]] = ()
    """GV's markup (`annotations.json`): its typed notes are never the architect's value."""
    unopened: frozenset[int] | None = None
    """The main file's pages the check never opened (its coverage); None: not known (none)."""
    stop: Callable[[], bool] | None = None
    """Stop was pressed (2026-10-23): the tag reader and the ladder try no further item (each one
    tried is kept); GV's stickers, which only ever take a red away, are still told apart."""
    count: Callable[[int], Callable[[], None]] | None = None
    """The live line's item count (`ChatEngine.count_items`): given the items to try, it returns
    the function that counts one tried."""
    field_cuts: Callable[[str, int], Any] | None = None
    """A vendor page's field-cut labels (`field_cut.page_field_cuts`, the book's `field_cuts`):
    a difference code read that is the vendor's overall with GV's field cut in it is Right."""
    rules_md: str | None = None
    """The chat's `rules.md` (the job values in force), for the GV rules whose job values are
    missing (`job_value_rules`)."""
    pages: Callable[[str, int], dim_ends.Page | None] | None = None
    """Each page's dimensions and what their ends land on (`dim_ends.pages_reader`), for the
    like-for-like step (`_ends_step`)."""
    pasted: Callable[[str, int], Sequence[Any]] | None = None
    """The reference's pasted views by (file, page) (`pasted_views.PastedReader`): the sink
    model, the splash and the countertop depth its sections show (`pasted_compare`)."""
    outlines: Callable[[str, int, tuple[float, float, float, float]], str | None] | None = None
    """The reference's line work at a spot (`claim_rules.OutlineReader`): a wall outline is
    never the countertop."""
    typed: reading_routes.TypedOf | None = None
    """Both OCR engines on a typed number's own spot (`typed_ocr`): a typed number's second,
    independent reading before a red (final plan #2)."""


@dataclass(slots=True)
class Escalation:
    """What the free steps did to one answer's findings."""

    changed: list[dict[str, Any]] = field(default_factory=list)
    absorbed: dict[str, str] = field(default_factory=dict)
    """Folded copies: {finding id: the reason, naming the item that carries it}."""
    tried: int = 0
    settled: dict[str, int] = field(default_factory=dict)
    """How many items each step settled (an outcome: wrong, right or folded)."""
    log: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    """Per finding id: each step tried, its outcome and what code found."""
    round_matches: int = 0
    """Items compared only with the vendor's own drawing (`round_reference`)."""
    linked: dict[str, str] = field(default_factory=dict)
    """Per finding id: how code compared such an item with the architect instead."""

    def block(self) -> dict[str, Any]:
        return {
            "tried": self.tried,
            "settled": dict(self.settled),
            "absorbed": len(self.absorbed),
            "round_matches": self.round_matches,
            "linked": len(self.linked),
        }


# ---- small readers ------------------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _box(raw: object) -> tuple[str | None, int, tuple[float, float, float, float]] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        page = int(raw["page"])
        x0, y0, x1, y1 = (float(raw[key]) for key in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    name = raw.get("file")
    return (
        (None if name is None else str(name)),
        page,
        (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
    )


def _centre(box: Sequence[float]) -> tuple[float, float]:
    return (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2


def _inside(point: tuple[float, float], box: Sequence[float], slack: float = _SLACK_PT) -> bool:
    return (
        box[0] - slack <= point[0] <= box[2] + slack
        and box[1] - slack <= point[1] <= box[3] + slack
    )


def undecided(finding: Mapping[str, Any]) -> bool:
    """An item the escalation works on: Not compared today (a Right item not looked at on both
    sides aside), never one the reviewer decided, nor GV's filler-range note (`filler_range`), site-fit item
    (`run_fit`) or sink front offset item (`sink_offset`)."""
    if finding.get("created_by") == "filler_range":
        return False  # GV's filler range decided it, the architect first: nothing to climb
    if finding.get("created_by") == "run_fit":
        return False  # GV's site-fit rule decided it by its own arithmetic: nothing to climb
    if finding.get("created_by") == "sink_offset":
        return False  # GV's sink front offset: code proved it or said why not (#1807)
    if finding.get("created_by") == sink_centre.CREATED_BY:
        return False  # the sink centre against the sink base: code compared it or said why not
    return proof.proof_group(finding) in proof.UNDECIDED and finding.get("status") != "same"


def _labels_in(
    ctx: EscalationContext, finding: Mapping[str, Any], key: str, side: str
) -> list[Mapping[str, Any]]:
    where = _box(finding.get(key))
    if where is None:
        return []
    name, page, box = where
    file = name or ctx.main_file or ""
    return [
        dict(label, page=page, file=file)
        for label in ctx.labels.get((file, page), [])
        if label.get("side") == side and _inside(_centre(label.get("box") or (0, 0, 0, 0)), box)
    ]


def _same(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= PRECISION / 2


def _provenance(label: Mapping[str, Any]) -> dict[str, Any]:
    """A label as a finding's `dims` side keeps it (where its value came from)."""
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
        # An OCR reading (a picture's or a spot's, `label_origin`) stays one on the finding.
        **({"origin": label["origin"]} if label.get("origin") else {}),
    }


label_provenance = _provenance


def _ref(label: Mapping[str, Any]) -> str:
    return f"p{label.get('page')}.{label.get('id')}"


# ---- the steps ----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Item:
    finding: dict[str, Any]
    steps: list[dict[str, str]] = field(default_factory=list)
    changed: bool = False
    outcome: str | None = None
    """wrong / right / folded: settled by a step (None: still undecided)."""

    def note(self, step: StepName, outcome: str, how: str) -> None:
        self.steps.append({"step": step, "outcome": outcome, "how": how})


def _check(item: _Item, key: str, value: str) -> None:
    checks = dict(item.finding.get("checks") or {})
    if checks.get(key) != value:
        checks[key] = value
        item.finding["checks"] = checks
        item.changed = True


def _settle_right(item: _Item, step: StepName, how: str) -> None:
    was = str(item.finding.get("status"))
    item.finding.update(
        status="same",
        settled={"outcome": "right", "method": f"escalation_{step}", "how": how, "was": was},
        compared=True,
    )
    item.finding.pop("keep_reason", None)
    item.finding.pop("question", None)
    item.changed = True
    item.outcome = "right"


def _equal_parts(item: _Item, ctx: EscalationContext) -> None:
    """A vendor side that prints only EQ marks: its code label is an equal part code worked out
    (`dims.json`'s derived label: the printed overall, less the chain's printed parts, shared
    equally), so its value is that part's, said as code's ("EQ (20")"), and compared like any
    value code read. Any other label (or none) leaves the item as it is."""
    finding = item.finding
    text = " ".join(str(finding.get("vendor") or "").split())
    if not _EQ.search(text) or _claim(text) is not None:
        return
    dims = dict(finding.get("dims") or {}) if isinstance(finding.get("dims"), Mapping) else {}
    label: Mapping[str, Any] | None = _label_of(ctx, dims.get("vendor"))
    if label is None:
        boxed = [
            lab for lab in _labels_in(ctx, finding, "vendor_box", "vendor") if lab.get("derived")
        ]
        label = boxed[0] if len(boxed) == 1 else None
    derived = label.get("derived") if label is not None else None
    value = _decimal(label.get("inches")) if label is not None else None
    if not isinstance(derived, Mapping) or derived.get("rule") != "equal_parts" or not value:
        return
    assert label is not None
    inches = dict(finding.get("inches") or {})
    inches["vendor"] = str(value)
    words = inch_words(value)
    how = (
        f"the EQ marks share {derived.get('from')}'s printed overall equally in "
        f"{derived.get('parts')} parts: {words} each (code)"
    )
    finding.update(inches=inches, vendor=f"EQ ({words})")
    dims["vendor"] = {**_provenance(label), "derived": dict(derived)}
    finding["dims"] = dims
    checks = dict(finding.get("checks") or {})
    checks["verify_vendor"] = {
        "result": "match",
        "route": "drawn" if label.get("route") == "drawn" else "text",
        "read": f"EQ ({words})",
        "reads": [f"{_ref(label)}: {how}"],
        "reason": how,
        "value": words,
        "pending": False,
        "words": f"Code worked out the equal part: {how}",
    }
    finding["checks"] = checks
    item.changed = True
    _check(item, "printed_vendor", "match")
    item.note("equal_parts", "read", f"{_ref(label)}: {how}")
    _printed_compare(item, "equal_parts", ctx)


def _label_of(ctx: EscalationContext, side: object) -> dict[str, Any] | None:
    """The set pack's label a finding's `dims` side names (its file, page and id)."""
    if not isinstance(side, Mapping) or not side.get("id"):
        return None
    file = str(side.get("file") or ctx.main_file or "")
    page = int(side.get("page") or 0)
    for label in ctx.labels.get((file, page), []):
        if label.get("id") == side.get("id"):
            return dict(label, page=page, file=file)
    return None


def _claim(text: str) -> str | None:
    checks = import_module("app.review.assistant.finding_checks")
    claim = checks.claim_value(text)
    return claim if claim is not None and checks.to_inches(claim) is not None else None


def _reread(item: _Item, ctx: EscalationContext) -> None:
    """A vendor value of a difference between two drawings that has no spot (no box, no label):
    the read-number tool looks for it on the page (#1746). Read with the same value: its spot and
    code's reading go on the item (the code checks and the pairing proof still decide any red).
    Unread: its reason goes on the item, which stays listed as Not compared, never red and never
    dropped for having no spot (`spot_guard`)."""
    finding = item.finding
    if ctx.reread is None or finding.get("status") not in ("different", "check"):
        return
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    if finding.get("vendor_box") or (dims or {}).get("vendor"):
        return
    claim = _claim(" ".join(str(finding.get("vendor") or "").split()))
    page = finding.get("page")
    if claim is None or not isinstance(page, int) or isinstance(page, bool) or page >= 1000:
        return  # a set's page of a separate file (1001...) has no file of its own here
    file = str(ctx.main_file or "")
    numbers = import_module("app.review.assistant.read_number")
    try:
        reading = ctx.reread(numbers.Request(file, page, "vendor", claim=claim))
    except Exception as failed:  # noqa: BLE001 - a tool that cannot run settles nothing
        item.note("read_number", "no_change", f"the read-number tool could not run ({failed})")
        return
    checks = dict(finding.get("checks") or {})
    checks["verify_vendor"] = reading.as_verify(claim)
    finding["checks"] = checks
    item.changed = True
    box = reading.box
    if not reading.matches(claim) or not box or len(box) != 4:
        item.note("read_number", "no_change", f"not read on page {page}: {reading.reason}")
        return
    finding["vendor_box"] = {
        "page": page,
        "x0": float(box[0]),
        "y0": float(box[1]),
        "x1": float(box[2]),
        "y1": float(box[3]),
        "file": file or None,
    }
    if reading.label:
        label = _label_of(ctx, {"id": reading.label, "page": page, "file": file})
        if label is not None:
            finding["dims"] = {**(dims or {}), "vendor": _provenance(label)}
    item.note("read_number", "read", f"read on page {page}: {reading.reason}")


def _labels_step(item: _Item, ctx: EscalationContext) -> None:
    """Code's label in each box that prints the item's value is a code reading of it; a vendor
    value nobody could read becomes the only vendor label in its box. Before that, a vendor side
    of EQ marks takes its equal part's value (`_equal_parts`) and a vendor value with no spot is
    looked for by the read-number tool (`_reread`)."""
    _equal_parts(item, ctx)
    _reread(item, ctx)
    finding = item.finding
    inches = dict(finding.get("inches") or {})
    dims = dict(finding.get("dims") or {}) if isinstance(finding.get("dims"), Mapping) else {}
    found: list[str] = []
    for side, key in (("vendor", "vendor_box"), ("architect", "arch_box")):
        labels = _labels_in(ctx, finding, key, side)
        wanted = _decimal(inches.get(side))
        hits = [lab for lab in labels if _same(_decimal(lab.get("inches")), wanted)]
        if wanted is None and side == "vendor" and finding.get("keep_reason") == "unreadable":
            # Nobody could read the vendor's value: the one vendor label in its box is it.
            readable = [lab for lab in labels if _decimal(lab.get("inches")) is not None]
            if len(readable) == 1:
                hits = readable
                value = _decimal(readable[0].get("inches"))
                inches["vendor"] = str(value)
                finding["vendor"] = str(readable[0].get("text") or "") or (
                    inch_words(value) if value is not None else ""
                )
                finding["inches"] = inches
                item.changed = True
        if len(hits) != 1:
            continue
        label = hits[0]
        if side not in dims:
            dims[side] = _provenance(label)
            finding["dims"] = dims
            item.changed = True
        _check(item, f"printed_{side}", "match")
        found.append(f"{side} {label.get('text')} ({_ref(label)})")
    if not found:
        item.note("labels", "no_change", "no code label prints the item's value in its box")
        return
    item.note("labels", "read", "code read " + "; ".join(found))
    _printed_compare(item, "labels", ctx)


def _printed_compare(item: _Item, step: StepName, ctx: EscalationContext | None = None) -> None:
    """A "Your decision" item whose two values code now read at their spots: equal is Right
    (the resolver's printed-against-printed rule); different is a difference code read, which
    still needs a pairing proof to be clouded. Like for like only (2026-10-23): a splash's size is
    never settled against an architect size code does not see named as the same splash's
    (`splash_views.unlike`: the meeting set's splash length against the plan's top width)."""
    finding = item.finding
    if finding.get("status") == "different":
        # A difference code read (recorded so, or by the resolver) may be GV's field cut; never
        # an item the splash step looked at (its own outcome stands).
        if finding.get(import_module("app.review.assistant.splash_views").FIELD) is None:
            _field_cut(item, step, ctx)
        return
    if finding.get("status") != "check":
        return
    splash = import_module("app.review.assistant.splash_views")
    record = finding.get(splash.FIELD)
    if isinstance(record, Mapping) and record.get("outcome") in ("likely_problem", "same"):
        return  # the splash step compared it on the architect's words: its outcome stands
    checks = finding.get("checks") or {}
    if checks.get("printed_vendor") != "match" or checks.get("printed_architect") != "match":
        return
    dims = finding.get("dims") or {}
    vendor, architect = dims.get("vendor") or {}, dims.get("architect") or {}
    if vendor.get("orientation", "h") != architect.get("orientation", "h"):
        return
    inches = finding.get("inches") or {}
    a, v = _decimal(inches.get("architect")), _decimal(inches.get("vendor"))
    if a is None or v is None:
        return
    unlike = splash.unlike(finding, ctx.phrases if ctx is not None else None)
    if unlike is not None:
        run, refused = _splash_wall_run(finding, ctx) if _same(a, v) else (None, None)
        if run is not None:
            _settle_wall_run(item, step, run)
            return
        if not isinstance(finding.get(splash.FIELD), Mapping):
            # The splash step's own reason (the views it looked at) is kept when it has one.
            finding[splash.FIELD] = splash.not_alike(unlike)
            item.changed = True
        item.note(
            step, "no_change", f"not like for like: {unlike}" + (f"; {refused}" if refused else "")
        )
        return
    if _same(a, v):
        _settle_right(
            item,
            step,
            f"code read {inch_words(v)} at the vendor's spot and {inch_words(a)} at the "
            "architect's: the same",
        )
        return
    if _field_cut(item, step, ctx, status="different"):
        return
    finding["status"] = "different"
    item.changed = True


def _field_cut(
    item: _Item, step: StepName, ctx: EscalationContext | None, *, status: str = "different"
) -> bool:
    """GV's field cut on a difference code read at both spots (`field_cut.vet`, settled with GV):
    the vendor's overall that is the architect's value plus one field cut per wall end the drawing
    shows is Right, as when the item is recorded (`chat_findings`); a vendor label that prints the
    field cut itself is never compared with the architect (the item stays as it is). True when the
    rule spoke. The record-time check never saw an item the reading left open and code then read
    as a difference here (a top with its field cut was Right on one page and Not compared on its
    reversed twin)."""
    finding = item.finding
    if ctx is None or ctx.field_cuts is None:
        return False
    checks = finding.get("checks") or {}
    if checks.get("printed_vendor") != "match" or checks.get("printed_architect") != "match":
        return False
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else None
    vendor = (dims or {}).get("vendor")
    architect = (dims or {}).get("architect")
    if not isinstance(vendor, Mapping) or not isinstance(vendor.get("page"), int):
        return False
    if not isinstance(architect, Mapping) or vendor.get("orientation", "h") != architect.get(
        "orientation", "h"
    ):
        return False
    # The labels the rule reads must print the two values compared (a label kept from an older
    # reading of the item is never the rule's).
    inches = finding.get("inches") or {}
    for side, label in (("vendor", vendor), ("architect", architect)):
        if not _same(_decimal(label.get("inches")), _decimal(inches.get(side))):
            return False
    file = str(vendor.get("file") or ctx.main_file or "")
    try:
        cuts = ctx.field_cuts(file, int(vendor["page"]))
    except Exception:  # noqa: BLE001 - no field cut known: the item is kept as it is
        _log.exception("escalation: field cut not worked out (page %s)", vendor.get("page"))
        return False
    rule = import_module("app.review.assistant.field_cut")
    vetted = rule.vet(
        dims,
        status=status,
        source=finding.get("source"),
        architect=finding.get("architect"),
        cuts=cuts,
    )
    if vetted is None:
        return False
    if vetted.outcome == "refuse":
        item.note(step, "no_change", vetted.words)
        return True
    finding["field_cut"] = vetted.as_dict()
    _settle_right(item, step, vetted.words)
    return True


def _under_markup_step(item: _Item, ctx: EscalationContext) -> None:
    """The drawing's own text under GV's markup, inside the vendor's box: one size there is the
    vendor's value."""
    finding = item.finding
    if ctx.under is None or finding.get("keep_reason") != "unreadable":
        return
    if _decimal((finding.get("inches") or {}).get("vendor")) is not None:
        return
    where = _box(finding.get("vendor_box"))
    if where is None:
        return
    name, page, box = where
    checks = import_module("app.review.assistant.finding_checks")
    try:
        words = ctx.under(name or ctx.main_file or "", page, box)
    except Exception as failed:  # noqa: BLE001 - a step that cannot run settles nothing
        item.note("under_markup", "no_change", f"could not read under the markup ({failed})")
        return
    # The drawing's own ink only (on GV's combined sheets coloured text may be GV's; on an
    # ordinary upload `under_markup` marks nothing coloured), joined line by line so "33 1/4\""
    # is one size, not two.
    lines: dict[int, list[Mapping[str, Any]]] = {}
    for word in words:
        if not word.get("coloured"):
            lines.setdefault(round(float(word["box"][1]) / 4), []).append(word)
    sizes: dict[Decimal, str] = {}
    for row in lines.values():
        text = " ".join(str(w.get("text") or "") for w in sorted(row, key=lambda w: w["box"][0]))
        for token in checks.dimension_tokens(text):
            value = checks.to_inches(token)
            if value is not None:
                sizes.setdefault(value, token)
    if len(sizes) != 1:
        item.note(
            "under_markup",
            "no_change",
            "no single size under the markup" if not sizes else "more than one size there",
        )
        return
    value, text = next(iter(sizes.items()))
    inches = dict(finding.get("inches") or {})
    inches["vendor"] = str(value)
    finding.update(inches=inches, vendor=text)
    _check(item, "printed_vendor", "match")
    item.changed = True
    item.note("under_markup", "read", f"code read {text} under GV's markup")
    _printed_compare(item, "under_markup", ctx)


def _carrier(
    finding: Mapping[str, Any], others: Sequence[dict[str, Any]], areas: Mapping[int, str]
) -> dict[str, Any] | None:
    """Another copy of the same item (the same area, the same item words, the same vendor value)
    that already carries it: a proven Wrong first, else the newest undecided round."""
    key = round_item_key(finding)
    area = areas.get(int(finding.get("page") or 0))
    value = _decimal((finding.get("inches") or {}).get("vendor"))
    if not key or area is None:
        return None
    copies = [
        other
        for other in others
        if other.get("id") != finding.get("id")
        and round_item_key(other) == key
        and areas.get(int(other.get("page") or 0)) == area
        and (value is None or _same(_decimal((other.get("inches") or {}).get("vendor")), value))
    ]
    proven = [c for c in copies if proof.proof_group(c) == "clouded"]
    if proven:
        return proven[0]
    rounds = [c for c in copies if undecided(c)]
    if not rounds:
        return None
    newest = max(rounds, key=lambda c: (int(c.get("round") or 1), int(c.get("page") or 0)))
    mine = (int(finding.get("round") or 1), int(finding.get("page") or 0))
    later = (int(newest.get("round") or 1), int(newest.get("page") or 0))
    return newest if later > mine else None


def _vendor_sum_carrier(
    finding: Mapping[str, Any], others: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """A vendor string code proved does not add up (the consistency check's), on the same page,
    whose printed overall is this item's vendor value."""
    if finding.get("keep_reason") != "vendor_disagrees" and finding.get("source") != (
        "vendor_arithmetic"
    ):
        return None
    value = _decimal((finding.get("inches") or {}).get("vendor"))
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    for other in others:
        if (
            other.get("id") == finding.get("id")
            or other.get("page") != finding.get("page")
            or other.get("created_by") != "consistency"
            or proof.proof_group(other) != "clouded"
        ):
            continue
        total = to_inches(str(other.get("printed_total") or other.get("vendor") or ""))
        if value is not None and _same(total, value):
            return other
    return None


def _fold(item: _Item, carrier: dict[str, Any], step: StepName, result: Escalation) -> None:
    """The item joins the copy that carries it: listed on it (`also_on`, as the resolver's), so the
    drawing marks this page too; dropped from the answer with the reason."""
    reason = (
        f"Same item as {carrier.get('id')} (page {carrier.get('page')}), which carries it: "
        f"{'a proven Wrong' if proof.proof_group(carrier) == 'clouded' else 'one question'}."
    )
    finding = item.finding
    also = list(carrier.get("also_on") or [])
    if not any(entry.get("finding_id") == finding["id"] for entry in also):
        also.append(
            {
                key: finding.get(source)
                for key, source in (
                    ("finding_id", "id"),
                    ("page", "page"),
                    ("vs_page", "vs_page"),
                    ("round", "round"),
                    ("item", "item"),
                    ("vendor", "vendor"),
                    ("architect", "architect"),
                    ("vendor_box", "vendor_box"),
                )
            }
        )
        carrier["also_on"] = also
        if not any(f is carrier for f in result.changed):
            result.changed.append(carrier)
    result.absorbed[str(finding["id"])] = reason
    item.note(step, "folded", reason)
    item.outcome = "folded"


def _pairs_step(item: _Item, ctx: EscalationContext) -> None:
    """Code paired the item's two labels itself, by drawn position: the pairing is proved."""
    finding = item.finding
    if finding.get("status") != "different" or finding.get("paired_by") == "code":
        return
    dims = finding.get("dims") or {}
    vendor, architect = dims.get("vendor"), dims.get("architect")
    if not isinstance(vendor, Mapping) or not isinstance(architect, Mapping):
        item.note("pairs", "no_change", "the item's values are not both on code's labels")
        return
    paired = ctx.pairs.get(_ref(vendor))
    if paired is None:
        item.note("pairs", "no_change", f"code made no pairing for {_ref(vendor)}")
        return
    if paired != _ref(architect):
        item.note("pairs", "no_change", f"code pairs {_ref(vendor)} with {paired}, not this one")
        return
    finding["paired_by"] = "code"
    item.changed = True
    item.note("pairs", "proved", f"code paired {_ref(vendor)} with {paired} by drawn position")
    if proof.proof_group(judged(finding)) == "clouded":
        item.outcome = "wrong"


def _chain_step(item: _Item, ctx: EscalationContext) -> None:
    """The reference's chain summed like a printed overall (fix 4, 2026-10-25; #1909's rule for the
    chains code proves): the item's architect label is one part of a chain on the reference's
    drawing (`dim_ends.chain_of`: parts end to end on one dimension line, an extension line at
    every end and joint, the line stopping at the run's ends, no overall printed), every part
    printed as text. When the whole run's sum is the vendor's value, and the vendor's dimension
    and the run measure between ends that are never unlike (`dim_ends.alike`, #1930), the item is
    Right, its architect side the parts and their sum. A different sum changes nothing: the AI
    paired the vendor's dimension with one part, never with the run, so it stays as it was."""
    finding = item.finding
    if finding.get("status") not in ("different", "check") or finding.get("architect_parts"):
        return
    if finding.get("source") not in ("architect", None) or proof.reviewer_decided(finding):
        return
    checks = finding.get("checks") or {}
    if checks.get("printed_vendor") != "match" or checks.get("printed_architect") != "match":
        return
    dims = dict(finding["dims"]) if isinstance(finding.get("dims"), Mapping) else {}
    vendor_side, architect_side = dims.get("vendor"), dims.get("architect")
    if not isinstance(vendor_side, Mapping) or not isinstance(architect_side, Mapping):
        return
    inches = dict(finding.get("inches") or {})
    for side, label in (("vendor", vendor_side), ("architect", architect_side)):
        if not _same(_decimal(label.get("inches")), _decimal(inches.get(side))):
            return  # the labels must print the two values compared
        if label.get("route") != "text":
            return  # a chain is summed only from values printed as text
    vendor_page, architect_page = _page_of(ctx, vendor_side), _page_of(ctx, architect_side)
    if vendor_page is None or architect_page is None:
        return
    vendor = vendor_page.ends(vendor_side.get("id"))
    chain = dim_ends.chain_of(architect_page, architect_side.get("id"))
    if chain is None or vendor is None or vendor.span.axis != chain.run.span.axis:
        return
    file, page = architect_page.file, architect_page.page
    labels = {str(label.get("id")): label for label in ctx.labels.get((file, page), [])}
    parts = [labels.get(part.id) for part in chain.parts]
    if any(
        part is None
        or part.get("route") != "text"
        or part.get("derived")
        or part.get("side") != "architect"
        for part in parts
    ):
        item.note("chain_sums", "no_change", "a part of the architect's chain is not printed text")
        return
    words = " + ".join(format_inches(part.inches) for part in chain.parts)
    total, value = chain.inches, Fraction(vendor.span.inches)
    # A splash's size is never settled against a size code does not see named as the same
    # splash's (`splash_views.unlike`, #1918): the reference's run is the top's.
    splash = import_module("app.review.assistant.splash_views").unlike(finding, ctx.phrases)
    wall_run = None
    if splash is not None:
        # Except a splash's length against the top's run between two walls code sees (Anant,
        # 2026-10-25): equal is Right, a different sum changes nothing (below).
        wall_run, refused = _splash_wall_run(finding, ctx, chain.run)
        if wall_run is None:
            item.note(
                "chain_sums",
                "no_change",
                f"not like for like: {splash}" + (f"; {refused}" if refused else ""),
            )
            return
    verdict, why = dim_ends.alike(vendor, chain.run)
    if verdict == "unlike":
        item.note("chain_sums", "no_change", f"the architect's chain {words}: {why}")
        return
    if total != value:
        extent, proved_why = (
            dim_ends.same_kinds_run(vendor_page, vendor, architect_page, chain)
            if wall_run is None
            else ("cannot_tell", "a splash's length that differs from the run changes nothing")
        )
        if extent == "same_extent":
            # GV's formula (top width = the chain's sum, final plan #9): code proved the vendor's
            # dimension and the whole run end at the same points, so the difference is the
            # formula's. A new kind of check: a Likely problem, never red.
            _chain_difference(item, chain, value, words, proved_why)
            return
        item.note(
            "chain_sums",
            "no_change",
            f"the architect's chain {words} adds to {format_inches(total)}; the vendor prints "
            f"{format_inches(value)}: code did not pair the vendor's dimension with the whole run",
        )
        return
    boxes = [[float(v) for v in part.get("box") or (0, 0, 0, 0)] for part in parts if part]
    box = [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]
    ids = [part.id for part in chain.parts]
    texts = [str(part.get("text") or "") for part in parts if part]
    summed = str(Decimal(total.numerator) / Decimal(total.denominator))
    dims["architect"] = {
        "id": "+".join(ids),
        "page": page,
        "file": file,
        "text": " + ".join(texts),
        "inches": summed,
        "route": "text",
        "side": "architect",
        "orientation": architect_side.get("orientation"),
        "box": box,
        "derived": {"sum_of": ids, "parts": texts},
    }
    inches.update(architect=summed, difference="0")
    finding.update(
        dims=dims,
        inches=inches,
        architect=f"{words} = {format_inches(total)}",
        arch_box={
            "x0": box[0],
            "y0": box[1],
            "x1": box[2],
            "y1": box[3],
            "file": file,
            "page": page,
        },
        architect_parts={
            "parts": [format_inches(part.inches) for part in chain.parts],
            "total": format_inches(total),
            "vendor": format_inches(value),
            "chain_proved": True,
            "ends": chain.run.as_dict(),
        },
    )
    how = (
        f"the architect prints no overall here, only parts ({words} = {format_inches(total)}, one "
        "chain end to end on one dimension line); their sum is the vendor's "
        f"{format_inches(value)} ({why})"
    )
    if wall_run is not None:
        _settle_wall_run(item, "chain_sums", f"{how}; {wall_run}")
        return
    finding[formulas.FIELD] = formulas.record(
        formulas.CHAIN, "same", how, expected=total, labels={"vendor": vendor.span.id, "parts": ids}
    )
    _settle_right(item, "chain_sums", formulas.right_how(formulas.CHAIN, how))


def _chain_difference(
    item: _Item,
    chain: dim_ends.Chain,
    value: Fraction,
    words: str,
    proved_why: str,
) -> None:
    """A vendor top width that differs from the reference chain's sum, where code proved both end
    at the same points (`dim_ends.same_kinds_run`): GV's formula's Likely problem ("please check
    and confirm"), never red (`formulas`, kind `formula_chain`, no keyed cases yet)."""
    finding = item.finding
    total = chain.inches
    difference = abs(total - value)
    how = (
        f"the reference's chain {words} = {format_inches(total)}; the vendor prints "
        f"{format_inches(value)}, {format_inches(difference)} "
        f"{'more' if value > total else 'less'} ({proved_why})"
    )
    finding[formulas.FIELD] = formulas.record(
        formulas.CHAIN,
        "different",
        how,
        expected=total,
        labels={"vendor": chain.run.span.id, "parts": [part.id for part in chain.parts]},
    )
    finding.update(status="check", architect=f"{words} = {format_inches(total)}")
    finding[likely_problems.FIELD] = {
        "by": formulas.CHAIN.kind,
        "kind": "size",
        "proof": "likely_only",
        "sure": "likely",
        "why": how,
        "words": formulas.please_check(formulas.CHAIN, how),
    }
    finding.pop("settled", None)
    item.changed = True
    item.note("chain_sums", "read", f"GV's formula: {how}")


def _splash_wall_run(
    finding: Mapping[str, Any],
    ctx: EscalationContext | None,
    architect: dim_ends.Ends | None = None,
) -> tuple[str | None, str | None]:
    """A splash's length compared with the reference's run of the top between two walls (Anant,
    2026-10-25, relaxing #1918's "never against a size not printed as the splash's" for this one
    case): `(why it may be compared, None)`, or `(None, why not)`, or `(None, None)` when the item
    is not such a splash length. #1918 was written after a splash length was matched with a top's
    WIDTH, so all of this must hold, else nothing is compared:

    - the vendor's words say it is the splash's length, of no kind but the back one
      (`splash_views.wall_run_part`; never a height, thickness, width of the top or side splash);
    - both labels have their drawn dimension lines, along the same direction;
    - the reference's dimension (`architect`: its label's, or the chain's whole run) runs from
      wall to wall: a wall drawn at both ends and neither end a cabinet's side, the overhang's
      edge or a field cut (`dim_ends.wall_to_wall`);
    - neither end of the vendor's dimension is known to be anything but a wall, and the two are
      never unlike (`dim_ends.alike`, #1930).

    The caller compares only equal values (a Right); a difference changes nothing."""
    if ctx is None:
        return None, None
    splash = import_module("app.review.assistant.splash_views")
    part = splash.wall_run_part(finding, ctx.phrases)
    if part is None:
        return None, None
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    vendor_side, architect_side = (dims or {}).get("vendor"), (dims or {}).get("architect")
    if not isinstance(vendor_side, Mapping) or not isinstance(architect_side, Mapping):
        return None, None
    vendor_page, architect_page = _page_of(ctx, vendor_side), _page_of(ctx, architect_side)
    if vendor_page is None or architect_page is None:
        return None, "code could not read the dimension lines of both drawings"
    vendor = vendor_page.ends(vendor_side.get("id"))
    chain = architect
    if architect is None:
        architect = architect_page.ends(architect_side.get("id"))
    if vendor is None or architect is None:
        return None, "code found no drawn dimension line for both sizes"
    # The dimensions code proves things about must be the ones whose values are compared (a
    # label kept from an older reading of the item never is).
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}

    def compared(span: dim_ends.Span, side: str) -> bool:
        value = _decimal((inches or {}).get(side))
        drawn = Fraction(span.inches)
        return value is not None and _same(Decimal(drawn.numerator) / drawn.denominator, value)

    if not compared(vendor.span, "vendor"):
        return None, "the vendor's dimension line is not the size compared"
    if chain is None and not compared(architect.span, "architect"):
        return None, "the architect's dimension line is not the size compared"
    if vendor.span.axis != architect.span.axis:
        return None, "the two dimensions do not run the same way"
    for end in (vendor.low, vendor.high):
        known = dim_ends.not_a_wall(end)
        if known:
            return None, f"the vendor's {part.part} length ends at {known}"
    verdict, unlike_why = dim_ends.alike(vendor, architect)
    if verdict == "unlike":
        return None, unlike_why
    walls, why = dim_ends.wall_to_wall(architect_page, architect)
    size = format_inches(architect.span.inches)
    if not walls:
        return None, f"the architect's {size} is not a run from wall to wall: {why}"
    return (
        f"the architect's {size} is the top's run from wall to wall ({why}), so a back "
        f"{part.part} along that wall is as long; the vendor's {part.part} length is "
        f"{format_inches(vendor.span.inches)}"
    ), None


def _settle_wall_run(item: _Item, step: StepName, how: str) -> None:
    """Right on a splash's length the same as the top's run between two walls (`_splash_wall_run`),
    marked so the like-for-like step keeps it (`splash_views.downgrade_unlike`)."""
    splash = import_module("app.review.assistant.splash_views")
    record = item.finding.get(splash.FIELD)
    item.finding[splash.FIELD] = {
        **(dict(record) if isinstance(record, Mapping) else {}),
        "outcome": "same",
        "by": splash.WALL_RUN,
        "why": how,
        # The two labels it was proved for: a later step that pairs the item with other labels
        # is never kept by this record (`splash_views.downgrade_unlike`).
        "labels": splash.label_keys(item.finding),
    }
    item.finding[formulas.FIELD] = formulas.record(formulas.SPLASH, "same", how)
    _settle_right(item, step, formulas.right_how(formulas.SPLASH, how))


def _vendor_drawings(
    ctx: EscalationContext, page: int
) -> list[tuple[int, Sequence[float], Sequence[Mapping[str, Any]]]]:
    """The vendor's drawings of `page`'s area (every round): (page, the drawing's box, the page's
    labels), from the page map. Empty when the page has no area there."""
    file = ctx.main_file or ""
    entries = [
        e
        for e in (ctx.page_map or {}).get("pages") or []
        if isinstance(e, Mapping) and e.get("file") in (None, file)
    ]
    own = next((e for e in entries if e.get("page") == page), None)
    area = None if own is None else (own.get("area") or own.get("section"))
    if not area:
        return []
    found: list[tuple[int, Sequence[float], Sequence[Mapping[str, Any]]]] = []
    for entry in entries:
        if (entry.get("area") or entry.get("section")) != area:
            continue
        number = entry.get("page")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        for half in entry.get("halves") or []:
            box = half.get("box") if isinstance(half, Mapping) else None
            if half.get("role") == "vendor" and isinstance(box, Sequence) and len(box) == 4:
                found.append((number, box, ctx.labels.get((file, number), [])))
    return found


def _vendor_search_step(
    item: _Item, ctx: EscalationContext, live: Sequence[dict[str, Any]] = ()
) -> None:
    """An item the AI says the vendor's drawing does not show: code searches the vendor's drawings
    of its area for the architect's printed value and keeps what it found on the item (see the
    module). A millwork item code proves missing (`missing_items.judge`) is red; any other item
    keeps its outcome, and the search says why it is not red."""
    finding = item.finding
    if not classification.missing_on_vendor(finding):
        return
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    architect = (dims or {}).get("architect")
    label = _label_of(ctx, architect) if isinstance(architect, Mapping) else None
    claim = label.get("text") if label is not None else finding.get("architect")
    page = (label or {}).get("page") or finding.get("vs_page") or finding.get("page")
    if not isinstance(page, int) or isinstance(page, bool):
        return
    drawings = _vendor_drawings(ctx, page) or _vendor_drawings(ctx, int(finding.get("page") or 0))
    found = search_area(claim, (label or {}).get("orientation"), drawings)
    if found.found == "unknown":
        item.note("vendor_search", "no_change", "no value or no vendor drawing to search")
        return
    search = found.as_dict()
    carrier = _missing_carrier(finding, live)
    if found.found == "none" and carrier is not None:
        # The same architect label is already a proved missing item: this copy folds into it.
        search["not_red"] = f"the same item as {carrier.get('id')}, which carries it"
    elif found.found == "none":
        verdict = _judge_missing(finding, label, ctx, page, live)
        if verdict.red:
            red = _missing_red(finding, verdict, search)
            if proof.proof_group(red) == "clouded":
                finding.clear()
                finding.update(red)
                item.changed = True
                item.note("vendor_search", "proved", verdict.why)
                return
            search["not_red"] = "code's proof did not pass"
        else:
            search["not_red"] = verdict.why
    finding[VENDOR_SEARCH] = search
    item.changed = True
    item.note("vendor_search", "searched", search_words(finding[VENDOR_SEARCH]) or "")


# ---- before the ladder: GV's stickers and the tag reader (#1859) ---------------------------------

#: The finding's key for the GV sticker its vendor value sits in (`classification.GV_STICKER`).
GV_STICKER: Final = classification.GV_STICKER


def _norm_words(text: object) -> str:
    return re.sub(r"[^A-Z0-9/]+", " ", str(text or "").upper()).strip()


#: A size alone, once its words in brackets are left out: `48" (45" between fillers)` is `48"`.
_BRACKETED: Final = re.compile(r"\([^)]*\)")
_ONE_SIZE: Final = re.compile(r"[0-9][0-9 /'\-\".\[\]]*")


def _claim_kind(claim: str) -> tuple[str, Any]:
    """What a claimed vendor value is: ("tags", keys), ("size", inches) or ("words", text)."""
    checks = import_module("app.review.assistant.finding_checks")
    tags = import_module("app.review.assistant.tag_reader").tag_keys(claim)
    if tags:
        return "tags", tags
    bare = " ".join(_BRACKETED.sub(" ", claim).split())
    value = checks.claim_value(bare)
    inches = checks.to_inches(value) if value is not None else None
    if inches is not None and _ONE_SIZE.fullmatch(bare):
        return "size", inches
    return "words", _norm_words(claim)


def _sizes(text: str) -> set[Any]:
    checks = import_module("app.review.assistant.finding_checks")
    text = re.sub(r"/\s+", "/", text)
    return {
        checks.to_inches(token)
        for token in checks.dimension_tokens(text)
        if checks.to_inches(token) is not None
    }


def _same_as_sticker(claim: object, sticker_text: object) -> bool:
    """The claimed vendor value is the sticker's: the same size, the same tags, or the same
    words."""
    said, there = " ".join(str(claim or "").split()), " ".join(str(sticker_text or "").split())
    if not said or not there:
        return False
    kind, value = _claim_kind(said)
    if kind == "tags":
        found = import_module("app.review.assistant.tag_reader").tag_keys(there)
        return bool(found) and set(value) <= set(found)
    if kind == "size":
        return value in _sizes(there)
    y = _norm_words(there)
    return len(value) >= 6 and len(y) >= 6 and (value in y or y in value)


def _vendor_prints_it(
    ctx: EscalationContext, file: str, page: int, spot: Sequence[float], claim: str
) -> bool:
    """The vendor's own ink at the item's spot prints the claimed value too (code's labels and
    phrases never read GV's coloured markup): then the value is the vendor's, sticker or not."""
    kind, value = _claim_kind(" ".join(claim.split()))
    if kind == "size":
        return any(
            lab.get("side") in ("vendor", None)
            and _inside(_centre(lab.get("box") or (0, 0, 0, 0)), spot, 2.0)
            and _same(_decimal(lab.get("inches")), value)
            for lab in ctx.labels.get((file, page), [])
        )
    here = " ".join(
        str(p.get("text") or "")
        for p in ctx.phrases.get((file, page), [])
        if p.get("side") in ("vendor", None) and _overlaps(spot, p.get("box"))
    )
    if kind == "tags":
        return set(value) <= set(import_module("app.review.assistant.tag_reader").tag_keys(here))
    return len(value) >= 6 and value in _norm_words(here)


def _under_sticker(
    ctx: EscalationContext, file: str, page: int, sticker: Mapping[str, Any], spot: Sequence[float]
) -> tuple[str | None, str]:
    """The vendor's own value code reads under the sticker, and how; `None` with why when code
    reads none. A size: code's one label there, else the read-number tool at the sticker's box.
    Words or a tag: the drawing's printed words there (at the item's spot), else the tag reader's
    strokes."""
    try:
        box = tuple(float(v) for v in sticker.get("box") or ())
    except (TypeError, ValueError):
        return None, "the sticker has no box"
    if len(box) != 4:
        return None, "the sticker has no box"
    checks = import_module("app.review.assistant.finding_checks")
    text = str(sticker.get("text") or "")
    if any(checks.to_inches(token) is not None for token in checks.dimension_tokens(text)):
        labels = [
            lab
            for lab in ctx.labels.get((file, page), [])
            if lab.get("side") in ("vendor", None)
            and _inside(_centre(lab.get("box") or (0, 0, 0, 0)), box, 3.0)
        ]
        if len(labels) == 1:
            return (
                str(labels[0].get("text") or ""),
                f"code's label {_ref(dict(labels[0], page=page))}",
            )
        if len(labels) > 1:
            return None, "several of the vendor's labels sit under it"
        if ctx.reread is None:
            return None, "no code reading of the vendor's ink there"
        numbers = import_module("app.review.assistant.read_number")
        try:
            reading = ctx.reread(numbers.Request(file, page, "vendor", box=box))
        except Exception as failed:  # noqa: BLE001 - a tool that cannot run reads nothing
            return None, f"the read-number tool could not run ({failed})"
        if reading.outcome == "read" and reading.text:
            return str(reading.text), f"the read-number tool ({' and '.join(reading.agreed)})"
        return None, str(reading.reason or "not read")
    words = [
        str(p.get("text") or "").strip()
        for p in ctx.phrases.get((file, page), [])
        if p.get("side") in ("vendor", None)
        and _inside(_centre(p.get("box") or (0, 0, 0, 0)), box, 1.0)
        and _overlaps(spot, p.get("box"))
        and re.search(r"[A-Za-z]{2,}", str(p.get("text") or ""))
    ]
    if words:
        return " ".join(dict.fromkeys(words)), "the drawing's printed text"
    if not import_module("app.review.assistant.tag_reader").tag_keys(text):
        return None, "code reads none of the vendor's printed words under it"
    if ctx.strokes is not None:
        try:
            tags, read, _ink, why = ctx.strokes(file, page, box)
        except Exception as failed:  # noqa: BLE001 - a reader that cannot run reads nothing
            return None, f"the tag reader could not run ({failed})"
        if tags:
            return str(read), "the tag reader (two OCR engines)"
        return None, str(why or "no printed words under it")
    return None, "no printed words under it"


def _sticker_step(item: _Item, ctx: EscalationContext) -> None:
    """A vendor value inside one of GV's stickers that prints the same value is GV's (#1859)."""
    finding = item.finding
    if finding.get(GV_STICKER) is not None or finding.get("created_by") in _CODE_MADE:
        return
    where = _box(finding.get("vendor_box"))
    if where is None:
        return
    name, page, box = where
    file = name or ctx.main_file or ""
    stickers = import_module("app.review.assistant.gv_stickers")
    on_page = ctx.stickers.get((file, page), [])
    near = [
        s
        for s in on_page
        if stickers.sticker_at([s], box, 4.0) is not None or _overlaps(box, s.get("box"))
    ]
    hit = next((s for s in near if _same_as_sticker(finding.get("vendor"), s.get("text"))), None)
    if hit is None or _vendor_prints_it(ctx, file, page, box, str(finding.get("vendor") or "")):
        return
    under, how = _under_sticker(ctx, file, page, hit, box)
    finding[GV_STICKER] = {
        "page": page,
        "box": [float(v) for v in hit.get("box") or ()],
        "text": str(hit.get("text") or ""),
        "under": under,
        "how": how,
    }
    item.changed = True
    item.note(
        "gv_sticker",
        "refused",
        f"the vendor's {finding.get('vendor')} is GV's sticker on page {page}; "
        + (f"under it the vendor prints {under} ({how})" if under else f"under it: {how}"),
    )


def _overlaps(box: Sequence[float], other: object) -> bool:
    if not isinstance(other, Sequence) or isinstance(other, str):
        return False
    try:
        o = [float(v) for v in other]
    except (TypeError, ValueError):
        return False
    return len(o) == 4 and box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3]


#: Findings code made itself (never an AI's claim to refuse or re-read).
_CODE_MADE: Final = frozenset({"architect_items", "consistency", "ledger"})


def _tag_step(item: _Item, ctx: EscalationContext) -> None:
    """An item whose two sides name tags is read and compared by code; a note in words that would
    be "Illegible" gets its true reason (`tag_reader`)."""
    finding = item.finding
    tags = import_module("app.review.assistant.tag_reader")
    if (
        finding.get(tags.TAG_READ) is not None
        or finding.get(GV_STICKER) is not None
        or finding.get("created_by") in _CODE_MADE
        or classification.internal(finding)
        or proof.reviewer_decided(finding)
        or finding.get("status") not in ("same", "different", "check")
    ):
        return
    tag_item = tags.is_tag_item(finding)
    words = (
        not tag_item
        and tags.words_only(finding.get("vendor"))
        and proof.classified(finding).reason == "unreadable"
    )
    if not tag_item and not words:
        return
    where = _box(finding.get("vendor_box"))
    if where is None:
        return
    name, page, box = where
    file = name or ctx.main_file or ""

    def spot(at: tuple[str | None, int, tuple[float, float, float, float]]) -> Any:
        f = at[0] or ctx.main_file or ""
        return tags.read_spot(
            f,
            at[1],
            at[2],
            ctx.phrases.get((f, at[1]), []),
            page_map=ctx.page_map,
            strokes=ctx.strokes,
        )

    vendor = spot((file, page, box))
    if tag_item:
        arch_at = _box(finding.get("arch_box"))
        architect = spot(arch_at) if arch_at is not None else None
        decision = tags.decide(
            finding.get("vendor"),
            finding.get("architect"),
            vendor,
            architect,
            prototype_reference=classification.prototype_governs(finding),
            changes=ctx.changes,
        )
    else:
        architect = None
        decision = tags.decide_words(finding.get("vendor"), finding.get("architect"), vendor)
    finding[tags.TAG_READ] = {
        **decision.as_dict(),
        "vendor": vendor.as_dict(),
        **({"architect": architect.as_dict()} if architect is not None else {}),
    }
    item.changed = True
    if decision.outcome == "right":
        _settle_right(item, "tags", decision.why)
    elif decision.outcome == "wrong" and decision.proof is not None:
        finding.update(status="different", paired_by="code", text_proof=dict(decision.proof))
        _check(item, "text", "agrees")
    item.note("tags", decision.outcome, decision.why)


#: A claim about a notch (or a stepped end) in the top.
_NOTCH_CLAIM: Final = re.compile(r"\bnotch\w*|\bstepped\b|\bjogg?\w*", re.IGNORECASE)
#: Room round a claim's box where the notch's step may be drawn (points).
_NOTCH_PAD_PT: Final = 12.0
#: What GV decided (client meeting, 2026-10-23).
NOTCH_RULE: Final = (
    "a notch in the top (code found its step on the vendor's drawing): GV cuts unusual notches "
    "on site, so a notch is never compared (GV, client meeting 2026-10-23)"
)


def _page_of(ctx: EscalationContext, side: object) -> dim_ends.Page | None:
    if ctx.pages is None or not isinstance(side, Mapping):
        return None
    try:
        page = int(side.get("page") or 0)
    except (TypeError, ValueError):
        return None
    file = str(side.get("file") or ctx.main_file or "")
    try:
        return ctx.pages(file, page)
    except Exception:  # noqa: BLE001 - a page code cannot read: nothing to say about its ends
        _log.exception("escalation: ends not read (p%s)", page)
        return None


def _notch_claim(item: _Item, ctx: EscalationContext) -> str | None:
    """A claim about a notch in the top (the whole-drawing look's "stepped end") where code finds a
    notch's step on the vendor's drawing in the claim's box: GV's decided rule drops it."""
    finding = item.finding
    if finding.get("status") not in ("check", "different") or proof.reviewer_decided(finding):
        return None
    words = " ".join(str(finding.get(key) or "") for key in ("item", "vendor"))
    if not _NOTCH_CLAIM.search(words):
        return None
    where = _box(finding.get("vendor_box"))
    if where is None:
        return None
    name, page_no, box = where
    page = _page_of(ctx, {"page": page_no, "file": name or ctx.main_file})
    if page is None:
        return None
    pad = _NOTCH_PAD_PT
    steps = page.notches_in((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad))
    if not steps:
        item.note("ends", "no_change", "a notch named, but code found no notch's step there")
        return None
    return f"Not compared: {NOTCH_RULE}."


#: A claim that the top is L-shaped or has a return.
_RETURN_CLAIM: Final = re.compile(r"\bL[\s-]?shaped?\b|\breturns?\b", re.IGNORECASE)
_NO_RETURN: Final = re.compile(
    r"\b(?:no|not|without|never)\b[^.;,]*\b(?:L[\s-]?shaped?|returns?)\b", re.IGNORECASE
)
_RIGHT_END: Final = re.compile(r"\bright\b", re.IGNORECASE)
_LEFT_END: Final = re.compile(r"\bleft\b", re.IGNORECASE)
#: A size longer than this both ways in the box may be a real L-shaped top's two legs (inches).
_RETURN_LEG_IN: Final = Fraction(30)
#: The least a top's printed length is (inches) for its ends to say where the top stops.
_TOP_MIN_IN: Final = Fraction(12)
WALL_RETURN: Final = (
    "the L drawn on the reference is its wall, not the top: the reference's top sizes stop at the "
    "wall's face there (code found the wall drawn as a band beyond them)"
)


def _wall_return_claim(item: _Item, ctx: EscalationContext) -> str | None:
    """A claim that the reference's top is L-shaped (has a return) where code finds that the
    reference's top sizes in the claim's box stop at a wall drawn beyond them (`dim_ends.wall_band`):
    the L is the wall's outline, not the top; the claim is dropped."""
    finding = item.finding
    if finding.get("status") not in ("check", "different") or proof.reviewer_decided(finding):
        return None
    if finding.get("dims"):
        return None  # an item about printed sizes: the like-for-like step reads it
    claim = str(finding.get("architect") or "")
    if not _RETURN_CLAIM.search(claim) or _NO_RETURN.search(claim):
        return None  # the reference's own side must be said to have the L (not "no return")
    where = _box(finding.get("arch_box"))
    if where is None:
        return None
    name, page_no, box = where
    if not name:
        return None
    page = _page_of(ctx, {"page": page_no, "file": name})
    if page is None:
        return None
    boxes: dict[str, Sequence[float]] = {
        str(label.get("id")): label["box"]
        for label in ctx.labels.get((name, page_no), [])
        if isinstance(label.get("box"), Sequence) and len(label["box"]) == 4
    }
    right, left = bool(_RIGHT_END.search(claim)), bool(_LEFT_END.search(claim))
    in_box = [
        span
        for span in page.spans.values()
        if span.id in boxes and _inside(_centre(boxes[span.id]), box, 0.0)
    ]
    if any(span.inches > _RETURN_LEG_IN for span in in_box if span.axis != "h") and any(
        span.inches > _RETURN_LEG_IN for span in in_box if span.axis == "h"
    ):
        return None  # sizes both ways longer than a top's depth: a real return leg may be there
    for span in in_box:
        if span.inches < _TOP_MIN_IN or span.id not in boxes:
            continue
        if not _inside(_centre(boxes[span.id]), box, 0.0):
            continue
        ends = []
        if not right or left or span.axis != "h":
            ends.append((span.a, -1))
        if not left or right or span.axis != "h":
            ends.append((span.b, 1))
        if any(dim_ends.wall_band(page, span, end, outward) for end, outward in ends):
            return f"Not compared: {WALL_RETURN}."
    return None


def _ends_step(
    item: _Item,
    ctx: EscalationContext,
    live: Sequence[dict[str, Any]],
    result: Escalation,
) -> str | None:
    """Like for like (fix 3, 2026-10-25; `dim_ends`): what each of the item's two code-read labels
    measures from and to. Unlike ends are never compared: the item goes to the GV rule that relates
    them (the sink centre, `sink_centre`; the field cut speaks first, `field_cut.vet`), or is Not
    compared with the plain reason. A notch claim code sees drawn is dropped (GV's rule). Returns
    the reason when the item is dropped."""
    finding = item.finding
    if ctx.pages is None or proof.reviewer_decided(finding):
        return None
    if finding.get("created_by") == sink_centre.CREATED_BY:
        return None
    dropped = _notch_claim(item, ctx) or _wall_return_claim(item, ctx)
    if dropped is not None:
        return dropped
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else None
    vendor_side = (dims or {}).get("vendor")
    architect_side = (dims or {}).get("architect")
    if not isinstance(vendor_side, Mapping) or not isinstance(architect_side, Mapping):
        return None
    if finding.get("source") not in ("architect", None):
        return None
    vendor_page = _page_of(ctx, vendor_side)
    architect_page = _page_of(ctx, architect_side)
    if vendor_page is None or architect_page is None:
        return None
    vendor = vendor_page.ends(vendor_side.get("id"))
    architect = architect_page.ends(architect_side.get("id"))
    if vendor is None or architect is None:
        return None
    if vendor.span.axis != architect.span.axis:
        return None
    if ctx.field_cuts is not None:
        rule = import_module("app.review.assistant.field_cut")
        try:
            cuts = ctx.field_cuts(
                str(vendor_side.get("file") or ctx.main_file or ""),
                int(vendor_side.get("page") or 0),
            )
            vetted = rule.vet(
                dims,
                status="different",
                source=finding.get("source"),
                architect=finding.get("architect"),
                cuts=cuts,
            )
        except Exception:  # noqa: BLE001 - no field cut known: the ends decide
            vetted = None
        if vetted is not None:
            return None  # GV's field-cut rule relates the two (it speaks in the labels step)
    if _length_formula(item, ctx, vendor_page, vendor, architect_page, architect):
        return None
    verdict, why = dim_ends.alike(vendor, architect)
    if verdict != "unlike":
        return None
    cuts_in = dim_ends.field_cut_relief(vendor, architect)
    if cuts_in is not None:
        # GV's field-cut rule relates them (the overall is the run plus one field cut per wall
        # end): the same once the field cuts are taken off leaves the item as it is.
        per_end = Fraction(getattr(vendor_page.field_cuts, "per_end", 1) or 1)
        without = vendor.span.inches - cuts_in * per_end
        if without == architect.span.inches:
            if finding.get("status") != "different":
                return None  # the same once its field cut is off: the item stays as it was
            # Called a difference, but it is the field cut: never a red (GV's settled rule).
            why = (
                f"the vendor's {format_inches(vendor.span.inches)} includes GV's "
                f"{format_inches(per_end)} field cut at {cuts_in} wall end(s); without it the top "
                f"is {format_inches(without)}, the same as the architect's"
            )
            finding[dim_ends.FIELD] = dim_ends.record(finding, vendor, architect, "unlike", why)
            item.changed = True
            item.note("ends", "no_change", why)
            return None
        why = (
            f"{why}; without its field cut the vendor's top is "
            f"{format_inches(without)}, the architect's {format_inches(architect.span.inches)}"
        )
    if {"sink_centre"} & (vendor.low.kinds | vendor.high.kinds):
        routed = sink_centre.route(finding, vendor_page, architect_page)
        if routed is not None:
            carrier = next(
                (
                    other
                    for other in live
                    if other is not finding
                    and str(other.get("id")) not in result.absorbed
                    and sink_centre.same_sink(routed, other)
                ),
                None,
            )
            if carrier is not None:
                return (
                    f"the same sink centre as {carrier.get('id')} (GV Review's sink-centre check)"
                )
            item.finding.clear()
            item.finding.update(routed)
            item.changed = True
            item.note("ends", "read", f"{why}: compared by the sink-centre check instead")
            return None
    finding[dim_ends.FIELD] = dim_ends.record(finding, vendor, architect, verdict, why)
    item.changed = True
    item.note("ends", "no_change", f"not like for like: {why}")
    return None


def _length_formula(
    item: _Item,
    ctx: EscalationContext,
    vendor_page: dim_ends.Page,
    vendor: dim_ends.Ends,
    architect_page: dim_ends.Page,
    architect: dim_ends.Ends,
) -> bool:
    """GV's countertop length formula (final plan #9, `formulas.top_length`) on an item whose two
    dimensions code proved measure the top and the cabinets (`formulas.measures_top_against_run`): the
    vendor's top = the reference's cabinet run + a field cut at each wall end + the end overhang at
    each finished end. Runs only when both labels are the values compared and both agree with their
    drawn lengths. The same is Right with the formula; a difference is a Likely problem (a new kind
    of check, never red); a job value nobody entered makes it Not compared, saying which. True
    when the formula spoke."""
    finding = item.finding
    if not formulas.measures_top_against_run(vendor, architect):
        return False
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
    for side, ends in (("vendor", vendor), ("architect", architect)):
        drawn = Fraction(ends.span.inches)
        if not _same(
            Decimal(drawn.numerator) / drawn.denominator, _decimal((inches or {}).get(side))
        ):
            return False  # the labels code read are not the two values compared
    for page, ends, whose in (
        (vendor_page, vendor, "the vendor's"),
        (architect_page, architect, "the reference's"),
    ):
        fit = page.fit
        check = fit.check(ends.span.id) if fit is not None else None
        if check is None or not check.agrees:
            return False  # a value not checked against its drawn length never feeds a formula
    per_end = getattr(vendor_page.field_cuts, "per_end", None)
    cut = Fraction(per_end) if per_end else formulas.setting(ctx.rules_md, "field_cut")
    if cut is None:
        cut = import_module("app.review.assistant.field_cut").DEFAULT_PER_END
    found = formulas.top_length(
        vendor,
        architect,
        field_cut=cut,
        end_overhang=formulas.setting(ctx.rules_md, formulas.END_OVERHANG),
    )
    if found is None:
        return False
    vendor_words = format_inches(vendor.span.inches)
    labels = {"vendor": vendor.span.id, "architect": architect.span.id}
    if found.outcome == "missing":
        why = f"the vendor's top is {vendor_words}; GV's formula needs {found.words}"
        finding[formulas.FIELD] = formulas.record(formulas.LENGTH, "missing", why, labels=labels)
        finding[job_value_rules.FIELD] = formulas.missing_record(formulas.LENGTH, found.missing)
        if finding.get("status") == "different":
            finding["status"] = "check"  # one measures the top, the other the cabinets: never red
        item.changed = True
        item.note("ends", "no_change", f"GV's formula: {why}")
        return True
    if found.outcome == "same":
        how = f"the vendor's top {vendor_words} = {found.words}"
        finding[formulas.FIELD] = formulas.record(
            formulas.LENGTH, "same", how, expected=found.expected, labels=labels
        )
        finding.pop(likely_problems.FIELD, None)
        _settle_right(item, "ends", formulas.right_how(formulas.LENGTH, how))
        return True
    expected = found.expected if found.expected is not None else architect.span.inches
    off = abs(vendor.span.inches - expected)
    how = (
        f"the vendor's top is {vendor_words}, {format_inches(off)} "
        f"{'more' if vendor.span.inches > expected else 'less'} than {found.words}"
    )
    finding[formulas.FIELD] = formulas.record(
        formulas.LENGTH, "different", how, expected=expected, labels=labels
    )
    finding[likely_problems.FIELD] = {
        "by": formulas.LENGTH.kind,
        "kind": "size",
        "proof": "likely_only",
        "sure": "likely",
        "why": how,
        "words": formulas.please_check(formulas.LENGTH, how),
    }
    finding.update(status="check")
    finding.pop("settled", None)
    item.changed = True
    item.note("ends", "read", f"GV's formula: {how}")
    return True


def _same_field_cuts(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation
) -> None:
    """An item called the same whose two numbers differ is never Right on the AI's word
    (`classification.differing_values`, 2026-10-27): its difference may be GV's field cut, so the
    field-cut rule vets it as the difference it is (`_field_cut`, settled with GV). Allowed: Right
    with code's record; else it stays Not compared."""
    for finding in live:
        if (
            str(finding.get("id")) in result.absorbed
            or finding.get("status") != "same"
            or finding.get("field_cut") is not None
            or classification.differing_values(finding) is None
        ):
            continue
        item = _Item(finding)
        try:
            _field_cut(item, "labels", ctx, status="different")
        except Exception:  # noqa: BLE001 - not vetted: the item stays Not compared
            _log.exception("escalation: field cut not vetted on %s", finding.get("id"))
            continue
        if item.changed:
            finding.update(judged(finding))
            result.changed.append(finding)


def _mark_row_places(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation
) -> None:
    """A Right on a piece of a row of sizes needs both pieces to start at the same place along their
    rows (2026-10-27, `row_places`): the AI paired the vendor's range gap with the reference's
    other piece of that size, further up the same counter. Code records where each starts; a pick that starts
    elsewhere is pointed at the reference piece that starts where the vendor's does, or, with none,
    the item is Not compared (`classification`). Rows code cannot measure: the item as before."""
    if ctx.pages is None:
        return
    for finding in live:
        if str(finding.get("id")) in result.absorbed or finding.get("status") != "same":
            continue
        if finding.get("source") not in ("architect", "brand_prototype", "gv_review"):
            continue
        if finding.get("formula") or finding.get("field_cut") or finding.get("paired_by") == "code":
            continue  # GV's formula, the field cut or code's own pair compared it
        if isinstance(finding.get("decision"), Mapping) and finding["decision"].get("decision"):
            continue  # the reviewer decided it on the pair it shows
        held = finding.get(row_places.FIELD)
        if (
            isinstance(held, Mapping)
            and held.get("outcome") == "repointed"
            and held.get("architect") == row_places.side_key(finding, "architect")
        ):
            continue  # code's own pick, recorded when it was made
        try:
            record = row_places.judge(finding, ctx.pages)
        except Exception:  # noqa: BLE001 - not measured: the item as it is
            _log.exception("escalation: row places failed on %s", finding.get("id"))
            continue
        if record is None or held == record:
            continue
        if record["outcome"] == "repointed":
            reference = (finding.get("dims") or {}).get("architect") or {}
            key = (str(reference.get("file")), int(reference.get("page") or 0))
            pick = str(record["architect"]).rsplit("|", 1)[-1]
            label = next((x for x in ctx.labels.get(key, ()) if str(x.get("id")) == pick), None)
            if label is None:
                continue  # code's pick is not among the page's labels: the item as it is
            row_places.repoint(finding, label)
        finding[row_places.FIELD] = record
        finding.update(judged(finding))
        result.changed.append(finding)


def _mark_right_ends(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation
) -> None:
    """A Right needs like-for-like proof too (final plan #4, 2026-10-25): each Right that pairs a
    vendor label with a reference label carries `dim_ends.LIKE_FOR_LIKE`, whether code proved both
    dimensions run between the same kinds of points (`dim_ends.right_ends`). One it could not prove
    is a "Matches (ends not proved)" (`proof.matches_unproved`), for the cards to fold under a
    proved sibling. Data only: the outcome is unchanged."""
    if ctx.pages is None:
        return
    for finding in live:
        if str(finding.get("id")) in result.absorbed or finding.get("status") != "same":
            continue
        try:
            record = dim_ends.right_ends(finding, ctx.pages)
        except Exception:  # noqa: BLE001 - not read: no record, the Right as before
            _log.exception("escalation: like-for-like record failed on %s", finding.get("id"))
            continue
        if record is not None and finding.get(dim_ends.LIKE_FOR_LIKE) != record:
            finding[dim_ends.LIKE_FOR_LIKE] = record
            result.changed.append(finding)


def _mark_right_proof(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation
) -> None:
    """A Right needs code's proof that both dimensions measure the same thing (2026-10-27,
    `right_proof`): the AI paired the vendor's 4" band at the counter's edge with the reference's 4"
    from the floor to an outlet, a tick true only by luck. Code records, for the two labels each
    Right pairs, whether its geometry proves the same ends, shows other ends, or the size is the
    only such dimension on both drawings; without proof the item is Not compared
    (`classification`). Items code compared itself, and those a reviewer decided, are left."""
    if not ctx.labels:
        return
    labels = ends_proof.labels_reader(ctx.labels)
    cache: dict[Any, Any] = {}
    for finding in live:
        if str(finding.get("id")) in result.absorbed or finding.get("status") != "same":
            continue
        try:
            record = right_proof.judge(finding, labels, ctx.pages, ctx.page_map, cache)
        except Exception:  # noqa: BLE001 - not judged: no record, the item as it was
            _log.exception("escalation: right proof failed on %s", finding.get("id"))
            continue
        held = finding.get(right_proof.FIELD)
        if record is None:
            if held is not None:
                finding.pop(right_proof.FIELD, None)
                result.changed.append(finding)
            continue
        if held != record:
            finding[right_proof.FIELD] = record
            result.changed.append(finding)


def _mark_drafting_notes(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation
) -> None:
    """The drafting note next to each item's reference size (final plan #9, `drafting_notes`):
    HOLD (a hard requirement), V.I.F. / +/- / field verify (never red, never a Likely problem: a
    field-verify note), or a difference of 1/2" or less to a wall's face (the drafting
    convention). Only what code reads next to the label; the outcome is the proof's."""

    def labels(file: str, page: int) -> Sequence[Mapping[str, Any]]:
        return ctx.labels.get((file, page), ())

    def phrases(file: str, page: int) -> Sequence[Mapping[str, Any]]:
        return ctx.phrases.get((file, page), ())

    def ends(file: str, page: int, label_id: str) -> dim_ends.Ends | None:
        if ctx.pages is None:
            return None
        found = ctx.pages(file, page)
        return found.ends(label_id) if found is not None else None

    for finding in live:
        if str(finding.get("id")) in result.absorbed:
            continue
        try:
            record = drafting_notes.mark(finding, labels, phrases, ends)
        except Exception:  # noqa: BLE001 - no note read: the item as it was
            _log.exception("escalation: drafting note not read on %s", finding.get("id"))
            continue
        if finding.get(drafting_notes.FIELD) == record:
            continue
        if record is None:
            finding.pop(drafting_notes.FIELD, None)
        else:
            finding[drafting_notes.FIELD] = record
        result.changed.append(finding)


def _before_ladder(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation
) -> set[str]:
    """Every item, whatever its outcome, before the ladder (#1859): GV's stickers are never the
    vendor's, and tags are compared by code. Returns the ids it decided (the ladder leaves them)."""
    early: set[str] = set()
    for finding in live:
        item = _Item(finding)
        try:
            dropped = _ends_step(item, ctx, live, result)
        except Exception:  # noqa: BLE001 - the item stays as it was; the other steps go on
            _log.exception("escalation: like-for-like step failed on %s", finding.get("id"))
            dropped = None
        if dropped is not None:
            result.absorbed[str(finding["id"])] = dropped
            result.settled["ends"] = result.settled.get("ends", 0) + 1
            result.tried += 1
            early.add(str(finding["id"]))
            continue
        if item.changed and finding.get("created_by") == sink_centre.CREATED_BY:
            # The sink-centre check compared it (or said why not): nothing else to try on it.
            item.finding.update(judged(item.finding))
            group = proof.proof_group(item.finding)
            item.outcome = "right" if group == "right" else None
            result.settled["ends"] = result.settled.get("ends", 0) + 1
            item.finding["escalation"] = {
                "outcome": item.outcome or "undecided",
                "steps": item.steps,
            }
            result.log[str(item.finding["id"])] = item.steps
            result.changed.append(item.finding)
            result.tried += 1
            early.add(str(item.finding["id"]))
            continue
        _sticker_step(item, ctx)
        if ctx.stop is None or not ctx.stop():
            # Stop: GV's stickers are still told apart (that only ever takes a red away); the tag
            # reader, which can decide an item, waits for a Continue (2026-10-23).
            _tag_step(item, ctx)
        if not item.changed:
            continue
        item.finding.update(judged(item.finding))
        group = proof.proof_group(item.finding)
        item.outcome = (
            "wrong" if group == "clouded" else "right" if group == "right" else item.outcome
        )
        if item.outcome is not None:
            result.settled["tags"] = result.settled.get("tags", 0) + 1
        item.finding["escalation"] = {"outcome": item.outcome or "undecided", "steps": item.steps}
        result.log[str(item.finding["id"])] = item.steps
        result.changed.append(item.finding)
        result.tried += 1
        early.add(str(item.finding["id"]))
    if ctx.stop is None or not ctx.stop():
        _pasted_step(live, ctx, result, early)
    _claims_step(live, ctx, result, early)
    return early


def _claims_step(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation, early: set[str]
) -> None:
    """GV's settled rules and code's facts on every open claim (`claim_rules`, 2026-10-25): a
    notch, a unit count one sheet cannot settle, a wall outline taken for the top, a side splash
    taken for a mitred piece, which end on a unit drawn both ways round. Only ever withdraws an
    item or makes it Not compared (a unit count that differs per type is kept, its reason naming
    the type); never red."""
    rules = import_module("app.review.assistant.claim_rules")
    open_items = [f for f in live if str(f["id"]) not in result.absorbed]
    try:
        found = rules.changes(
            open_items,
            page_texts=ctx.page_texts,
            main_file=ctx.main_file,
            outlines=ctx.outlines,
            labels=ctx.labels,
        )
    except Exception:  # noqa: BLE001 - the items stay as they were
        _log.warning("escalation: the claim rules step failed", exc_info=True)
        return
    by_id = {str(f["id"]): f for f in open_items}
    for finding_id, change in found:
        finding = by_id[finding_id]
        item = _Item(finding)
        finding[rules.FIELD] = change.record()
        item.changed = True
        if change.outcome == "withdrawn":
            result.absorbed[finding_id] = change.why
            item.outcome = "folded"
        elif change.outcome == "kept":
            # The claim stands; its card's reason says what code found (a unit type missing).
            mark = finding.get(likely_problems.FIELD)
            if isinstance(mark, Mapping):
                before_why = " ".join(str(mark.get("why") or "").split())
                code_why = f"{change.why[0].upper()}{change.why[1:]}."
                finding[likely_problems.FIELD] = {
                    **mark,
                    "why": f"{code_why} {before_why}".strip(),
                }
        else:
            finding.update(judged(finding))
        item.note("claim_rules", change.outcome, change.why)
        result.settled["claim_rules"] = result.settled.get("claim_rules", 0) + 1
        before = finding.get("escalation")
        steps = list(before.get("steps") or []) if isinstance(before, Mapping) else []
        finding["escalation"] = {
            "outcome": item.outcome or "undecided",
            "steps": steps + item.steps,
        }
        result.log[finding_id] = steps + item.steps
        result.changed.append(finding)
        if finding_id not in early:
            result.tried += 1
        early.add(finding_id)


def _pasted_step(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation, early: set[str]
) -> None:
    """Every open item the reference's pasted views settle (`pasted_compare`, 2026-10-25): the
    sink model they name (Right, or a Likely problem), the splash they draw (the look's "no
    splash" withdrawn) and the countertop depth they print, like for like. Never red."""
    if ctx.pasted is None:
        return
    compare = import_module("app.review.assistant.pasted_compare")
    open_items = [f for f in live if str(f["id"]) not in result.absorbed]
    try:
        found = compare.changes(open_items, ctx.pasted, ctx.phrases, ctx.page_map, ctx.main_file)
    except Exception:  # noqa: BLE001 - the items stay as they were
        _log.warning("escalation: the pasted views step failed", exc_info=True)
        return
    by_id = {str(f["id"]): f for f in open_items}
    for finding_id, change in found:
        finding = by_id[finding_id]
        item = _Item(finding)
        finding[compare.FIELD] = {**change.record, "why": change.why}
        item.changed = True
        if change.outcome in ("withdrawn", "folded"):
            result.absorbed[finding_id] = change.why
            item.outcome = "folded"
        else:
            for key in change.drop:
                finding.pop(key, None)
            finding.update(change.updates)
            if change.outcome == "right":
                _settle_right(item, "pasted_views", change.why)
            finding.update(judged(finding))
            group = proof.proof_group(finding)
            item.outcome = "right" if group == "right" else item.outcome
        item.note("pasted_views", change.outcome, change.why)
        if change.outcome != "noted":
            result.settled["pasted_views"] = result.settled.get("pasted_views", 0) + 1
        before = finding.get("escalation")
        steps = list(before.get("steps") or []) if isinstance(before, Mapping) else []
        finding["escalation"] = {
            "outcome": item.outcome or "undecided",
            "steps": steps + item.steps,
        }
        result.log[finding_id] = steps + item.steps
        result.changed.append(finding)
        if finding_id not in early:
            result.tried += 1
        early.add(finding_id)


def _drawing_role(ctx: EscalationContext, label: Mapping[str, Any]) -> tuple[str | None, list[Any]]:
    """The role of the page-map drawing that holds `label` (the smallest one), and its box."""
    file = str(label.get("file") or ctx.main_file or "")
    holding: list[tuple[float, str, list[Any]]] = []
    for entry in (ctx.page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or entry.get("page") != label.get("page"):
            continue
        if entry.get("file") not in (None, file):
            continue
        for half in entry.get("halves") or []:
            box = half.get("box") if isinstance(half, Mapping) else None
            if not (isinstance(box, Sequence) and len(box) == 4):
                continue
            if _inside(_centre(label.get("box") or (0, 0, 0, 0)), box, 0.0):
                size = (float(box[2]) - float(box[0])) * (float(box[3]) - float(box[1]))
                holding.append((size, str(half.get("role") or "unknown"), list(box)))
    if not holding:
        return None, []
    _size, role, box = min(holding, key=lambda h: h[0])
    return role, box


def _judge_missing(
    finding: Mapping[str, Any],
    label: Mapping[str, Any] | None,
    ctx: EscalationContext,
    page: int,
    live: Sequence[Mapping[str, Any]],
) -> missing_items.Judged:
    """`missing_items.judge` on an item code found printed nowhere on the vendor's drawings."""
    file = ctx.main_file or ""
    role, holder = _drawing_role(ctx, label) if label is not None else (None, [])

    def inside(raw: Mapping[str, Any]) -> bool:
        return bool(holder) and _inside(_centre(raw.get("box") or (0, 0, 0, 0)), holder, 0.0)

    where = (str((label or {}).get("file") or file), int((label or {}).get("page") or page))
    architect_labels = [
        dict(x, page=where[1], file=where[0])
        for x in ctx.labels.get(where, [])
        if x.get("side") == "architect" and inside(x)
    ]
    architect_phrases = [p for p in ctx.phrases.get(where, []) if inside(p)]
    notes = [
        a
        for a in ctx.annotations
        if a.get("page") == where[1]
        and a.get("kind") == "FreeText"
        and a.get("file") in (None, where[0])
    ]
    entries = [
        e
        for e in (ctx.page_map or {}).get("pages") or []
        if isinstance(e, Mapping) and e.get("file") in (None, file)
    ]
    drawings: list[missing_items.VendorDrawing] = []
    for number, box, labels in _vendor_drawings(ctx, page) or _vendor_drawings(
        ctx, int(finding.get("page") or 0)
    ):
        key = (file, number)
        halves: list[Mapping[str, Any]] = [
            h
            for e in entries
            if e.get("page") == number
            for h in e.get("halves") or []
            if isinstance(h, Mapping)
        ]
        roles = {str(h.get("role")) for h in halves}
        half = next((h for h in halves if h.get("role") == "vendor" and h.get("box") == box), {})
        number_round = half.get("round")
        x0, y0, x1, y1 = (float(v) for v in box)
        drawings.append(
            missing_items.VendorDrawing(
                page=number,
                round=number_round if isinstance(number_round, int) else None,
                box=(x0, y0, x1, y1),
                labels=labels,
                phrases=ctx.phrases.get(key, []),
                text=ctx.page_texts.get(key, "") if roles == {"vendor"} else "",
                picture_share=ctx.pictures.get(key),
                opened=ctx.unopened is None or number not in ctx.unopened,
            )
        )
    return missing_items.judge(
        finding,
        label=label,
        label_role=role,
        architect_labels=architect_labels,
        architect_phrases=architect_phrases,
        notes=notes,
        drawings=drawings,
        others=[f for f in live if f.get("id") != finding.get("id")],
        colour=_colour_reader(ctx),
    )


def _colour_reader(ctx: EscalationContext) -> missing_items.ColourReader | None:
    """Whether an architect label is set in GV's colour, read from the drawing's own text at its
    box (`under_markup`'s words carry their colour, on GV's combined sheets only: on an ordinary
    upload every colour is the drawing's own ink); None when it cannot be looked at."""
    under = ctx.under
    if under is None:
        return None

    def coloured(label: Mapping[str, Any]) -> bool | None:
        try:
            x0, y0, x1, y1 = (float(v) for v in label.get("box") or ())
            words = under(
                str(label.get("file") or ctx.main_file or ""),
                int(label.get("page") or 0),
                (x0 - 1, y0 - 1, x1 + 1, y1 + 1),
            )
        except Exception:  # noqa: BLE001 - a look that fails proves nothing
            return None
        printed = [w for w in words if re.search(r"\d", str(w.get("text") or ""))]
        if not printed:
            return None
        return any(bool(w.get("coloured")) for w in printed)

    return coloured


def _missing_red(
    finding: Mapping[str, Any], verdict: missing_items.Judged, search: Mapping[str, Any]
) -> dict[str, Any]:
    """The item as a red: a missing millwork item code proved (`missing_items.judge`)."""
    first = verdict.values[0]
    inches = _decimal(first.get("inches"))
    checks = dict(finding.get("checks") or {})
    checks["text"] = "agrees"
    red = dict(finding)
    red.update(
        status="different",
        architect=str(first.get("text") or ""),
        vendor="not shown on the vendor's drawings",
        why=f"{verdict.why}.",
        paired_by="code",
        checks=checks,
        inches={"architect": None if inches is None else str(inches), "vendor": None},
        keep_reason=None,
        check_reason=None,
        text_proof={
            "kind": missing_items.KIND,
            "item": verdict.kind,
            "code": str(first.get("text") or ""),
            "architect": [
                {
                    "page": v.get("page"),
                    "id": v.get("id"),
                    "text": str(v.get("text") or ""),
                    "orientation": v.get("orientation"),
                }
                for v in verdict.values
            ],
            "vendor": [{"page": page, "round": number} for page, number in verdict.rounds],
            "searched": verdict.searched,
            "pairing": "the page map's area pairs the drawings",
        },
    )
    red[VENDOR_SEARCH] = dict(search)
    if first.get("page") != finding.get("page"):
        red["vs_page"] = first.get("page")
    return judged(red)


def _missing_reds(live: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{architect label ref: the proved missing millwork item that rests on it}."""
    reds: dict[str, dict[str, Any]] = {}
    for finding in live:
        proof_ = finding.get("text_proof")
        if (
            isinstance(proof_, Mapping)
            and proof_.get("kind") == missing_items.KIND
            and proof.proof_group(finding) == "clouded"
        ):
            for value in proof_.get("architect") or []:
                reds.setdefault(f"p{value.get('page')}.{value.get('id')}", finding)
    return reds


def _missing_carrier(
    finding: Mapping[str, Any], live: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """The proved missing millwork item that rests on `finding`'s architect label, if any."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    architect = (dims or {}).get("architect")
    if not isinstance(architect, Mapping):
        return None
    carrier = _missing_reds(live).get(f"p{architect.get('page')}.{architect.get('id')}")
    return None if carrier is None or carrier is finding else carrier


def _fold_missing_copies(live: Sequence[dict[str, Any]], result: Escalation) -> None:
    """An item the AI says the vendor does not show that rests on the same architect label as a
    proved missing millwork item is that item: it folds into the red."""
    if not _missing_reds(live):
        return
    for finding in live:
        if str(finding["id"]) in result.absorbed or finding.get("text_proof"):
            continue
        if not undecided(finding) or not classification.missing_on_vendor(finding):
            continue
        carrier = _missing_carrier(finding, live)
        if carrier is None:
            continue
        item = _Item(finding, steps=list((finding.get("escalation") or {}).get("steps") or []))
        _fold(item, carrier, "vendor_search", result)
        finding["escalation"] = {"outcome": "folded", "steps": item.steps}
        result.settled["vendor_search"] = result.settled.get("vendor_search", 0) + 1
        if not any(f is finding for f in result.changed):
            result.changed.append(finding)


def escalate(findings: Sequence[Mapping[str, Any]], ctx: EscalationContext) -> Escalation:
    """The free code steps on every undecided item of `findings` (an answer's live findings),
    the biggest first (see the module). Pure: returns what changed."""
    result = Escalation()
    live = [dict(f) for f in findings]
    # First (Anant, 2026-10-22): an item compared only with the vendor's own earlier round is
    # never Right; code compares it with the architect instead where it can (`round_reference`).
    linked = compare_with_architect(
        live, labels=ctx.labels, page_map=ctx.page_map, pairs=ctx.pairs, main_file=ctx.main_file
    )
    result.round_matches = linked.marked
    result.linked = dict(linked.linked)
    result.absorbed.update(linked.absorbed)
    relinked = {str(f["id"]): judged(f) for f in linked.changed}
    live = [relinked.get(str(f["id"]), f) for f in live if str(f["id"]) not in linked.absorbed]
    result.changed.extend(relinked.values())
    # How each Wrong's numbers were read, before any step groups them (final plan #2): kept with
    # the items the ladder tries and recorded on every item at the end (a Stop leaves the rest).
    _mark_reading(live, ctx, None)
    early = _before_ladder(live, ctx, result)
    by_id = {str(f["id"]): f for f in live}
    areas = _areas(ctx)
    order = by_impact(live)
    waiting = sum(1 for f in order if str(f["id"]) not in early and undecided(f))
    tried = ctx.count(waiting) if ctx.count is not None else (lambda: None)
    for finding in order:
        if str(finding["id"]) in early or not undecided(finding):
            continue
        if ctx.stop is not None and ctx.stop():
            break  # Stop: the items not tried stay as they are (Continue tries them)
        tried()
        result.tried += 1
        item = _Item(by_id[str(finding["id"])])
        for step in FREE_STEPS:
            before = proof.proof_group(judged(item.finding) if item.changed else item.finding)
            if step == "labels":
                _labels_step(item, ctx)
            elif step == "under_markup":
                _under_markup_step(item, ctx)
            elif step == "same_item":
                others = [f for f in live if str(f["id"]) not in result.absorbed]
                carrier = _carrier(item.finding, others, areas)
                if carrier is not None:
                    _fold(item, carrier, step, result)
            elif step == "vendor_sums":
                carrier = _vendor_sum_carrier(item.finding, live)
                if carrier is not None:
                    _fold(item, carrier, step, result)
            elif step == "vendor_search":
                _vendor_search_step(item, ctx, live)
            elif step == "chain_sums":
                _chain_step(item, ctx)
            else:
                _pairs_step(item, ctx)
            if item.changed:
                item.finding.update(judged(item.finding))
                _mark_reading([item.finding], ctx, None, stops=False)
            after = proof.proof_group(item.finding)
            if item.outcome is None and before != after and after in ("clouded", "right"):
                item.outcome = "wrong" if after == "clouded" else "right"
            if item.outcome is not None:
                result.settled[step] = result.settled.get(step, 0) + 1
                break
        item.finding["escalation"] = {
            "outcome": item.outcome or "undecided",
            "steps": item.steps,
        }
        result.log[str(item.finding["id"])] = item.steps
        result.changed.append(item.finding)
    _fold_missing_copies(live, result)
    _mark_job_rules(live, ctx, result)
    _same_field_cuts(live, ctx, result)
    _mark_row_places(live, ctx, result)
    _mark_right_ends(live, ctx, result)
    _mark_right_proof(live, ctx, result)
    _mark_drafting_notes(live, ctx, result)
    _mark_reading(live, ctx, result)
    # One copy of each changed finding, its last state.
    last = {str(f["id"]): f for f in result.changed}
    result.changed = list(last.values())
    return result


def _mark_reading(
    live: Sequence[dict[str, Any]],
    ctx: EscalationContext,
    result: Escalation | None,
    *,
    stops: bool = True,
) -> None:
    """Each Wrong's record of how the numbers it rests on were read (`reading_routes.mark`, final
    plan #2): each label's reading route, its millimetres, and its drawn length at its view's fit
    (`dim_ends.Page.fit`), both OCR engines on a typed number's spot (`ctx.typed`) and, for a
    number still short of two routes, the number reader (`ctx.reread`: its stroke shapes and its
    ruler). A red needs two different routes that agree on every number. Data only here: `proof`
    groups by it."""

    def labels_of(file: str, page: int) -> Sequence[Mapping[str, Any]]:
        return ctx.labels.get((file, page)) or ()

    def fit_of(file: str, page: int) -> Any:
        found = ctx.pages(file, page) if ctx.pages is not None else None
        return found.fit if found is not None else None

    numbers = None
    if ctx.reread is not None:
        label_routes = import_module("app.review.assistant.read_number").label_routes

        def numbers(label: Mapping[str, Any]) -> Any:
            return label_routes(ctx.reread, label)

    for finding in live:
        if stops and ctx.stop is not None and ctx.stop():
            return  # Stop: the items stay as they are (Continue records them)
        if result is not None and str(finding.get("id")) in result.absorbed:
            continue
        if isinstance(finding.get("decision"), Mapping) and finding["decision"].get("decision"):
            continue  # the reviewer decided it: the reviewer is its witness
        try:
            changed = reading_routes.mark(finding, labels_of, fit_of, ctx.typed, numbers)
        except Exception:  # noqa: BLE001 - no record: the item is judged on its labels alone
            _log.exception("escalation: reading record failed on %s", finding.get("id"))
            continue
        if changed and result is not None:
            result.changed.append(finding)


def _mark_job_rules(
    live: Sequence[dict[str, Any]], ctx: EscalationContext, result: Escalation
) -> None:
    """Each item still open that GV's own rule sets (the countertop's depth, the sink cut-out's
    size) and whose job values nobody entered gets the rule and the values it is missing
    (`job_value_rules.mark`), so its Not-compared reason says what would settle it. Never an
    outcome: the item stays Not compared."""
    if ctx.rules_md is None:
        return
    for finding in live:
        if str(finding.get("id")) in result.absorbed or not undecided(finding):
            continue
        held = finding.get(formulas.FIELD)
        if isinstance(held, Mapping) and held.get("outcome") == "missing":
            continue  # a formula named the job value it waits for (`_length_formula`)
        record = job_value_rules.mark(finding, ctx.rules_md)
        if finding.get(job_value_rules.FIELD) == record:
            continue
        if record is None:
            finding.pop(job_value_rules.FIELD, None)  # the job values are entered now
        else:
            finding[job_value_rules.FIELD] = record
        result.changed.append(finding)


def _areas(ctx: EscalationContext) -> dict[int, str]:
    resolver = import_module("app.review.assistant.decision_resolver")
    return dict(resolver.page_areas(ctx.page_map, ctx.main_file))


# ---- loading the facts --------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def context_from_pack(
    pack: Path | None,
    main_file: str | None,
    *,
    page_map: Mapping[str, Any] | None = None,
    under: UnderReader | None = None,
    reread: NumberReader | None = None,
    stickers: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] | None = None,
    strokes: Callable[[str, int, tuple[float, float, float, float]], Any] | None = None,
    changes: Sequence[Mapping[str, Any]] = (),
    unopened: frozenset[int] | None = None,
    stop: Callable[[], bool] | None = None,
    count: Callable[[int], Callable[[], None]] | None = None,
    field_cuts: Callable[[str, int], Any] | None = None,
    rules_md: str | None = None,
    segments: Callable[[str, int], Any] | None = None,
    pasted: Callable[[str, int], Sequence[Any]] | None = None,
    outlines: Callable[[str, int, tuple[float, float, float, float]], str | None] | None = None,
) -> EscalationContext:
    """The free steps' facts from a set pack (`dims.json`, `pairs.json`, the stickers code hid
    (`<name>_stickers.json`), and for a missing item `page_texts.json`, `health.json`,
    `annotations.json`). `stickers`: GV's stickers when the pack lists none (an older pack);
    `unopened`: the pages the check never opened; `pasted`: the reference's pasted views
    (`pasted_views.PastedReader`), else that step does nothing; `segments`: each page's line work
    (`vendor_consistency.line_work`; default: the pack's kept line work, when it keeps any)."""
    labels: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    phrases: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    texts: dict[tuple[str, int], str] = {}
    pictures: dict[tuple[str, int], float] = {}
    notes: list[Mapping[str, Any]] = []
    pairs: dict[str, str] = {}
    if pack is not None:
        dims = _read(pack / "dims.json")
        for entry in (dims or {}).get("pages") or [] if isinstance(dims, Mapping) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                key = (str(entry.get("file")), int(entry["page"]))
                labels[key] = [
                    label for label in entry.get("labels") or [] if isinstance(label, Mapping)
                ]
                phrases[key] = [p for p in entry.get("phrases") or [] if isinstance(p, Mapping)]
        for name, into in (("page_texts.json", texts), ("health.json", pictures)):
            found = _read(pack / name)
            for entry in (found or {}).get("pages") or [] if isinstance(found, Mapping) else []:
                if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
                    continue
                key = (str(entry.get("file")), int(entry["page"]))
                if into is texts:
                    texts[key] = str(entry.get("text") or "")
                else:
                    share = _decimal((entry.get("facts") or {}).get("picture_share"))
                    if share is not None:
                        pictures[key] = float(share)
        marks = _read(pack / "annotations.json")
        notes = [a for a in marks if isinstance(a, Mapping)] if isinstance(marks, list) else []
        pairs = code_pairs(_read(pack / "pairs.json"))
        if page_map is None:
            page_map = _read(pack / "page_map.json")
        listed = import_module("app.review.assistant.gv_stickers").read_folder(pack)
        if listed is not None:
            stickers = listed
    return EscalationContext(
        main_file=main_file,
        labels=labels,
        page_map=page_map if isinstance(page_map, Mapping) else None,
        pairs=pairs,
        under=under,
        reread=reread,
        phrases=phrases,
        stickers=stickers or {},
        strokes=strokes,
        changes=changes,
        page_texts=texts,
        pictures=pictures,
        annotations=notes,
        unopened=unopened,
        stop=stop,
        count=count,
        field_cuts=field_cuts,
        rules_md=rules_md,
        pages=dim_ends.pages_reader(
            labels,
            phrases,
            segments if segments is not None else dim_ends.kept_line_work(pack),
            field_cuts,
            main_file,
        ),
        pasted=pasted,
        outlines=outlines if outlines is not None else _outlines_of(pasted),
        typed=import_module("app.review.assistant.typed_ocr").reader_for_pack(pack),
    )


def _outlines_of(
    pasted: object,
) -> Callable[[str, int, tuple[float, float, float, float]], str | None] | None:
    """The outline reader on the same PDFs the pasted views read (`claim_rules.OutlineReader`)."""
    pdf_of = getattr(pasted, "pdf_of", None)
    if not callable(pdf_of):
        return None
    reader: Callable[[str, int, tuple[float, float, float, float]], str | None] = import_module(
        "app.review.assistant.claim_rules"
    ).OutlineReader(pdf_of)
    return reader


def _repicture(book: Any, finding: dict[str, Any]) -> None:
    """An item now compared at another architect spot shows that spot's picture, never the old
    one's (no picture is never a failed step)."""
    picture = getattr(book, "_picture", None)
    if not callable(picture) or not isinstance(finding.get("arch_box"), Mapping):
        return
    try:
        made = picture(finding["arch_box"])
    except (OSError, ValueError, TypeError):
        made = (None, None)
    finding["arch_picture_id"] = made[0] if isinstance(made, tuple) and made else None


def stickers_of_uploads(
    pdf_paths: Mapping[str, Path],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """GV's stickers searched in the uploads themselves (a set pack made before they were listed)."""
    module = import_module("app.review.assistant.gv_stickers")
    out: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for name, path in pdf_paths.items():
        if path.is_file():
            out.update(module.by_page(module.stickers_of(path), name))
    return out


def finish_changes(pack: Path | None) -> list[dict[str, Any]]:
    """Code's finish check's proved "changed" findings on the set pack (`architect_items`): the
    tag reader's proof that a different tag is the same item changed."""
    try:
        result = import_module("app.review.assistant.architect_items").check_pack(pack)
    except Exception:  # noqa: BLE001 - without it no tag item is Wrong, never a crash
        _log.exception("escalation: the finish check for the tag reader failed")
        return []
    if result is None:
        return []
    return [
        f
        for f in result.findings
        if isinstance(f.get("text_proof"), Mapping) and f["text_proof"].get("kind") == "changed"
    ]


def _under_reader(pdf_paths: Mapping[str, Path]) -> UnderReader:
    def read(file: str, page: int, box: tuple[float, float, float, float]) -> list[dict[str, Any]]:
        path = pdf_paths.get(file)
        if path is None or not path.is_file():
            return []
        module = import_module("app.review.assistant.under_markup")
        found = module.read_under_markup(path, page, box)
        return [dict(word) for word in getattr(found, "words", []) or []]

    return read


def _stop_of(engine: Any) -> Callable[[], bool] | None:
    cancel = getattr(engine, "cancel", None)
    return cancel.is_set if cancel is not None else None


def _rules_md(workspace: Any) -> str | None:
    """The chat's `rules.md` (the job values in force), or `None` when it cannot be read."""
    try:
        return str((workspace.skills / "rules.md").read_text(encoding="utf-8"))
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _line_work(workspace: Any) -> Callable[[str, int], Any] | None:
    """The uploads' line work, kept in the set pack (`vendor_consistency.line_work`)."""
    uploads = getattr(workspace, "uploads", None)
    if uploads is None or workspace.set_pack is None:
        return None
    consistency = import_module("app.review.assistant.vendor_consistency")
    return consistency.line_work(Path(uploads), Path(workspace.set_pack))  # type: ignore[no-any-return]


def escalate_answer(engine: Any) -> Escalation | None:
    """The engine's hook (whole-set work, after the resolver, before the trust report): the free
    steps on the answer's undecided items, each change shown, folded copies dropped with their
    reason. `None` when the answer has no set pack."""
    workspace = engine.tools.workspace
    book = engine.book
    if workspace.set_pack is None or not book.live():
        return None
    plan = import_module("app.review.assistant.full_check_plan")
    page_map = plan.read_page_map(workspace.set_pack, assume=True, main_file=book.main_file)
    numbers = import_module("app.review.assistant.read_number")
    tags = import_module("app.review.assistant.tag_reader")
    gv_stickers = import_module("app.review.assistant.gv_stickers")
    listed = gv_stickers.read_folder(workspace.set_pack)
    ctx = context_from_pack(
        workspace.set_pack,
        book.main_file,
        page_map=page_map,
        under=_under_reader(book.pdf_paths()),
        reread=numbers.reader_for_pack(workspace.set_pack, workspace.uploads),
        stickers=listed if listed is not None else stickers_of_uploads(book.pdf_paths()),
        strokes=tags.stroke_reader_for_pack(workspace.set_pack, workspace.uploads),
        changes=finish_changes(workspace.set_pack),
        unopened=frozenset(
            int(entry["page"])
            for entry in book.coverage_map()["pages"]
            if entry.get("status") == "not_opened"
        ),
        stop=_stop_of(engine),
        count=getattr(engine, "count_items", None),
        field_cuts=getattr(book, "field_cuts", None),
        rules_md=_rules_md(workspace),
        segments=_line_work(workspace),
        pasted=import_module("app.review.assistant.pasted_views").PastedReader.for_pack(
            workspace.set_pack, workspace.uploads, page_map=page_map
        ),
    )
    with engine.finding_lock:
        before = {str(f["id"]): f.get("arch_box") for f in book.live()}
        result = escalate(book.live(), ctx)
        for finding_id, reason in result.absorbed.items():
            if book.absorb(finding_id):
                engine._show_dropped(finding_id, reason)
        for finding in result.changed:
            if finding["id"] in result.absorbed:
                continue
            if finding.get("arch_box") != before.get(str(finding["id"])):
                _repicture(book, finding)
            if book.replace(finding):
                engine._show_finding(finding)
    items_counted = getattr(engine, "items_counted", None)
    if items_counted is not None:
        items_counted()
    _log.info("assistant escalation: %s", result.block())
    return result
