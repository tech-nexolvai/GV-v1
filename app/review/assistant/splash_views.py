"""Splash sizes on the reference's other views: code, $0, no AI (the owner, 2026-10-23).

Anant: "check elevation and section drawings for splash details when they are absent from
architectural plans". A vendor's splash size is often compared with the one architect view it was
paired with (a plan), which prints no splash size, while the same sheet's section, or the item's
other sheet, does. This step runs after code's other checks (before the measuring by scale), on
the open vendor splash items the architect gives no size for, of a vendor drawing paired with a
separate architect file (on GV's combined sheets the area's sections are already in the compare).
It looks for the size on the item's other views, in this order:

1. **The same sheet**: the other views of the architect sheet the vendor drawing was paired with
   (`AcrossCandidate.views`: the views code found, or the set index's checked ones), when the
   view's title names the same item, or only its kind (`SECTION`) on a sheet no view of which names
   another item.
2. **The same item elsewhere** in the same project: the one other architect sheet whose checked
   item name equals this one's, or that prints the vendor's own item tag. Two such sheets: no look.
3. **The architect's pointers**: a section or detail callout (`views.callouts`) printed on those
   views, followed only to the one sheet of the project whose number is exactly the callout's, and
   a view numbered exactly so (every tool links only an exact match; nothing is guessed).

Never a brand prototype sheet (R1: the architect governs; a prototype splash is what the updated
design removed), a key plan, another project's sheet, or an older copy of a sheet (when older
copies cannot be worked out, no sheet but the paired one is used). The views the pack's
`look_in_views` names are searched (countertop: plan, elevation, section).

**What counts as printed:** the architect's own words, read from the drawing's text, printing a
size right beside the part's name: before it with at most a measure word between (`4" BACKSPLASH`,
`4" HIGH BACKSPLASH`: a height), or after it with its measure said when the phrase starts with
the part's name (`BACKSPLASH 3/4" THK`). Any other size of the phrase is another part's
(`24" WIDE COUNTERTOP W/ 4" BACKSPLASH`: the 24" is the top's), two sizes joined as one callout
(`4" X 3/4"`) or a size whose measure is not said are not used, and a general note (`U.O.N.`,
`ALL ... UNLESS NOTED`) is never one item's size. Dimension labels inside a found view are counted, never tied to the splash
by code.

**Like for like** (#1909): a back splash is never compared with a side splash, a height never with
a thickness or a length, and a splash's Right never stands on an architect size code does not see
printed as the same splash's (`unlike`, `right_unproved`: the meeting set's "back splash length
36"" against the plan's top width 36" was a false Right). The steps that make a Right on equal
values apply it: this step (to the helpers' Rights), the escalation's printed compare, the
look-again's proofs and the round reference; code's own pairing by geometry and the devil's
advocate's pass stand. Only words code read count, never the AI's typed words. One exception
(Anant, 2026-10-25): a back splash's length (no other kind, never a height or thickness) may be
compared with the reference's run of the top when code sees that run go from wall to wall (a wall
drawn at both ends, neither end a cabinet's side, the overhang's edge or a field cut;
`wall_run_part`, `dim_ends.wall_to_wall`, the escalation's printed compare and chain sums): the
same is Right, a difference changes nothing. (The simple
check's compare has its own strict end words: the top of a side splash, the face of a splash.)

**Outcomes** (the item keeps its vendor spot; code never guesses a value):

- printed the same: Right (both values read by code, the architect's from its words);
- printed differently: a Likely problem ("please check and confirm"), never red: the architect's
  size is in its words, not on a dimension line, so no blind pick or code's ends can prove the two
  measure the same thing;
- two found views (or one view twice) print it differently: GV asks the architect (the #1907
  shape), and the vendor is compared with the governing view (a section over an elevation over a
  plan, settled);
- a thickness no view prints: the job's backsplash thickness, labelled "job value" (the owner's
  default, 2026-10-23): the same is Right, a different one a Likely problem, never red;
- nothing printed: Not compared, "the architect shows no splash size", naming every view looked
  at, and saying when numbers there are pen strokes code cannot read yet. A drawn splash with no
  size is left to the measuring by scale (`scale_measure`), which runs next.

The whole-drawing look's "the architect draws no splash" (a Likely problem) is not kept when a
found view prints the splash's size, or when a found view or the page holds so many pen-stroke
numbers code could not read (`Search.doubt`) that code cannot confirm the absence (the meeting
set's sections draw the splash in strokes); the item is then Not compared with that reason. With
a few unread numbers, it stays as the look said.

The small AI question (`assistant_splash_question`, off by default) only adds the open items this
step found views for to the roster's existing look-again, with the views to look in
(`splash_question_items`, `look_in_line`): no new call.

Verification: `tests/review/assistant/test_splash_views.py` and the made-up uploads in
`tests/review/assistant/test_hard_uploads.py`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from app.review.assistant import likely_problems
from app.review.assistant.finding_checks import claim_value, dimension_tokens, to_inches

__all__ = [
    "CREATED_BY",
    "FIELD",
    "METHOD",
    "WALL_RUN",
    "Companion",
    "Facts",
    "Part",
    "Reading",
    "Search",
    "add_to_answer",
    "candidates",
    "companion_views",
    "downgrade_unlike",
    "look_in_line",
    "not_alike",
    "note_readings",
    "pack_phrases",
    "part_of",
    "settle",
    "splash_item",
    "splash_question_items",
    "subject_of",
    "unlike",
    "wall_run_part",
]

_log = logging.getLogger(__name__)

#: The finding's field with what this step looked at and found.
FIELD: Final = "splash_views"
#: How an item this step settled says so (`settled.method`).
METHOD: Final = "splash_views"
#: Who makes this step's own items (the "Ask the architect" ones).
CREATED_BY: Final = "splash_views"
#: A phrase this close to an architect label (points) says what the label measures.
NEAR_PT: Final = 40.0
#: The measures of a part, from the words beside it (generic trade words).
_MEASURES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("thickness", r"\bthick(?:ness)?\b|\bthk\b|(?<![a-z])t\s*=|\bdepth\b|\bdeep\b"),
        ("height", r"\bheights?\b|\bhigh\b|\bht\b|\btall\b|(?<![a-z])h\b"),
        ("length", r"\blengths?\b|\blong\b|\blg\b|\bwidths?\b|\bwide\b|\bruns?\b"),
    )
)
#: A view title's words that name only its kind (the sheet's own item then).
_KIND_WORDS: Final = frozenset(
    {
        "SECTION",
        "SECTIONS",
        "ELEVATION",
        "ELEVATIONS",
        "ELEV",
        "PLAN",
        "PLANS",
        "DETAIL",
        "DETAILS",
        "ENLARGED",
        "VIEW",
        "TYPICAL",
        "TYP",
        "SCALE",
        "AT",
        "OF",
        "THE",
        "AND",
        "FOR",
        "WITH",
    }
)
_TITLE_WORD: Final = re.compile(r"[A-Z][A-Z]{2,}")
#: The governing order of the views (settled: a section or detail over an elevation over a plan;
#: a titled drawing of no known kind last).
GOVERNING: Final = ("section", "elevation", "plan", "drawing")
#: The views searched when no pack names them.
_DEFAULT_VIEWS: Final = ("plan", "elevation", "section")
_OPEN: Final = frozenset({"check"})


# ---- the part a text names ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """A pack's `look_in_views` entry, as this step reads it."""

    part: str
    words: tuple[str, ...]
    kinds: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    views: tuple[str, ...] = _DEFAULT_VIEWS


def _rules() -> tuple[Rule, ...]:
    registry = import_module("vocabulary.product_registry").registry()
    return tuple(
        Rule(r.part, tuple(r.words), dict(r.kinds), tuple(r.views))
        for product in registry.products
        for r in getattr(product, "look_in_views", ())
    )


def _pattern(words: Iterable[str]) -> re.Pattern[str]:
    return _compiled(tuple(words))


