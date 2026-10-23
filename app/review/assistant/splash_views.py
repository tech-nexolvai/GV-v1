"""Splash sizes on the reference's other views: code, $0, no AI (the owner, 2026-10-23).

Anant: "check elevation and section drawings for splash details when they are absent from
architectural plans". A vendor's splash size is often compared with the one architect view it was
paired with (a plan), which prints no splash size, while the same sheet's section, or the item's
other sheet, does. This step runs after code's other checks, on the vendor splash items that ended
"the architect gives no size", and looks for the size on the item's other views, in this order:

1. **The same sheet**: the other views of the architect sheet the vendor drawing was paired with
   (`AcrossCandidate.views`: the views code found, or the set index's checked ones), when the
   view's title names the same item or only its kind (`ELEVATION`, `SECTION`: the sheet's item).
2. **The same item elsewhere** in the project: the one other architect sheet whose checked item
   name equals this one's, or that prints the vendor's own item tag. Two such sheets: no look.
3. **The architect's pointers**: a section or detail callout (`views.callouts`) printed on those
   views, followed only to a sheet whose number is exactly the callout's and a view numbered
   exactly so (every tool links only an exact match; nothing is guessed).

Never a brand prototype sheet (R1: the architect governs; a prototype splash is what the updated
design removed), a key plan, another project's sheet, or an older copy of a sheet. The views the
pack's `look_in_views` names are searched (countertop: plan, elevation, section).

**What counts as printed:** the architect's own words naming the splash with a size beside them,
read from the drawing's text (`4" BACKSPLASH`, `BACKSPLASH 3/4" THK`), one measure each (a size
whose measure the words do not say, as in `3/4" X 4" SPLASH`, is not used). Dimension labels inside
a found view are counted, never tied to the splash by code (which one measures the splash is not
known without a reading of the picture: the small AI question, `assistant_splash_question`, off by
default, then only points the existing look-again at the found view).

**Like for like** (#1909): a back splash is never compared with a side splash, a height never with
a thickness or a length, and a splash's size never with an architect size code does not see named
as the splash's (`unlike`: the meeting set's "back splash length 36"" against the plan's top width
36" was a false Right).

**Outcomes** (the item keeps its spot; code never guesses a value):

- printed the same: Right (both values read by code, the architect's from its words);
- printed differently: a difference that needs the usual pairing proof (the blind look and code's
  ends, `proof.pairing_proved`) before it can be red; otherwise Not compared;
- two found views print it differently: GV asks the architect (the #1907 pattern), and the vendor
  is compared with the governing view (a section over an elevation over a plan, settled);
- a thickness no view prints: the job's backsplash thickness, labelled "job value" (the owner's
  default, 2026-10-23): the same is Right, a different one a Likely problem, never red;
- nothing printed: Not compared, "the architect shows no splash size", naming every view looked
  at, and saying when a view prints its numbers in pen strokes code cannot read yet. A drawn
  splash with no size is left to the measuring by scale (`scale_measure`), which runs next.

The whole-drawing look's "the architect draws no splash" (a Likely problem) is not kept when a
found view, or the architect's page, holds numbers in pen strokes code cannot read: code cannot
confirm the absence (the meeting set's sections draw the splash in strokes), so the item is Not
compared with that reason.

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
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from app.review.assistant import likely_problems
from app.review.assistant.finding_checks import claim_value, dimension_tokens, to_inches

__all__ = [
    "CREATED_BY",
    "FIELD",
    "METHOD",
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
    "part_of",
    "settle",
    "subject_of",
    "unlike",
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
    r"\b(?:to|from|of|in|on|at|above|below|under|over|behind|into|beside|near)"
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
    """Why two parts are not like for like, or `None`."""
    if a.kind and b.kind and a.kind != b.kind:
        return f"a {a.kind} {a.part} against a {b.kind} {b.part}"
    if a.measure and b.measure and a.measure != b.measure:
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


def unlike(
    finding: Mapping[str, Any],
    phrases: Phrases | None = None,
    rules: Sequence[Rule] | None = None,
) -> str | None:
    """Why a splash item's two sides are not like for like, or `None` (alike, or not a splash
    item). The vendor's side names the part (the item's words); the architect's side must too: its
    own value's words, or a printed phrase within `NEAR_PT` of its label (`phrases`: the pages'
    `dims.json` phrases by (file, page)). Then the kind (back, side) and the measure (height,
    thickness, length) must not differ."""
    rules = _rules() if rules is None else rules
    vendor = _vendor_part(finding, rules)
    if vendor is None:
        return None
    architect = part_of(finding.get("architect"), rules)
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    label = (dims or {}).get("architect")
    if architect is None and isinstance(label, Mapping):
        architect = part_of(label.get("text"), rules)
    if architect is None and phrases is not None:
        spot = _architect_spot(finding)
        if spot is not None:
            file, page, box = spot
            near = [
                part
                for phrase in phrases.get((file, page), ())
                if (at := _box(phrase.get("box"))) is not None and _gap(at, box) <= NEAR_PT
                if (part := part_of(phrase.get("text"), rules)) is not None
            ]
            if near:
                architect = near[0]
    if architect is None:
        return (
            f"the architect's size is not shown to be the {vendor.part}'s (it may be another "
            "part's size, such as the top's own)"
        )
    return _clash(vendor, architect)


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
    why = unlike(finding, phrases)
    if why is None:
        return None
    out = dict(finding)
    out["status"] = "check"
    out["keep_reason"] = "measures_something_else"
    out.pop("settled", None)
    out[FIELD] = not_alike(why)
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
            if boxes:
                unread[(str(entry.get("file")), int(entry["page"]))] = boxes
        index = import_module("app.review.assistant.set_index").load(folder)
        page_map = _read(folder / "page_map.json")
        roles: dict[tuple[str, int], str] = {}
        for entry in (page_map or {}).get("pages") or [] if isinstance(page_map, Mapping) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                roles[(str(entry.get("file")), int(entry["page"]))] = str(entry.get("role") or "")
        return cls(
            phrases=phrases,
            unread=unread,
            index=index,
            roles=roles,
            older=_older(folder, page_map, index),
        )

    def index_of(self, file: str, page: int) -> Mapping[str, Any]:
        return self.index.get(f"{file}#{page}") or {}


def _older(folder: Path, page_map: Any, index: Any) -> frozenset[tuple[str, int]]:
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
    except Exception:  # noqa: BLE001 - no older copy known: none left out
        _log.warning("splash views: older copies not worked out", exc_info=True)
        return frozenset()


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


def _item_words(text: object) -> set[str]:
    return {w for w in _TITLE_WORD.findall(str(text or "").upper()) if w not in _KIND_WORDS}


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


def _names_item(drawing: Any, item: set[str]) -> bool:
    """A view's title names the item (a rare word in common), or only its kind (the sheet's own
    item: `ELEVATION`), or the item's words are not known."""
    titles = list(getattr(drawing, "titles", ()) or ())
    if not titles or not item:
        return True
    return any(not _item_words(t) or _item_words(t) & item for t in titles)


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