@lru_cache(maxsize=256)
def _compiled(words: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(w).replace("\\ ", r"\s*") for w in sorted(words, key=len, reverse=True)
    )
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?:e?s)?(?![a-z0-9])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Part:
    """What a text says is measured: the part (`splash`), its kind (`back`, `side`; `None`: not
    said, or both), and the measure (`height`, `thickness`, `length`; `None`: not said)."""

    part: str
    kind: str | None = None
    measure: str | None = None

    @property
    def words(self) -> str:
        return " ".join(w for w in (self.kind, self.part, self.measure) if w)


def _measures(text: str) -> list[str]:
    return [name for name, pattern in _MEASURES if pattern.search(text)]


def part_of(text: object, rules: Sequence[Rule] | None = None) -> Part | None:
    """The part `text` names (see `Part`), or `None` when it names none of the packs' parts."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return None
    for rule in _rules() if rules is None else rules:
        if not _pattern(rule.words).search(flat):
            continue
        kinds = [kind for kind, words in rule.kinds.items() if _pattern(words).search(flat)]
        measures = _measures(flat)
        return Part(
            rule.part,
            kinds[0] if len(kinds) == 1 else None,
            measures[0] if len(measures) == 1 else None,
        )
    return None


#: A word before the part's name that makes it an end of another dimension, not the thing
#: measured ("counter height to the top of the splash", "outlet in the backsplash").
_END_BEFORE: Final = re.compile(
    r"(?:\b(?:to|from|of|in|on|at|above|below|under|over|behind|into|beside|near|with|incl"
    r"|including|excl|plus|less|minus|without|excluding|and|thru|through)\.?|\bw/|[+&])"
    r"(?:\s+(?:the|a|an))?\s*$",
    re.IGNORECASE,
)


def subject_of(text: object, rules: Sequence[Rule] | None = None) -> Part | None:
    """The part `text` measures (`part_of`), or `None` when every mention of it is an end of
    another dimension (`to the top of the splash`) or it names none."""
    flat = " ".join(str(text or "").split())
    for rule in _rules() if rules is None else rules:
        mentions = list(_pattern(rule.words).finditer(flat))
        if mentions and any(not _END_BEFORE.search(flat[: m.start()]) for m in mentions):
            return part_of(flat, [rule])
    return None


def _rule_of(part: Part, rules: Sequence[Rule]) -> Rule | None:
    return next((r for r in rules if r.part == part.part), None)


def _clash(a: Part, b: Part) -> str | None:
    """Why two parts are not like for like, or `None`. Like for like is never assumed: a kind
    (back, side) said on one side only, or a measure not said on both, is not like for like (a
    vendor's "splash height" may be the side splash's; review of #1918)."""
    if a.part != b.part:
        return f"a {a.part} against a {b.part}"
    if a.kind != b.kind:
        if a.kind and b.kind:
            return f"a {a.kind} {a.part} against a {b.kind} {b.part}"
        said = a.kind or b.kind
        return f"only one side says it is the {said} {a.part}"
    if not a.measure or not b.measure:
        return f"what is measured on the {a.part} is not said on both sides"
    if a.measure != b.measure:
        return f"the {a.part}'s {a.measure} against its {b.measure}"
    return None


# ---- like for like ------------------------------------------------------------------------------


def _box(raw: object) -> tuple[float, float, float, float] | None:
    if isinstance(raw, Mapping):
        try:
            x0, y0, x1, y1 = (float(raw[k]) for k in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            return None
    elif isinstance(raw, Sequence) and not isinstance(raw, str) and len(raw) == 4:
        try:
            x0, y0, x1, y1 = (float(v) for v in raw)
        except (TypeError, ValueError):
            return None
    else:
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _gap(a: Sequence[float], b: Sequence[float]) -> float:
    dx = max(a[0] - b[2], 0.0, b[0] - a[2])
    dy = max(a[1] - b[3], 0.0, b[1] - a[3])
    return float((dx * dx + dy * dy) ** 0.5)


def _architect_spot(finding: Mapping[str, Any]) -> tuple[str, int, tuple[float, ...]] | None:
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    label = (dims or {}).get("architect")
    if isinstance(label, Mapping):
        box = _box(label.get("box"))
        page = label.get("page")
        if box is not None and isinstance(page, int):
            return str(label.get("file") or ""), page, box
    spot = finding.get("arch_box")
    box = _box(spot)
    if box is not None and isinstance(spot, Mapping) and isinstance(spot.get("page"), int):
        return str(spot.get("file") or ""), int(spot["page"]), box
    return None


type Phrases = Mapping[tuple[str, int], Sequence[Mapping[str, Any]]]


_WHOLE: Final = (-1e9, -1e9, 1e9, 1e9)


def _printed_part(
    text: object, value: Fraction | None, rules: Sequence[Rule], view: Companion | None = None
) -> Part | None:
    """The part a printed phrase sizes, when it prints `value` (any value when `None`) right
    beside the part's name (`note_readings`), else `None`."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return None
    place = view or Companion("", 0, "drawing", "", _WHOLE, "")
    found = note_readings(place, [{"text": flat, "box": [0.0, 0.0, 1.0, 1.0]}], rules)
    return next((r.part for r in found if value is None or r.value == value), None)


def _architect_value(finding: Mapping[str, Any]) -> Fraction | None:
    raw = (
        (finding.get("inches") or {}).get("architect")
        if isinstance(finding.get("inches"), Mapping)
        else None
    )
    try:
        return Fraction(Decimal(str(raw))) if raw is not None else None
    except (InvalidOperation, ValueError):
        return None


def unlike(
    finding: Mapping[str, Any],
    phrases: Phrases | None = None,
    rules: Sequence[Rule] | None = None,
) -> str | None:
    """Why a splash item's two sides are not like for like, or `None` (alike, or not a splash
    item). The vendor's side names the part it measures (the item's words). The architect's side
    must be shown to size the same part by printed words code read (never the AI's typed words):
    its label's own text (`4" BACKSPLASH`),
    or a printed phrase within `NEAR_PT` of its label that prints the architect's same value right
    beside the part's name (`phrases`: the pages' `dims.json` phrases by (file, page)). A splash
    word merely near the label proves nothing. Then the kind (back, side) and the measure (height,
    thickness, length) must not differ."""
    rules = _rules() if rules is None else rules
    vendor = _vendor_part(finding, rules)
    if vendor is None:
        return None
    architect = _architect_part(finding, phrases, rules)
    if architect is None:
        return (
            f"the architect's size is not shown to be the {vendor.part}'s (it may be another "
            "part's size, such as the top's own)"
        )
    return _clash(vendor, architect)


def _architect_part(
    finding: Mapping[str, Any], phrases: Phrases | None, rules: Sequence[Rule]
) -> Part | None:
    """The part the architect's side is printed as sizing (`unlike`), or `None`."""
    value = _architect_value(finding)
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    label = (dims or {}).get("architect")
    # Only what code read on the drawing counts: the label's own text, or the page's phrases;
    # never the words the AI typed for the architect's side.
    architect = None
    if isinstance(label, Mapping):
        architect = _printed_part(label.get("text"), None, rules)
    if architect is None and phrases is not None and value is not None:
        spot = _architect_spot(finding)
        if spot is not None:
            file, page, box = spot
            near = sorted(
                (
                    (_gap(at, box), part)
                    for phrase in phrases.get((file, page), ())
                    if (at := _box(phrase.get("box"))) is not None and _gap(at, box) <= NEAR_PT
                    if (part := _printed_part(phrase.get("text"), value, rules)) is not None
                ),
                key=lambda pair: pair[0],
            )
            if near:
                architect = near[0][1]
    return architect


#: How an item settled on the top's run between two walls says so (`FIELD`'s `by`).
WALL_RUN: Final = "wall_run"