def _pair_of(work: Any, finding: Mapping[str, Any]) -> Any | None:
    page = finding.get("page")
    for pair in getattr(work, "across", ()) or ():
        vendor = pair.vendor
        if str(vendor.file) == str(getattr(work, "file", vendor.file)) and (
            vendor.own_page == page
        ):
            return pair
    return None


def _paired_page(finding: Mapping[str, Any], pair: Any) -> int | None:
    """The architect page the item was compared with: its own `vs_page` (or its spot's page on
    the architect's file), else the one page code paired the vendor drawing with."""
    spot = finding.get("arch_box")
    pages = {int(c.page) for c in pair.candidates if c.id in pair.chosen}
    files = {str(c.file) for c in pair.candidates}
    if (
        isinstance(spot, Mapping)
        and isinstance(spot.get("page"), int)
        and str(spot.get("file") or "") in files
    ):
        return int(spot["page"])
    vs = finding.get("vs_page")
    if isinstance(vs, int) and not isinstance(vs, bool):
        return vs
    return next(iter(pages)) if len(pages) == 1 else None


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

    @property
    def found(self) -> bool:
        return bool(self.views or self.notes)


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
    page = _paired_page(finding, pair)
    if page is None:
        return Search()
    sheet = next((c for c in pair.candidates if int(c.page) == page), None)
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

    # 1. The same sheet: its views that name the item (or only their kind).
    for drawing in _views_of(sheet):
        if _names_item(drawing, item):
            add(drawing, "the same sheet")
    # 2. The same item elsewhere in the project: exactly one other sheet.
    project = getattr(sheet, "project", None)
    name = _key(sheet_index.get("item"))
    tags = {_key(t) for t in vendor_index.get("item_tags") or [] if len(_key(t)) >= 4}
    others = []
    for candidate in pair.candidates:
        if int(candidate.page) == page or not _usable(candidate, facts):
            continue
        if project and getattr(candidate, "project", None) not in (None, project):
            continue
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
    for target in _callout_targets(pair, found, facts):
        add(target, "a callout")
    pages = [page, *(v.page for v in found)]
    return Search(tuple(found), tuple(notes), _stray(facts, str(sheet.file), pages, found))