def wall_run_part(
    finding: Mapping[str, Any],
    phrases: Phrases | None = None,
    rules: Sequence[Rule] | None = None,
) -> Part | None:
    """The vendor's splash whose LENGTH may be compared with the reference's run of the top
    between two walls (Anant, 2026-10-25, relaxing #1918 for this one case), or `None`. The
    vendor's words must say it measures the splash's length (never its height or thickness), and
    no kind but the back one (a side splash runs from the wall to the front edge, never wall to
    wall; "side and back splashes" is not one length). The architect's size must be printed as no
    part's: when its own words name a part, `unlike` decides as before. The caller still needs
    code to see a wall at both ends of the reference's run (`dim_ends.wall_to_wall`)."""
    rules = _rules() if rules is None else rules
    vendor = _vendor_part(finding, rules)
    if vendor is None or vendor.measure != "length":
        return None
    rule = _rule_of(vendor, rules)
    text = " ".join(" ".join(str(finding.get(key) or "").split()) for key in ("item", "vendor"))
    said = {
        kind for kind, words in (rule.kinds if rule else {}).items() if _pattern(words).search(text)
    }
    if said - {"back"}:
        return None
    if _architect_part(finding, phrases, rules) is not None:
        return None
    return vendor


def splash_item(finding: Mapping[str, Any]) -> bool:
    """Whether the item measures a part of the packs' `look_in_views` (a splash)."""
    return _vendor_part(finding, _rules()) is not None


def not_alike(why: str) -> dict[str, Any]:
    """The step's record on an item whose two sides are not like for like (`unlike`)."""
    return {"outcome": "not_like_for_like", "why": f"not like for like: {why}"}


def downgrade_unlike(finding: Mapping[str, Any], phrases: Phrases | None) -> dict[str, Any] | None:
    """A Right on a splash whose two sides code does not see as like for like, as Not compared
    (a value-equal pairing of a splash length with a top's width is no Right); `None` when it
    stays. Code's own geometric pairing (`paired_by: code`) and the reviewer's decision stand."""
    if finding.get("status") != "same" or finding.get("decision"):
        return None
    if finding.get("paired_by") == "code" or finding.get("source") not in (None, "architect"):
        return None
    settled = finding.get("settled")
    if isinstance(settled, Mapping) and settled.get("method") == METHOD:
        return None
    record = finding.get(FIELD)
    if isinstance(record, Mapping) and record.get("by") == WALL_RUN:
        return None  # compared with the top's run between two walls code sees (2026-10-25)
    why = unlike(finding, phrases)
    if why is None:
        return None
    out = dict(finding)
    out["status"] = "check"
    out["keep_reason"] = "measures_something_else"
    out.pop("settled", None)
    record = finding.get(FIELD)
    said = not_alike(why)
    if isinstance(record, Mapping) and record.get("why"):
        # The step's own record (the views it looked at) is kept beside the reason.
        said = {**record, **said, "why": f"{said['why']}; {record['why']}"}
    out[FIELD] = said
    return out


def pack_phrases(folder: Path | str | None) -> Phrases:
    """Each page's printed phrases (`dims.json`), by (file, page), for `unlike`; empty without a
    pack."""
    if folder is None:
        return {}
    dims = _read(Path(folder) / "dims.json")
    out: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for entry in (dims or {}).get("pages") or [] if isinstance(dims, Mapping) else []:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            out[(str(entry.get("file")), int(entry["page"]))] = [
                p for p in entry.get("phrases") or [] if isinstance(p, Mapping)
            ]
    return out


# ---- the set's facts ------------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Facts:
    """What this step reads of the set pack, by (file, own page)."""

    phrases: Phrases = field(default_factory=dict)
    unread: Mapping[tuple[str, int], tuple[tuple[float, float, float, float], ...]] = field(
        default_factory=dict
    )
    """The numbers in pen strokes code could not read on the architect's side
    (`reading_coverage`)."""
    index: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """The set index's checked reads (`set_index.load`), by `file#page`."""
    roles: Mapping[tuple[str, int], str] = field(default_factory=dict)
    """Each page's role (the page map)."""
    older: frozenset[tuple[str, int]] = frozenset()
    """Architect pages that are older copies of a later sheet (`sheet_revisions`)."""
    printed: Mapping[tuple[str, int], int] = field(default_factory=dict)
    """The numbers each page prints on the architect's side, read or not (`reading_coverage`)."""
    older_known: bool = True
    """Older copies were worked out; `False` (it failed): no other sheet than the paired one is
    used (fail closed)."""

    @classmethod
    def from_pack(cls, folder: Path) -> Facts:
        phrases: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
        dims = _read(folder / "dims.json")
        for entry in (dims or {}).get("pages") or [] if isinstance(dims, Mapping) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                phrases[(str(entry.get("file")), int(entry["page"]))] = [
                    p for p in entry.get("phrases") or [] if isinstance(p, Mapping)
                ]
        unread: dict[tuple[str, int], tuple[tuple[float, float, float, float], ...]] = {}
        printed: dict[tuple[str, int], int] = {}
        coverage = import_module("app.review.assistant.reading_coverage").load(folder)
        for entry in (coverage or {}).get("pages") or []:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
                continue
            boxes = tuple(
                b
                for one in entry.get("not_read") or []
                if isinstance(one, Mapping)
                and one.get("side") == "architect"
                and one.get("kind") == "strokes"
                and (b := _box(one.get("box"))) is not None
            )
            key = (str(entry.get("file")), int(entry["page"]))
            if boxes:
                unread[key] = boxes
            side = (
                (entry.get("sides") or {}).get("architect")
                if isinstance(entry.get("sides"), Mapping)
                else None
            )
            if isinstance(side, Mapping) and isinstance(side.get("printed"), int):
                printed[key] = int(side["printed"])
        index = import_module("app.review.assistant.set_index").load(folder)
        page_map = _read(folder / "page_map.json")
        older = _older(folder, page_map, index)
        roles: dict[tuple[str, int], str] = {}
        for entry in (page_map or {}).get("pages") or [] if isinstance(page_map, Mapping) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                roles[(str(entry.get("file")), int(entry["page"]))] = str(entry.get("role") or "")
        return cls(
            phrases=phrases,
            unread=unread,
            index=index,
            roles=roles,
            older=older if older is not None else frozenset(),
            printed=printed,
            older_known=older is not None,
        )

    def index_of(self, file: str, page: int) -> Mapping[str, Any]:
        return self.index.get(f"{file}#{page}") or {}


def _older(folder: Path, page_map: Any, index: Any) -> frozenset[tuple[str, int]] | None:
    """The architect's pages that are older copies of a later sheet (an earlier revision)."""
    if not isinstance(page_map, Mapping):
        return frozenset()
    revisions = import_module("app.review.assistant.sheet_revisions")
    consistency = import_module("app.review.assistant.vendor_consistency")
    roles = (
        page_map.get("upload_roles") if isinstance(page_map.get("upload_roles"), Mapping) else {}
    )
    files = [str(n) for n, r in (roles or {}).items() if r == "architect"]
    if not files:
        return frozenset()
    try:
        texts = consistency.page_texts(folder, list(page_map.get("files") or files))
        dims = _read(folder / "dims.json")
        out: set[tuple[str, int]] = set()
        for file in files:
            found = revisions.older_copies(
                page_map,
                texts,
                file,
                exact=revisions.exact_copies(file, dims, texts),
                index={"pages": index} if index else None,
                role="architect",
            )
            out |= {(file, page) for page in found}
        return frozenset(out)
    except Exception:  # noqa: BLE001 - not known: only the paired sheet is used (fail closed)
        _log.warning("splash views: older copies not worked out", exc_info=True)
        return None


# ---- the views to look in -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Companion:
    """One architect view of the item this step looks in."""

    file: str
    page: int
    """Its page in the architect's own file."""
    kind: str
    """`section`, `elevation`, `plan`, or `drawing` (a drawing whose title names no kind)."""
    title: str
    box: tuple[float, float, float, float]
    how: str
    """`the same sheet`, `the same item` or `a callout`."""
    labels: int = 0
    """Code's text labels inside it (never tied to the splash by code)."""
    unread: int = 0
    """Numbers in pen strokes code could not read inside it."""

    @property
    def words(self) -> str:
        """`section A5 'ADA VANITY SECTION' on page 20`."""
        title = " ".join(self.title.split())
        kind = "drawing" if self.kind == "drawing" else self.kind
        generic = not _item_words(title)
        named = f"the {kind}" if generic or not title else f"the {kind} '{title}'"
        return f"{named} (page {self.page})"