def _item_name(finding: Mapping[str, Any]) -> str:
    """The item's name without the part's own words (`Vanity: back splash height` -> `Vanity`)."""
    item = str(finding.get("item") or "")
    return item.split(":", 1)[0] if ":" in item else ""


def _callout_targets(pair: Any, found: Sequence[Companion], facts: Facts) -> list[Any]:
    views = import_module("app.review.assistant.simple_check.views")
    numbers: dict[tuple[str, int], tuple[str | None, list[Any]]] = {}
    for candidate in pair.candidates:
        if not _usable(candidate, facts):
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


def note_readings(
    view: Companion, phrases: Sequence[Mapping[str, Any]], rules: Sequence[Rule]
) -> list[Reading]:
    """The part's sizes the architect's words print inside `view` (see the module): a phrase
    naming the part with one size whose measure its words say, or one size printed right before
    the part's name (`4" BACKSPLASH`: a height)."""
    out: list[Reading] = []
    for phrase in phrases:
        box = _box(phrase.get("box"))
        text = " ".join(str(phrase.get("text") or "").split())
        if box is None or not text or not _inside(box, view.box):
            continue
        part = part_of(text, rules)
        if part is None:
            continue
        sizes = _sizes(text)
        if not sizes:
            continue
        if len(sizes) == 1:
            token, _a, end = sizes[0]
            measure = part.measure or _measure_after(text, end, len(text), _rule_of(part, rules))
            value = to_inches(token)
            if measure is not None and value is not None and value > 0:
                out.append(
                    Reading(Fraction(value), text, Part(part.part, part.kind, measure), view, box)
                )
            continue
        # Several sizes: each one only with its own measure word right after it.
        for n, (token, _a, end) in enumerate(sizes):
            upto = sizes[n + 1][1] if n + 1 < len(sizes) else len(text)
            words = _measures(text[end:upto][:12])
            value = to_inches(token)
            if len(words) == 1 and value is not None and value > 0:
                out.append(
                    Reading(Fraction(value), text, Part(part.part, part.kind, words[0]), view, box)
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
    unread = sum(v.unread for v in views) + sum(search.stray.values())
    absence = _absence_claim(finding)
    if absence and not readings and not unread:
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
        out.pop(likely_problems.FIELD, None)
        out["architect"] = "not confirmed by code"
        record["absence_not_confirmed"] = True
    if part.measure is None:
        record["why"] = (
            f"the architect shows no {part.part} size code can compare: the vendor's value is "
            f"not named as the {part.part}'s height, thickness or length (looked at: {looked})"
            + _unread_words(views, search.stray)
        )
        return _not_compared(out, record, views, question)
    governing = _governing(readings)
    values = {r.value for r in governing}
    added: list[dict[str, Any]] = []
    disagree = {r.value for r in readings} - values
    if governing and disagree:
        other = next(r for r in readings if r.value in disagree)
        added.append(_ask_architect(finding, governing[0], other, part))
        record["views_disagree"] = True
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
    if part.measure == "thickness" and job and job.get("thickness") is not None:
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
    else:
        # A difference code read: red only with the pairing proof (the blind look and code's
        # ends, `proof.pairing_proved`), never on this step alone.
        out["status"] = "different"
        out.pop("paired_by", None)
        out["why"] = f"{item}: {how}: they differ."
    record["outcome"] = out["status"]
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
            f"The architect's own drawings disagree: its {other.view.words} shows {other.text}, "
            f"its {governing.view.words} shows {governing.text}. GV compares the {part.part} with "
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
    if found is None:
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
    counts = {"looked": 0, "right": 0, "different": 0, "asked": 0, "not_compared": 0, "unlike": 0}
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
        key = {"same": "right", "different": "different"}.get(str(status), "not_compared")
        counts[key] += 1
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


#: The roster's look-again line for an item with views to look in (the small AI question, on).
def look_in_line(finding: Mapping[str, Any]) -> str | None:
    """`look in: the section (page 20), ...` for the look-again, when the question is on and this
    step found views; else `None`."""
    record = finding.get(FIELD)
    views = record.get("look_in") if isinstance(record, Mapping) else None
    if not views:
        return None
    return "look for the splash size in: " + "; ".join(str(v) for v in views)