#: Trade words every item shares: never what tells one item's view from another's
#: ("RECEPTION COUNTERTOP SECTION" is not the break room's countertop).
_TRADE_WORDS: Final = frozenset(
    {
        "COUNTERTOP",
        "COUNTERTOPS",
        "COUNTER",
        "COUNTERS",
        "TOP",
        "TOPS",
        "MILLWORK",
        "CASEWORK",
        "ROOM",
        "SPLASH",
        "BACKSPLASH",
        "SIDESPLASH",
        "INTERIOR",
        "SHEET",
        "DRAWING",
        "DRAWINGS",
    }
)


def _item_words(text: object) -> set[str]:
    return {
        w
        for w in _TITLE_WORD.findall(str(text or "").upper())
        if w not in _KIND_WORDS and w not in _TRADE_WORDS
    }


def _names_this(words: set[str], item: set[str]) -> bool:
    """Whether a view title's item words name this item: one set holds the other ("VANITY" and
    "PRIMARY VANITY"), never a word in common alone ("ADA VANITY" is not "PRIMARY VANITY")."""
    return bool(words) and bool(item) and (words <= item or item <= words)


def _kind_of(drawing: Any) -> str:
    views = import_module("app.review.assistant.simple_check.views")
    for title in getattr(drawing, "titles", ()) or ():
        kind = views._kind(str(title))
        if kind == "detail":
            return "section"
        if kind in ("plan", "elevation"):
            return str(kind)
    return "drawing"


def _views_of(candidate: Any) -> list[Any]:
    return list(getattr(candidate, "views", ()) or ()) or list(candidate.drawings)


def _inside(box: Sequence[float], within: Sequence[float], slack: float = 2.0) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return (
        within[0] - slack <= cx <= within[2] + slack
        and within[1] - slack <= cy <= within[3] + slack
    )


def _companion(drawing: Any, how: str, facts: Facts) -> Companion:
    file, page = str(drawing.file), int(drawing.own_page)
    box = tuple(float(v) for v in drawing.box)
    unread = sum(1 for b in facts.unread.get((file, page), ()) if _inside(b, box))
    return Companion(
        file=file,
        page=page,
        kind=_kind_of(drawing),
        title=" ".join(str(t) for t in (getattr(drawing, "titles", ()) or ())),
        box=box,  # type: ignore[arg-type]
        how=how,
        labels=sum(1 for lab in drawing.labels if (lab.route or "text") == "text"),
        unread=unread,
    )


def _key(text: object) -> str:
    return " ".join(re.findall(r"[A-Z0-9]+", str(text or "").upper()))


def _usable(candidate: Any, facts: Facts) -> bool:
    if getattr(candidate, "reference", False) or getattr(candidate, "key_plan", False):
        return False
    key = (str(candidate.file), int(candidate.page))
    return facts.roles.get(key) != "brand_prototype" and key not in facts.older


def _same_project(candidate: Any, project: str | None, pair: Any) -> bool:
    """The same project: the same known project, or, when no sheet's project is known, a set of
    one project (when any sheet's project is known, a sheet of unknown project is never used)."""
    if project is not None:
        return getattr(candidate, "project", None) == project
    return all(getattr(c, "project", None) is None for c in pair.candidates)


def _pair_of(work: Any, finding: Mapping[str, Any]) -> Any | None:
    page = finding.get("page")
    for pair in getattr(work, "across", ()) or ():
        vendor = pair.vendor
        if str(vendor.file) == str(getattr(work, "file", vendor.file)) and (
            vendor.own_page == page
        ):
            return pair
    return None


def _paired_page(finding: Mapping[str, Any], pair: Any) -> tuple[str, int] | None:
    """The architect sheet (file, its own page) the item was compared with: its spot's when it is
    on an architect file, else its `vs_page` on the one architect file it can be, else the one
    sheet code paired the vendor drawing with; `None` when that is not one sheet."""
    spot = finding.get("arch_box")
    chosen = {(str(c.file), int(c.page)) for c in pair.candidates if c.id in pair.chosen}
    files = {str(c.file) for c in pair.candidates}
    if (
        isinstance(spot, Mapping)
        and isinstance(spot.get("page"), int)
        and str(spot.get("file") or "") in files
    ):
        return str(spot["file"]), int(spot["page"])
    vs = finding.get("vs_page")
    if isinstance(vs, int) and not isinstance(vs, bool):
        named = {f for f, page in chosen if page == vs} or (files if len(files) == 1 else set())
        return (next(iter(named)), vs) if len(named) == 1 else None
    return next(iter(chosen)) if len(chosen) == 1 else None


@dataclass(frozen=True, slots=True)
class Search:
    """Where this step looked for one item."""

    views: tuple[Companion, ...] = ()
    """The views found, in the search order."""
    notes: tuple[str, ...] = ()
    """What the search could not use, in words."""
    stray: Mapping[int, int] = field(default_factory=dict)
    """Per page looked at: numbers in pen strokes code could not read outside every view found
    (a view code could not find, such as a section titled in strokes)."""

    doubt: bool = False
    """Code cannot confirm the architect draws no such part here: a found view, or a page looked
    at, holds at least `DOUBT_UNREAD` numbers in pen strokes code could not read, a third or more
    of the numbers printed there (a page with a few unread numbers does not count)."""

    @property
    def found(self) -> bool:
        return bool(self.views or self.notes)


#: The fewest unread pen-stroke numbers, and their least share of the numbers printed in a view
#: or on a page, that leave "the architect draws none" unconfirmed (`Search.doubt`).
DOUBT_UNREAD: Final = 5
DOUBT_SHARE: Final = Fraction(1, 3)


def _doubt(facts: Facts, file: str, views: Sequence[Companion], stray: Mapping[int, int]) -> bool:
    for view in views:
        if view.unread >= DOUBT_UNREAD and view.unread >= DOUBT_SHARE * (view.unread + view.labels):
            return True
    for page in {*stray, *(v.page for v in views)}:
        unread = len(facts.unread.get((file, page), ()))
        printed = facts.printed.get((file, page), unread)
        if unread >= DOUBT_UNREAD and unread >= DOUBT_SHARE * max(printed, 1):
            return True
    return False


def _stray(
    facts: Facts, file: str, pages: Iterable[int], views: Sequence[Companion]
) -> dict[int, int]:
    out: dict[int, int] = {}
    for page in dict.fromkeys(pages):
        boxes = facts.unread.get((file, page), ())
        mine = [v.box for v in views if v.page == page]
        count = sum(1 for b in boxes if not any(_inside(b, box) for box in mine))
        if count:
            out[page] = count
    return out


def companion_views(
    work: Any,
    finding: Mapping[str, Any],
    facts: Facts,
    views: Sequence[str] = _DEFAULT_VIEWS,
) -> Search:
    """The views to look in for this item's part (see the module), in the search order, with
    what the search could not use. Empty when the item has no architect page paired."""
    pair = _pair_of(work, finding)
    if pair is None:
        return Search()
    paired = _paired_page(finding, pair)
    if paired is None:
        return Search()
    file, page = paired
    sheet = next((c for c in pair.candidates if str(c.file) == file and int(c.page) == page), None)
    if sheet is None:
        return Search()
    if not _usable(sheet, facts):
        return Search(notes=(f"page {page} is a brand prototype or an older copy: not used",))
    allowed = set(views) | {"drawing"}
    vendor_index = facts.index_of(str(pair.vendor.file), int(pair.vendor.own_page))
    sheet_index = facts.index_of(str(sheet.file), int(sheet.page))
    item = (
        _item_words(sheet_index.get("item"))
        | _item_words(vendor_index.get("item"))
        | _item_words(_item_name(finding))
    )
    found: list[Companion] = []
    seen: set[tuple[str, int, tuple[float, ...]]] = set()
    notes: list[str] = []

    def add(drawing: Any, how: str) -> None:
        comp = _companion(drawing, how, facts)
        key = (comp.file, comp.page, comp.box)
        if key in seen or comp.kind not in allowed:
            return
        seen.add(key)
        found.append(comp)

    # 1. The same sheet: its views whose title names this item, and its views titled only by
    # their kind (`SECTION`) or not titled, when the sheet names no other item (a sheet of several
    # items, or one whose item is not known: a bare "SECTION" may be another item's).
    own = _views_of(sheet)
    named = [
        title_words
        for d in own
        for t in getattr(d, "titles", ()) or ()
        if (title_words := _item_words(t))
    ]
    mine = [one for one in named if _names_this(one, item)]
    several = any(not _names_this(one, item) for one in named)
    if len({frozenset(w) for w in mine}) > 1:
        # Two different item names on the sheet both fit this item's words ("VANITY" against
        # "PRIMARY VANITY" and "ADA VANITY"): which is the item's is not known.
        mine, several = [], True
    for drawing in own:
        titles = [str(t) for t in getattr(drawing, "titles", ()) or ()]
        its = [w for t in titles if (w := _item_words(t))]
        if its:
            if any(w in mine for w in its):
                add(drawing, "the same sheet")
        elif not several and (item or not named):
            add(drawing, "the same sheet")
    if not facts.older_known:
        # Older copies of sheets could not be told apart: no other sheet is used.
        notes.append("older copies of sheets could not be told apart: only this sheet was used")
        pages = [page, *(v.page for v in found)]
        stray = _stray(facts, file, pages, found)
        return Search(tuple(found), tuple(notes), stray, _doubt(facts, file, found, stray))
    # 2. The same item elsewhere in the project: exactly one other sheet.
    project = getattr(sheet, "project", None)
    name = _key(sheet_index.get("item"))
    tags = {_key(t) for t in vendor_index.get("item_tags") or [] if len(_key(t)) >= 4}
    others = []
    own_number = _key(sheet_index.get("sheet"))
    for candidate in pair.candidates:
        if (str(candidate.file), int(candidate.page)) == (file, page):
            continue
        if not _usable(candidate, facts) or not _same_project(candidate, project, pair):
            continue  # another project's sheet (or one of unknown project) is never used
        number = _key(facts.index_of(str(candidate.file), int(candidate.page)).get("sheet"))
        if own_number and number == own_number:
            continue  # the paired sheet's own number: a copy of it (an older revision, maybe)
        read = facts.index_of(str(candidate.file), int(candidate.page))
        their_tags = {_key(t) for t in read.get("item_tags") or []} | {
            _key(t) for d in _views_of(candidate) for t in getattr(d, "tags", ()) or ()
        }
        if (name and _key(read.get("item")) == name) or (tags & their_tags):
            others.append(candidate)
    if len(others) == 1:
        for drawing in _views_of(others[0]):
            add(drawing, "the same item")
    elif len(others) > 1:
        notes.append(
            f"{len(others)} other sheets name the same item (pages "
            f"{', '.join(str(c.page) for c in others[:6])}): none is used"
        )
    # 3. The architect's callouts on the views found, to a sheet and view numbered exactly so.
    for target in _callout_targets(pair, found, facts, project):
        add(target, "a callout")
    pages = [page, *(v.page for v in found)]
    stray = _stray(facts, file, pages, found)
    return Search(tuple(found), tuple(notes), stray, _doubt(facts, file, found, stray))


def _item_name(finding: Mapping[str, Any]) -> str:
    """The item's name without the part's own words (`Vanity: back splash height` -> `Vanity`)."""
    item = str(finding.get("item") or "")
    return item.split(":", 1)[0] if ":" in item else ""


def _callout_targets(
    pair: Any, found: Sequence[Companion], facts: Facts, project: str | None
) -> list[Any]:
    views = import_module("app.review.assistant.simple_check.views")
    numbers: dict[tuple[str, int], tuple[str | None, list[Any]]] = {}
    for candidate in pair.candidates:
        if not _usable(candidate, facts) or not _same_project(candidate, project, pair):
            continue
        key = (str(candidate.file), int(candidate.page))
        page_phrases = [
            (str(p.get("text") or ""), b)
            for p in facts.phrases.get(key, ())
            if (b := _box(p.get("box"))) is not None
        ]
        titled = views.find_views(page_phrases)
        sheet = views.sheet_number(page_phrases, titled)
        read = facts.index_of(*key)
        sheet = sheet or (views.normal_sheet(str(read.get("sheet") or "")) if read else None)
        numbered: list[tuple[str, str]] = []
        for view in titled:
            if view.number:
                numbered.append((view.number, view.title))
        for one in read.get("views") or [] if read else []:
            if isinstance(one, Mapping) and one.get("number"):
                numbered.append((str(one["number"]), str(one.get("title") or "")))
        numbers[key] = (sheet, [(n, t, candidate) for n, t in numbered])
    sheets = {s for s, _v in numbers.values() if s}
    out: list[Any] = []
    for comp in found:
        page_phrases = [
            (str(p.get("text") or ""), b)
            for p in facts.phrases.get((comp.file, comp.page), ())
            if (b := _box(p.get("box"))) is not None and _inside(b, comp.box)
        ]
        own_sheet = numbers.get((comp.file, comp.page), (None, []))[0]
        for callout in views.callouts(page_phrases, (), sheets):
            if callout.sheet == own_sheet:
                continue  # never a sheet to itself
            targets = [k for k, (s, _v) in numbers.items() if s == callout.sheet]
            if len(targets) != 1:
                continue
            for number, title, candidate in numbers[targets[0]][1]:
                if number not in callout.numbers:
                    continue
                match = [
                    d
                    for d in _views_of(candidate)
                    if any(_key(t) == _key(title) for t in getattr(d, "titles", ()) or ())
                ]
                if len(match) == 1:
                    out.append(match[0])
    return out


# ---- what the views print -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """A size the architect's words print for the part in one view."""

    value: Fraction
    text: str
    """The phrase as printed (`4" BACKSPLASH`)."""
    part: Part
    view: Companion
    box: tuple[float, float, float, float]


#: A bare size before the part's name (`4" BACKSPLASH`) is its height only from this size up: a
#: splash is never this thin, and a thickness never this big (`3/4" BACKSPLASH` says nothing).
BARE_HEIGHT_MIN: Final = 2

_SIZED: Final = re.compile(r"\d\s*(?:\"|''|”|')")


def _sizes(text: str) -> list[tuple[str, int, int]]:
    """Each size printed with an inch or foot mark in `text`: the token and where it is."""
    out: list[tuple[str, int, int]] = []
    start = 0
    for token in dimension_tokens(text):
        at = text.find(token, start)
        if at < 0:
            continue
        start = at + len(token)
        if _SIZED.search(token) is None:
            continue  # a bare number is no size in a note (`SS 01`)
        out.append((token, at, at + len(token)))
    return out


#: A general note (`U.O.N.`, `ALL ... UNLESS NOTED`): never one item's size (`TYP.` is read).
_GENERAL: Final = re.compile(
    r"\bU\.?\s?O\.?\s?N\b|\bALL\b|\bUNLESS\b|\bOTHERWISE\b|\bU\.?N\.?O\b|\bGENERAL\b",
    re.IGNORECASE,
)
#: Two sizes joined as one callout (`4" X 3/4"`): which is which is not said, so neither is read.
_PAIRED_SIZES: Final = re.compile(r"^\s*[xX\u00d7]\s*$")
#: Between a size and the part's name after it: nothing, or one measure word (`4" HIGH`).
_BEFORE_PART: Final = re.compile(r"^\s*(?:([A-Za-z]{1,9})\.?\s+)?$")
#: Between the part's name and a size after it: nothing, a colon or one measure word.
_AFTER_PART: Final = re.compile(r"^\s*[:=,-]?\s*(?:([A-Za-z]{1,9})\.?\s*[:=]?\s*)?$")


def note_readings(
    view: Companion, phrases: Sequence[Mapping[str, Any]], rules: Sequence[Rule]
) -> list[Reading]:
    """The part's sizes the architect's words print inside `view` (see the module). A size counts
    only right beside the part's name: before it, with at most one measure word between
    (`4" BACKSPLASH`, `4" HIGH BACKSPLASH`: a height), or after it, its measure said by the word
    after it (`BACKSPLASH 3/4" THK`). Any other size of the phrase is another part's
    (`24" WIDE COUNTERTOP W/ 4" BACKSPLASH`: the 24" is the top's). A general note
    (`U.O.N.`, `ALL ... UNLESS NOTED`) is never read; `TYP.` on the item's own view is. Two sizes
    joined as one callout (`4" X 3/4" BACKSPLASH`) are not read: which is the height is not said.
    A size after the part's name counts only when the phrase starts with it (`BACKSPLASH 3/4"
    THK`; in `COUNTERTOP W/ BACKSPLASH 25" DEEP` the 25" is the top's)."""
    out: list[Reading] = []
    for phrase in phrases:
        box = _box(phrase.get("box"))
        text = " ".join(str(phrase.get("text") or "").split())
        if box is None or not text or not _inside(box, view.box) or _GENERAL.search(text):
            continue
        rule = next((r for r in rules if _pattern(r.words).search(text)), None)
        if rule is None:
            continue
        sizes = _sizes(text)
        if not sizes:
            continue
        for mention in _pattern(rule.words).finditer(text):
            kinds = [
                k for k, words in rule.kinds.items() if _pattern(words).search(mention.group(0))
            ]
            kind = kinds[0] if len(kinds) == 1 else None
            for n, (token, at, end) in enumerate(sizes):
                value = to_inches(token)
                if value is None or value <= 0:
                    continue
                joined = (n > 0 and _PAIRED_SIZES.match(text[sizes[n - 1][2] : at])) or (
                    n + 1 < len(sizes) and _PAIRED_SIZES.match(text[end : sizes[n + 1][1]])
                )
                if joined:
                    continue
                measure: str | None = None
                if end <= mention.start():
                    between = _BEFORE_PART.match(text[end : mention.start()])
                    if between is None:
                        continue
                    word = between.group(1)
                    if word is None and value >= BARE_HEIGHT_MIN:
                        # `4" BACKSPLASH`: its height. A bare `3/4" BACKSPLASH` may be its
                        # thickness: its measure is not said, so it is not read.
                        measure = "height"
                    elif word is None:
                        continue
                    else:
                        found = _measures(word)
                        measure = found[0] if len(found) == 1 else None
                elif at >= mention.end() and not text[: mention.start()].strip():
                    between = _AFTER_PART.match(text[mention.end() : at])
                    if between is None:
                        continue
                    upto = sizes[n + 1][1] if n + 1 < len(sizes) else len(text)
                    words = _measures(text[end:upto][:12])
                    if between.group(1):
                        words = _measures(between.group(1)) or words
                    measure = words[0] if len(words) == 1 else None
                if measure is None:
                    continue
                out.append(
                    Reading(Fraction(value), text, Part(rule.part, kind, measure), view, box)
                )
    return out


# ---- settling one item --------------------------------------------------------------------------


@dataclass(slots=True)
class Settled:
    """What this step did to one item: the item as it now is, and its own new items (GV's
    question to the architect when two views disagree)."""

    finding: dict[str, Any]
    added: list[dict[str, Any]] = field(default_factory=list)


def _vendor_value(finding: Mapping[str, Any]) -> Fraction | None:
    """The vendor's value when code read it at its spot (its label, or the printed check)."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    label = (dims or {}).get("vendor")
    checks = finding.get("checks") if isinstance(finding.get("checks"), Mapping) else {}
    read = isinstance(label, Mapping) and str(label.get("route") or "text") == "text"
    read = read or (checks or {}).get("printed_vendor") == "match"
    if not read:
        return None
    raw = (
        (finding.get("inches") or {}).get("vendor")
        if isinstance(finding.get("inches"), Mapping)
        else None
    )
    try:
        if raw is not None:
            return Fraction(Decimal(str(raw)))
    except (InvalidOperation, ValueError):
        pass
    claim = claim_value(str(finding.get("vendor") or ""))
    value = None if claim is None else to_inches(claim)
    return None if value is None else Fraction(value)


def _vendor_number(finding: Mapping[str, Any]) -> Fraction | None:
    """The size the vendor's side claims (read by code or not), or `None`."""
    raw = (
        (finding.get("inches") or {}).get("vendor")
        if isinstance(finding.get("inches"), Mapping)
        else None
    )
    try:
        if raw is not None:
            return Fraction(Decimal(str(raw)))
    except (InvalidOperation, ValueError):
        pass
    claim = claim_value(str(finding.get("vendor") or ""))
    value = None if claim is None else to_inches(claim)
    return None if value is None else Fraction(value)


def _measure_after(text: str, end: int, upto: int, rule: Rule | None) -> str | None:
    """The measure the words right after a size say (`4" HIGH`, `3/4" THK`), or the height when
    the part's name follows the size straight away (`4" BACKSPLASH`); else `None`."""
    after = text[end:upto]
    words = _measures(after[:12])
    if len(words) == 1:
        return words[0]
    lead = re.match(r"\s*(?:high\s+)?", after, re.IGNORECASE)
    rest = after[lead.end() if lead else 0 :]
    if rule is not None and _pattern(rule.words).match(rest):
        return "height"
    return None


def _vendor_part(finding: Mapping[str, Any], rules: Sequence[Rule]) -> Part | None:
    """The vendor's part, from the item's and the vendor's words. Its measure: the item's own
    measure word when the item prints no size (`Vanity: back splash height`); else the words
    right after the vendor's claimed size (`4" back splash, 49" long` is a height: `long` belongs
    to the 49")."""
    item = " ".join(str(finding.get("item") or "").split())
    text = " ".join(str(finding.get("vendor") or "").split())
    named = subject_of(item, rules)
    part = named or subject_of(text, rules)
    if part is None:
        return None
    kind = part.kind
    own = part_of(text, rules)
    if own is not None and kind is None and named is None:
        kind = own.kind
    if not _sizes(item) and named is not None and part.measure is not None:
        return Part(part.part, kind, part.measure)
    sizes = _sizes(text)
    claim = claim_value(text)
    rule = _rule_of(part, rules)
    for n, (token, _start, end) in enumerate(sizes):
        if claim is not None and token.strip() != claim.strip() and n > 0:
            continue
        upto = sizes[n + 1][1] if n + 1 < len(sizes) else len(text)
        return Part(part.part, kind, _measure_after(text, end, upto, rule))
    if not sizes and own is not None and own.measure is not None:
        return Part(part.part, kind, own.measure)
    return Part(part.part, kind, None)


def _inches_words(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    if not rest:
        return f'{int(whole)}"'
    rest = Fraction(rest).limit_denominator(64)
    return f'{int(whole)} {rest.numerator}/{rest.denominator}"' if whole else f'{rest}"'


def _looked(views: Sequence[Companion]) -> str:
    return ", ".join(v.words for v in views) or "no other view of the item"


def _governing(readings: Sequence[Reading]) -> list[Reading]:
    """The readings of the view that governs (a section over an elevation over a plan)."""
    if not readings:
        return []
    best = min(GOVERNING.index(r.view.kind) for r in readings)
    return [r for r in readings if GOVERNING.index(r.view.kind) == best]


def _label(reading: Reading) -> dict[str, Any]:
    return {
        "id": "note",
        "origin": "note",
        "text": reading.text,
        "inches": str(Decimal(reading.value.numerator) / Decimal(reading.value.denominator)),
        "box": list(reading.box),
        "file": reading.view.file,
        "page": reading.view.page,
        "side": "architect",
        "route": "text",
    }


def _spot(reading: Reading) -> dict[str, Any]:
    x0, y0, x1, y1 = reading.box
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "file": reading.view.file,
        "page": reading.view.page,
    }


@dataclass(frozen=True, slots=True)
class JobValue:
    """The job's value for a part's measure (the backsplash thickness), as `rules.md` gives it."""

    value: Fraction
    text: str
    where: str


def settle(
    finding: Mapping[str, Any],
    search: Search,
    phrases: Phrases,
    *,
    rules: Sequence[Rule] | None = None,
    job: Mapping[str, JobValue] | None = None,
    question: bool = False,
) -> Settled | None:
    """One splash item the architect gives no size for, against the views found (see the
    module); `None` when it is not one or nothing is to change (a whole-drawing look's "no splash
    drawn" that code has no reason to doubt stays as the look said). `job`: the job values by
    measure (`thickness`). `question`: the small AI question is on (the look-again is pointed at
    the views)."""
    rules = _rules() if rules is None else rules
    if finding.get("status") not in _OPEN or finding.get("decision"):
        return None
    part = _vendor_part(finding, rules)
    if part is None:
        return None
    views = search.views
    item = " ".join(str(finding.get("item") or "this splash").split())
    looked = _looked(views)
    readings = [
        r
        for v in views
        for r in note_readings(v, phrases.get((v.file, v.page), ()), rules)
        if _clash(part, r.part) is None
    ]
    absence = _absence_claim(finding)
    if absence and not readings and not search.doubt:
        return None  # nothing code reads says otherwise: the look's Likely problem stays
    out = dict(finding)
    record: dict[str, Any] = {
        "part": part.words,
        "looked_at": [
            {"page": v.page, "kind": v.kind, "title": v.title, "how": v.how, "box": list(v.box)}
            for v in views
        ],
    }
    if absence:
        # The look said the architect draws none; code cannot confirm it (a size is printed, or
        # numbers in pen strokes are not read): never a Likely problem on that.
        mark = out.pop(likely_problems.FIELD, None)
        out["architect"] = "not confirmed by code"
        record["absence_not_confirmed"] = True
        # The look's claim kind (the vendor adds a piece, or leaves one out), kept for the
        # pasted views' step (`pasted_compare`): only an added piece can be withdrawn there.
        record["look_kind"] = mark.get("kind") if isinstance(mark, Mapping) else None
    if part.measure is None:
        record["why"] = (
            f"the architect shows no {part.part} size code can compare: the vendor's value is "
            f"not named as the {part.part}'s height, thickness or length (looked at: {looked})"
            + _unread_words(views, search.stray)
        )
        return _not_compared(out, record, views, question)
    kinds = {r.part.kind for r in readings if r.part.kind}
    if part.kind is None and len(kinds) > 1:
        record["why"] = (
            f"the architect prints sizes for the {' and the '.join(sorted(kinds))} {part.part}; the "
            f"vendor's value does not say which one it is (looked at: {looked})"
        )
        return _not_compared(out, record, views, question)
    governing = _governing(readings)
    added: list[dict[str, Any]] = []
    if governing:
        like = [r for r in readings if _clash(r.part, governing[0].part) is None]
        other = next((r for r in like if r.value != governing[0].value), None)
        if other is not None:
            # Two of the architect's views (or one view twice) print the part differently: GV
            # asks the architect, never red because of it.
            added.append(_ask_architect(finding, governing[0], other, part))
            record["views_disagree"] = True
    values = {r.value for r in governing}
    if len(values) > 1:
        record["why"] = (
            f"the architect's {governing[0].view.words} prints the {part.words} two ways "
            f"({', '.join(_inches_words(v) for v in sorted(values))}): not compared"
        )
        settled = _not_compared(out, record, views, question)
        settled.added = added
        return settled
    vendor = _vendor_value(finding)
    if governing:
        found = governing[0]
        record["read"] = {"text": found.text, "page": found.view.page, "view": found.view.words}
        if vendor is None:
            record["why"] = (
                f"the architect's {found.view.words} prints {found.text}; the vendor's value was "
                "not read by code at its spot"
            )
            settled = _not_compared(out, record, views, question)
            settled.added = added
            return settled
        return Settled(_compared(out, record, item, vendor, found), added)
    if (
        part.measure == "thickness"
        and part.kind == "back"
        and job
        and job.get("thickness") is not None
    ):
        # The job value is the BACKsplash thickness: never a side splash's, nor a splash whose
        # kind is not said (like for like).
        value = job["thickness"]
        record["job_value"] = {"text": value.text, "where": value.where}
        if vendor is not None:
            return Settled(_against_job(out, record, item, vendor, value, looked), added)
    parts = [f"the architect shows no {part.part} size (looked at: {looked})"]
    labelled = [v for v in views if v.labels]
    if labelled:
        parts.append(
            f"the {sum(v.labels for v in labelled)} sizes printed there are not named as the "
            f"{part.part}'s"
        )
    parts.extend(search.notes)
    record["why"] = "; ".join(parts) + _unread_words(views, search.stray)
    return _not_compared(out, record, views, question)


def _unread_words(views: Sequence[Companion], stray: Mapping[int, int]) -> str:
    """`; numbers in pen strokes code cannot read yet: 6 in the section (page 20); check them by
    eye`, or ""."""
    bits = [f"{v.unread} in {v.words}" for v in views if v.unread]
    bits += [
        f"{count} elsewhere on page {page} (in a view code could not find)"
        for page, count in stray.items()
    ]
    if not bits:
        return ""
    return (
        "; numbers in pen strokes code cannot read yet: " + ", ".join(bits) + "; check them by eye"
    )


def _compared(
    out: dict[str, Any], record: dict[str, Any], item: str, vendor: Fraction, found: Reading
) -> dict[str, Any]:
    """The item compared with the size the architect's words print in a found view."""
    out.update(
        {
            "architect": found.text,
            "arch_box": _spot(found),
            "vs_page": found.view.page,
            "source": "architect",
        }
    )
    dims = dict(out.get("dims") or {}) if isinstance(out.get("dims"), Mapping) else {}
    dims["architect"] = _label(found)
    out["dims"] = dims
    checks = dict(out.get("checks") or {}) if isinstance(out.get("checks"), Mapping) else {}
    checks["printed_architect"] = "match"
    checks.pop("verify_architect", None)
    out["checks"] = checks
    inches = dict(out.get("inches") or {}) if isinstance(out.get("inches"), Mapping) else {}
    inches["architect"] = _label(found)["inches"]
    inches["vendor"] = _decimal_text(vendor)
    inches["difference"] = _decimal_text(vendor - found.value)
    out["inches"] = inches
    out.pop("keep_reason", None)
    out.pop("check_reason", None)
    how = (
        f"code read {_inches_words(vendor)} at the vendor's spot and {found.text} in the "
        f"architect's words on {found.view.words}, found on {found.view.how}"
    )
    if vendor == found.value:
        out["status"] = "same"
        out["settled"] = {"outcome": "right", "method": METHOD, "how": how, "was": "check"}
        out["why"] = f"{item}: {how}: the same."
        record["outcome"] = "same"
    else:
        # A difference code read in the architect's words, not on a dimension line: no blind
        # pick or code's ends can prove the two measure the same, so it is a Likely problem
        # ("please check and confirm"), never red.
        out["status"] = "check"
        out.pop("paired_by", None)
        out["keep_reason"] = "architect_prints_nothing"
        out["why"] = f"{item}: {how}: they differ."
        # The item's reason is this comparison, never "the architect gives no size".
        record["why"] = (
            f"the architect's {found.view.words} prints {found.text}; the vendor's drawing shows "
            f"{_inches_words(vendor)}: please check and confirm they measure the same"
        )
        out[likely_problems.FIELD] = {
            "by": METHOD,
            "kind": "size",
            "proof": "likely_only",
            "sure": "likely",
            "why": out["why"],
            "words": (
                f"{likely_problems.LETTER_LEAD}: {item}: the architect's {found.view.words} "
                f"shows {found.text}; the vendor's drawing shows {_inches_words(vendor)}."
            ),
        }
        record["outcome"] = "likely_problem"
    out[FIELD] = record
    return out


def _decimal_text(value: Fraction) -> str:
    return str(Decimal(value.numerator) / Decimal(value.denominator))


def _absence_claim(finding: Mapping[str, Any]) -> bool:
    mark = finding.get(likely_problems.FIELD)
    return isinstance(mark, Mapping) and mark.get("kind") in ("extra_piece", "missing_piece")


def _not_compared(
    out: dict[str, Any], record: dict[str, Any], views: Sequence[Companion], question: bool
) -> Settled:
    out["status"] = "check"
    out.setdefault("keep_reason", "architect_prints_nothing")
    if question and views:
        record["look_in"] = [v.words for v in views]
    record["outcome"] = "not_compared"
    out[FIELD] = record
    return Settled(out)


def _against_job(
    out: dict[str, Any],
    record: dict[str, Any],
    item: str,
    vendor: Fraction,
    job: JobValue,
    looked: str,
) -> dict[str, Any]:
    """A splash thickness no view prints, against the job's value (labelled "job value"): the
    same is Right; a different one is a Likely problem, never red."""
    said = f"no architect view prints it (looked at: {looked}); the job value is {job.text} ({job.where})"
    if job.value == vendor:
        out["status"] = "same"
        out["architect"] = f"{job.text} (job value)"
        out["source"] = "rules"
        out["settled"] = {
            "outcome": "right",
            "method": METHOD,
            "how": f"{said}; the vendor prints {_inches_words(vendor)}",
            "was": "check",
        }
        out["why"] = f"{item}: matches the job value {job.text}: {said}."
        record["outcome"] = "right"
    else:
        out["status"] = "check"
        out["architect"] = f"{job.text} (job value)"
        out[likely_problems.FIELD] = {
            "by": METHOD,
            "kind": "size",
            "proof": "likely_only",
            "sure": "likely",
            "why": f"{said}; the vendor prints {_inches_words(vendor)}",
            "words": (
                f"{likely_problems.LETTER_LEAD}: {item}: the job value is {job.text}; the "
                f"vendor's drawing shows {_inches_words(vendor)}."
            ),
        }
        record["outcome"] = "likely_problem"
    out[FIELD] = record
    return out


def _ask_architect(
    finding: Mapping[str, Any], governing: Reading, other: Reading, part: Part
) -> dict[str, Any]:
    """GV's question to the architect when two of its views print the part differently (the #1907
    shape: a cross-view item on the architect's side, `rule_notes.views_note_of`). Never red."""
    item = _item_name(finding) or " ".join(str(finding.get("item") or "").split())
    return {
        "page": finding.get("page"),
        "vs_page": governing.view.page,
        "item": f"{item}: {part.words}" if item else part.words,
        "category": finding.get("category") or "other",
        "architect": governing.text,
        "vendor": other.text,
        "status": "check",
        "why": (
            f"The architect's own drawings disagree: {other.view.words} shows {other.text}, "
            f"{governing.view.words} shows {governing.text}. GV compares the {part.part} with "
            f"the {governing.view.kind}; GV asks the architect."
        ),
        "verified": True,
        "source": "cross_view",
        "reference_side": "architect",
        "arch_box": _spot(governing),
        "vendor_box": _spot(other),
        "dims": {
            "architect": _label(governing),
            "vendor": _label(other),
            "views": {
                "governing": governing.view.kind,
                "other": other.view.kind,
                "governing_page": governing.view.page,
                "other_page": other.view.page,
                "governs": f"the {part.part}",
            },
        },
    }


# ---- the engine's hook ------------------------------------------------------------------------------


def candidates(
    findings: Iterable[Mapping[str, Any]], rules: Sequence[Rule]
) -> list[dict[str, Any]]:
    """The splash items to look further for: still open, the architect printing no size for
    them (no architect label), not decided by the reviewer, and not this step's own."""
    out = []
    for finding in findings:
        if finding.get("status") not in _OPEN or finding.get("decision"):
            continue
        if finding.get(FIELD) or finding.get("created_by") == CREATED_BY:
            continue
        if finding.get("measured_by_scale"):
            continue
        dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
        if isinstance((dims or {}).get("architect"), Mapping):
            continue
        architect = claim_value(str(finding.get("architect") or ""))
        if (
            architect is not None
            and to_inches(architect) is not None
            and not _absence_claim(finding)
        ):
            continue
        if _vendor_part(finding, rules) is None or _vendor_number(finding) is None:
            continue  # a splash's material or presence, not a size: not this step's
        out.append(dict(finding))
    return out


def _job_values(engine: Any) -> dict[str, JobValue]:
    try:
        rules_md = (engine.tools.workspace.skills / "rules.md").read_text(encoding="utf-8")
    except (OSError, AttributeError, TypeError):
        return {}
    sink = import_module("app.review.assistant.sink_offset")
    found = sink.setting_from(rules_md, "backsplash_thickness", "backsplash thickness")
    if found is None or found.origin not in ("project value", "drawing set value"):
        # Settled with GV: the backsplash thickness is entered per job, never a company value.
        return {}
    return {"thickness": JobValue(found.value, found.text, found.where)}


def _question_on() -> bool:
    try:
        settings = import_module("app.config").get_settings()
    except Exception:  # noqa: BLE001 - no settings: the question stays off
        return False
    return str(getattr(settings, "assistant_splash_question", "off")) == "on"


def add_to_answer(engine: Any) -> dict[str, int] | None:
    """The engine's hook (a full check, at its finish, $0, before the measuring by scale): every
    open splash item the architect gives no size for, looked for on its other views."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    folder = getattr(workspace, "set_pack", None)
    if folder is None:
        return None
    rules = _rules()
    if not rules:
        return None
    book = engine.book
    live = book.live()
    found = candidates(live, rules)
    splashes = [f for f in live if f.get("status") == "same" and part_of(f.get("item"), rules)]
    if not found and not splashes:
        return None
    facts = Facts.from_pack(Path(folder))
    counts = {"looked": 0, "right": 0, "likely": 0, "asked": 0, "not_compared": 0, "unlike": 0}
    for finding in splashes:
        downgraded = downgrade_unlike(finding, facts.phrases)
        if downgraded is not None:
            with engine.finding_lock:
                if book.replace(downgraded):
                    engine._show_finding(downgraded)
                    counts["unlike"] += 1
    if not found:
        return counts
    worklist = import_module("app.review.assistant.simple_check.worklist")
    work = worklist.build_worklist(Path(folder), book.main_file)
    job = _job_values(engine)
    question = _question_on()
    for finding in found:
        part = _vendor_part(finding, rules)
        rule = _rule_of(part, rules) if part is not None else None
        search = companion_views(
            work, finding, facts, rule.views if rule is not None else _DEFAULT_VIEWS
        )
        if not search.found:
            continue  # no architect page paired: the pairing's own reason stands
        settled = settle(finding, search, facts.phrases, rules=rules, job=job, question=question)
        if settled is None:
            continue
        counts["looked"] += 1
        status = settled.finding.get("status")
        if status == "same":
            key = "right"
        elif likely_problems.FIELD in settled.finding:
            key = "likely"
        else:
            key = "not_compared"
        counts[key] += 1
        if settled.finding.get("arch_box") != finding.get("arch_box"):
            # Compared on another view: its picture is that view's spot, never the old one's.
            picture = getattr(book, "_picture", None)
            try:
                made = picture(settled.finding["arch_box"]) if callable(picture) else (None, None)
            except OSError:
                made = (None, None)  # no picture is never a failed step
            settled.finding["arch_picture_id"] = made[0]
        with engine.finding_lock:
            if book.replace(settled.finding):
                engine._show_finding(settled.finding)
            for extra in settled.added:
                added = book.add_code_finding(extra, created_by=CREATED_BY)
                if added is not None:
                    engine._show_finding(added)
                    counts["asked"] += 1
    if counts["looked"] or counts["unlike"]:
        _log.warning("assistant splash views: %s", counts)
    return counts


def splash_question_items(
    findings: Iterable[Mapping[str, Any]], done: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """The open splash items this step gave views to look in (`look_in`: only when the small AI
    question is on), not yet looked at again: the roster's look-again reads those views for them
    (`look_in_line`). Empty when the question is off."""
    seen = {str(d) for d in done}
    out = []
    for finding in findings:
        record = finding.get(FIELD)
        if not isinstance(record, Mapping) or not record.get("look_in"):
            continue
        if finding.get("status") != "check" or str(finding.get("id")) in seen:
            continue
        out.append(dict(finding))
    return out


#: The roster's look-again line for an item with views to look in (the small AI question, on).
def look_in_line(finding: Mapping[str, Any]) -> str | None:
    """`look in: the section (page 20), ...` for the look-again, when the question is on and this
    step found views; else `None`."""
    record = finding.get(FIELD)
    views = record.get("look_in") if isinstance(record, Mapping) else None
    if not views:
        return None
    return "look for the splash size in: " + "; ".join(str(v) for v in views)
