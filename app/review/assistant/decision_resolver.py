"""Fewer decisions (#1554): code settles the "Your decision" items the drawings can answer.

The owner, on a 60-page full check that left 60 items for the reviewer: "60 decisions from you is a
lot. If there is no drawing to compare, that makes sense, but clear the others." Most of those 60
were not real questions: the same item on its round 1 and round 2 pages, fillers that GV's own
filler limits already answer, vendor strings code can add up, a value a newer round already
fixed, a brand prototype that prints the vendor's value, tag lists where the architect only tags
other trades' finishes.

This pass runs once, after the helpers and the lead have answered and code's checks of the findings
have run (a full check or a whole-set answer only), before the trust report, the cloud drawing and
the report. It takes every finding whose status is `check` ("Your decision") and, in this order:

1. **Same item** on several pages or rounds (the same area of the set, the same item, or the same
   main vendor value of the same kind of size): one item, listing every page (`also_on`). When the
   other copy is Wrong or Right, that one carries the item (a newer round that matches = "round 2
   fixed it").
2. **Code's own rules**, each with its evidence:
   - tags where the architect only tags other trades' finishes (tile, paint, wall covering): not a
     difference; a naming-only tag difference is Right only when a legend on the sheets prints both
     codes on one line, else it stays;
   - outside countertop and cabinet scope (light fixtures): not a difference;
   - GV's limits in force for the set (the settings table of the exported `rules.md`): the filler
     limits make a filler Wrong or Right;
   - the vendor's own string (parts and total): Wrong when code's sum is off by a slip (up to 1"); a
     bigger gap stays, because the parts are probably not the whole string;
   - printed against printed: both values read at their spots by `verify_value` (code's $0
     witness), the same kind of size, not scaled, nothing saying they measure different things:
     Wrong, or Right within the drawing's precision (1/16");
   - the brand prototype prints the vendor's value, the lead says it matches, and code reads both
     values at their spots: Right.

   **No Right on the AI's word alone** (security audit 2026-10-18): every Right needs code's
   witness (both values printed at their spots, the vendor's sum, GV's rule, or the drawings' own
   legend line); without it the item stays "Your decision". A newer round's value quoted by the AI
   is not printed at the item's spot, so it no longer settles an item by itself, and a decision
   never joins a Right item that only the AI calls right.
3. **A sharper read** where the only thing missing is the vendor's number (under GV's markup,
   uncertain, cut off): one Sonnet read at low effort on a crop without GV's markup proposes the
   value, and code compares only after `verify_value` confirms it is printed there. Capped
   (`RESOLVER_READ_LIMIT` reads, `RESOLVER_CAP_MICROS` per answer), counted in the message's cost.
4. Everything left stays "Your decision" with ONE question written by code from the item: what is
   missing (the architect prints nothing, the two sizes measure different things, which drawing
   governs, the number cannot be read, a job value is missing, a layout to compare by eye).

Nothing is dropped: a settled finding keeps `settled` (outcome, method, evidence in plain words), a
merged one is listed on the item it joined (`also_on`) and in the answer's `resolver` block, which
the screen shows as a folded "Settled automatically (n)" group and the cloud drawing and the
report list. Code decides every outcome; a model only reads a value.

`resolve` is pure (its tools are passed in); `resolve_answer(engine)` is the engine's hook.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from time import monotonic
from typing import Any, Final, Literal

from app.review.assistant.classification import internal, missing_on_vendor
from app.review.assistant.finding_checks import (
    arithmetic_off_by,
    claim_value,
    dimension_tokens,
    to_inches,
)
from app.review.assistant.witness import disagreement, witnesses

__all__ = [
    "MILLWORK_FAMILIES",
    "OTHER_TRADE_FAMILIES",
    "PRECISION",
    "RESOLVER_CAP_MICROS",
    "RESOLVER_READ_LIMIT",
    "Resolution",
    "ResolverContext",
    "SharperRead",
    "page_areas",
    "recount_words",
    "resolve",
    "resolve_answer",
    "settings_in_force",
]

_log = logging.getLogger(__name__)

type Outcome = Literal["wrong", "right", "merged", "not_a_difference"]
type KeepReason = Literal[
    "architect_prints_nothing",
    "measures_something_else",
    "which_governs",
    "unreadable",
    "needs_job_value",
    "layout",
    "vendor_disagrees",
    "outside_gv_rule",
    "other",
]

#: Two printed values this close are the same: the smallest step GV's drawings print.
PRECISION: Final = Decimal("0.0625")
#: A vendor string whose parts miss its total by more than this is probably not one whole string
#: (interior widths under an overall, a missing part): a person looks instead of a Wrong.
SLIP_LIMIT: Final = Decimal(1)
#: Values below this never make two items "the same item" (3/4" tops and 4" splashes are on
#: every sheet).
SAME_VALUE_MIN: Final = Decimal(6)
#: The sharper reads one answer's resolver may make, and the money they may use.
RESOLVER_READ_LIMIT: Final = 8
RESOLVER_CAP_MICROS: Final = 500_000
#: How long code's witness (`verify_value`, a few seconds a value on drawn ink) may take in all.
VERIFY_BUDGET_S: Final = 90.0

OUTCOME_WORDS: Final[Mapping[str, str]] = {
    "wrong": "Wrong",
    "right": "Right",
    "merged": "Same item",
    "not_a_difference": "Not a difference",
}

# ---- reading the finding's words ---------------------------------------------------------------

_SCALED: Final = re.compile(r"\b(about|approx\w*|scal\w*|roughly|estimated)\b|~|≈", re.IGNORECASE)
#: The lead's own words that the two sizes do not measure the same thing.
_OTHER_MEASURE: Final = re.compile(
    r"measures? (?:something|a different|another)|different (?:points|extent)|not the (?:width|"
    r"overall|height|depth)|to the top of|includes?\b|including\b|band\b|splash|soffit|bulkhead|"
    r"box above|half[- ]width|centre ?line|center ?line|\bCL\b|locates?\b|confirm what",
    re.IGNORECASE,
)
_GV_MARKUP: Final = re.compile(
    r"GV'?s? (?:yellow|markup|cloud|box|note|ink)|yellow[- ]box|GV[- ]markup|red ink|coloured ink"
    r"|(?:noted|marked|written) by GV",
    re.IGNORECASE,
)
_UNREADABLE: Final = re.compile(
    r"unreadable|not readable|not legible|illegible|uncertain|unclear|cut off|tiny print|hidden|"
    r"covered by|under GV|too small",
    re.IGNORECASE,
)
_NOTHING_PRINTED: Final = re.compile(
    r"\bno\b[^.;]*\b(?:printed|shown|given|dimension|dimensions|sizes?|widths?|depths?|heights?)\b"
    r"|not printed|prints no|nothing to compare|n/a",
    re.IGNORECASE,
)
_LAYOUT: Final = re.compile(
    r"\blayouts?\b|\bdoors?\b|\bdrawers?\b|\bbays?\b|\bcubb|\bshel(?:f|ves)\b|\blockers?\b|"
    r"\bbins?\b|\bcounts?\b|\bpulls?\b|\bblocking\b|\bpanels?\b",
    re.IGNORECASE,
)
_JOB_VALUE: Final = re.compile(
    r"settings?|job values?|project values?|needs? (?:the )?counter", re.IGNORECASE
)
_OUT_OF_SCOPE: Final = re.compile(
    r"not (?:countertop|cabinet|millwork)[^.;]*scope|light fixtures?|pendants?|lighting",
    re.IGNORECASE,
)
_TAG_WORD: Final = re.compile(r"\btags?\b|finish(?:es)?\b", re.IGNORECASE)
_TAG_CODE: Final = re.compile(r"\b([A-Z]{1,4})-?(\d{1,4})(?:-[A-Z]{2,6})?\b")
#: Tag families of other trades (tile, paint, wall covering, tackboard, flooring, ceilings).
_OTHER_TRADES: Final = frozenset(
    {"TW", "TL", "WT", "PT", "PTW", "P", "V", "VWC", "WC", "TB", "CPT", "LVT", "RB", "ACT", "ACP"}
)
#: The same families for other code (`architect_items` leaves these tags out).
OTHER_TRADE_FAMILIES: Final = _OTHER_TRADES
#: Tag families of the millwork GV reviews (stone, laminate, wood, solid surface, quartz).
_MILLWORK: Final = frozenset({"ST", "LA", "WD", "PL", "PLAM", "SS", "QZ", "CT", "MW", "L", "WV"})
#: The same families for other code (`one_card` matches two copies of a finish item by them).
MILLWORK_FAMILIES: Final = _MILLWORK
_ROUND_ONE: Final = re.compile(r"\(\s*round\s*1\b[^)]*\)|round\s*1\s*:[^;)]*", re.IGNORECASE)
_SCALES_ABOUT: Final = re.compile(
    r"scal\w*\s+(?:about\s+|~\s*)?(\d+(?:\s+\d+/\d+)?\s*\"?)", re.IGNORECASE
)
_CONFIRM: Final = re.compile(r"confirm (whether|which|if|what)\b([^.;]+)", re.IGNORECASE)
_FILLER: Final = re.compile(r"\bfillers?\b", re.IGNORECASE)
#: An item about a run's overall or its parts that mentions its fillers is not a filler item (the
#: paid run of 2026-10-19 read "overalls 118\" (with fillers)" as a 118" filler, #1652).
_RUN_ITEM: Final = re.compile(r"\boveralls?\b|\bsegments?\b|\bstrings?\b", re.IGNORECASE)
_MEASURE_WORDS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("width", re.compile(r"\b(?:widths?|wide|runs?|lengths?)\b", re.IGNORECASE)),
    ("height", re.compile(r"\b(?:heights?|high|tall)\b", re.IGNORECASE)),
    ("depth", re.compile(r"\b(?:depths?|deep)\b", re.IGNORECASE)),
)
_LONGEST_SHORT: Final = 70


def _words(finding: Mapping[str, Any], *keys: str) -> str:
    return " ".join(str(finding.get(key) or "") for key in keys)


def _short(text: object, limit: int = _LONGEST_SHORT) -> str:
    """`text` on one line, cut at a word boundary."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0].rstrip(",;:(")
    return f"{cut}…"


def _thing(item: object) -> str:
    """The item as words inside a sentence: "the bussing station overall width"."""
    flat = " ".join(str(item or "this item").split())
    return f"the {flat[:1].lower()}{flat[1:]}" if flat else "this item"


def _inches_words(value: Decimal) -> str:
    whole = int(value)
    rest = value - whole
    if rest == 0:
        return f'{whole}"'
    sixteenths = int(rest * 16)
    numerator, denominator = sixteenths, 16
    while numerator % 2 == 0 and denominator > 1:
        numerator //= 2
        denominator //= 2
    fraction = f"{numerator}/{denominator}"
    return f'{whole} {fraction}"' if whole else f'{fraction}"'


def _measure(finding: Mapping[str, Any]) -> str | None:
    text = str(finding.get("item") or "")
    for name, pattern in _MEASURE_WORDS:
        if pattern.search(text):
            return name
    return None


def _main_value(text: object) -> Decimal | None:
    """The inches of the value a side's words are about (the whole text, else its first size)."""
    value = claim_value(text)
    return to_inches(value) if value is not None else None


def _leading_value(text: object) -> str | None:
    """The side's value when its words START with it (`61 1/2" (tiny print)` gives `61 1/2"`),
    else `None`: a sentence that only mentions a size somewhere is not a claim of that size."""
    if not isinstance(text, str):
        return None
    tokens = dimension_tokens(text)
    if tokens:
        first = tokens[0]
        start = text.find(first.split()[0])
        return first if 0 <= start <= 2 else None
    return text if to_inches(text) is not None else None


def _codes(text: object) -> list[tuple[str, str]]:
    """The finish tags written in `text`: (family, the tag as written)."""
    found: list[tuple[str, str]] = []
    for match in _TAG_CODE.finditer(str(text or "")):
        found.append((match.group(1), match.group(0)))
    return found


# ---- the set's areas and settings --------------------------------------------------------------


def page_areas(page_map: Mapping[str, Any] | None, main_file: str | None) -> dict[int, str]:
    """Each page of the vendor's file and the area it belongs to (the set pack's page map): an
    area's round 2 and prototype pages share its key."""
    areas: dict[int, str] = {}
    scope = import_module("app.review.assistant.chat_scope")
    for key, (_title, pages) in scope.area_pages(page_map, main_file).items():
        for page in pages:
            areas[int(page)] = str(key)
    return areas


_SETTING_ROW: Final = re.compile(r"^\|[^|]*\(`(?P<key>[a-z_]+)`\)\s*\|\s*(?P<value>[^|]+?)\s*\|")


def settings_in_force(rules_md: str | None) -> dict[str, str]:
    """The settings in force for the set, from the exported `rules.md` table: key -> value as
    written (`filler_max` -> `2"`). A setting nobody entered (`not set`) is left out."""
    found: dict[str, str] = {}
    for line in (rules_md or "").splitlines():
        match = _SETTING_ROW.match(line.strip())
        if match is None:
            continue
        value = match.group("value").strip()
        if value.lower().startswith("not set"):
            continue
        found[match.group("key")] = value
    return found


# ---- the context and the result ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SharperRead:
    """One sharper read of a finding's spots: what the reader saw, or `None` for a side."""

    architect: str | None
    vendor: str | None
    sure: bool


type Verify = Callable[[Mapping[str, Any], str], str | Mapping[str, Any]]
"""(box, value) -> `match` / `mismatch` / `cannot_read`, or `verify_value`'s whole answer (its
`result`, `route` text / drawn, ...): code's $0 witness."""
type Read = Callable[[Mapping[str, Any]], SharperRead | None]
"""A finding -> its sharper read (a model call within the cap), `None` when none could be made."""
type Legend = Callable[[Sequence[int], Sequence[str]], str | None]
"""(pages, tags) -> a line of the drawings' own text that names all the tags, else `None`."""


@dataclass(slots=True)
class ResolverContext:
    """What the resolver may use. Every tool is optional: without it, its rule does not run."""

    areas: Mapping[int, str] = field(default_factory=dict)
    settings: Mapping[str, str] = field(default_factory=dict)
    verify: Verify | None = None
    read: Read | None = None
    legend: Legend | None = None
    read_limit: int = RESOLVER_READ_LIMIT
    reads: int = 0


@dataclass(slots=True)
class Resolution:
    """The resolver's answer: the findings it changed, the ones it folded into another (id ->
    reason, for the `finding_dropped` block) and the `resolver` block's content."""

    changed: list[dict[str, Any]]
    absorbed: dict[str, str]
    block: dict[str, Any]


def _counts(findings: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    statuses = [str(f.get("status")) for f in findings]
    return {
        "wrong": statuses.count("different"),
        "decision": statuses.count("check"),
        "right": statuses.count("same"),
    }


def _round(finding: Mapping[str, Any]) -> int:
    value = finding.get("round")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 1


def _page(finding: Mapping[str, Any]) -> int:
    value = finding.get("page")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _area(finding: Mapping[str, Any], areas: Mapping[int, str]) -> str:
    """The finding's area of the set; without a page map, its page pair stands in."""
    for key in ("page", "vs_page"):
        page = finding.get(key)
        if isinstance(page, int) and page in areas:
            return areas[page]
    pages = sorted(p for p in (finding.get("page"), finding.get("vs_page")) if isinstance(p, int))
    return f"pages {pages[0]}" if pages else "?"


def _linked(a: Mapping[str, Any], b: Mapping[str, Any], areas: Mapping[int, str]) -> bool:
    if areas:
        return _area(a, areas) == _area(b, areas)
    pages_a = {p for p in (a.get("page"), a.get("vs_page")) if isinstance(p, int)}
    pages_b = {p for p in (b.get("page"), b.get("vs_page")) if isinstance(p, int)}
    return bool(pages_a & pages_b)


def _item_key(finding: Mapping[str, Any]) -> str:
    text = str(finding.get("item") or "").lower()
    text = re.sub(r"\b(?:round|rev(?:ision)?)\s*\d\b", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _status_words(finding: Mapping[str, Any]) -> str:
    return {"different": "Wrong", "same": "Right", "check": "Your decision"}.get(
        str(finding.get("status")), str(finding.get("status"))
    )


def _pages_words(finding: Mapping[str, Any]) -> str:
    page = finding.get("page")
    vs_page = finding.get("vs_page")
    if isinstance(vs_page, int) and vs_page != page:
        return f"p.{page} (architect p.{vs_page})"
    return f"p.{page}"


# ---- the rules -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Settle:
    outcome: Outcome
    method: str
    how: str
    checks: Mapping[str, Any] = field(default_factory=dict)
    """The witness code found, recorded on the finding's checks so its trust follows the rule in
    `witness.py`: printed (both values read at their spots), arithmetic, or GV's rule."""


def _other_trades_tags(finding: Mapping[str, Any]) -> _Settle | None:
    """A tag-only item where the architect tags only other trades' finishes."""
    item = str(finding.get("item") or "")
    if not _TAG_WORD.search(item) or _LAYOUT.search(item):
        return None
    architect = _codes(finding.get("architect"))
    vendor = _codes(_ROUND_ONE.sub(" ", str(finding.get("vendor") or "")))
    if not architect or not vendor:
        return None
    if dimension_tokens(finding.get("architect")):
        return None  # the architect side also gives a size: more than tags
    if any(family not in _OTHER_TRADES for family, _tag in architect):
        return None
    millwork = [tag for family, tag in vendor if family in _MILLWORK]
    if not millwork or any(family in _OTHER_TRADES for family, _tag in vendor):
        return None
    theirs = ", ".join(dict.fromkeys(tag for _family, tag in architect))
    ours = ", ".join(dict.fromkeys(millwork))
    return _Settle(
        "not_a_difference",
        "other_trades_tags",
        f"the architect tags only other trades' finishes here ({theirs}: tile, paint or wall "
        f"covering), so the vendor's millwork tags ({ours}) have nothing to differ from.",
    )


def _out_of_scope(finding: Mapping[str, Any]) -> _Settle | None:
    if finding.get("category") != "other":
        return None
    text = _words(finding, "item", "why")
    if not _OUT_OF_SCOPE.search(text) or _FILLER.search(text):
        return None
    return _Settle(
        "not_a_difference",
        "out_of_scope",
        "light fixtures are not countertop or cabinet work, so GV does not review them.",
    )


def _limit(settings: Mapping[str, str], key: str) -> Decimal | None:
    value = settings.get(key)
    return to_inches(value) if value is not None else None


def _filler_limits(
    finding: Mapping[str, Any], settings: Mapping[str, str]
) -> _Settle | KeepReason | None:
    """GV's filler limits in force for the set (rules.md) against the vendor's filler widths.

    Never red (DECIDED 2026-10-20, as `filler_range`): GV's usual range is a note, so a filler
    outside it is kept as a GV note (`outside_gv_rule`) for the later rules (the architect's own
    print may still settle it); a filler goes red only by the architect or GV's site-fit rule
    (`run_fit`)."""
    if not _FILLER.search(str(finding.get("item") or "")):
        return None  # a filler mentioned beside a run's width is not a filler item
    if _RUN_ITEM.search(str(finding.get("item") or "")):
        return None  # a run's overall or parts "with fillers" is not a filler width
    low, high = _limit(settings, "filler_min"), _limit(settings, "filler_max")
    if low is None or high is None:
        return None
    vendor = _ROUND_ONE.sub(" ", str(finding.get("vendor") or ""))
    if _GV_MARKUP.search(vendor):
        return None  # a value of GV's own markup is never the vendor's
    widths = [w for w in (to_inches(t) for t in dimension_tokens(vendor)) if w is not None]
    if not widths:
        return None
    outside = sorted({w for w in widths if w < low or w > high})
    limits = f"{_inches_words(low)} to {_inches_words(high)}"
    where = "rules.md, the settings in force for this set"
    if outside:
        return "outside_gv_rule"  # GV's usual range is a note, never a red
    listed = ", ".join(_inches_words(w) for w in sorted(set(widths)))
    return _Settle(
        "right",
        "gv_limits",
        f"GV's filler limits are {limits} ({where}); the vendor's fillers ({listed}) are within.",
        {"rule": "agrees"},
    )


def _vendor_sum(finding: Mapping[str, Any]) -> _Settle | KeepReason | None:
    if finding.get("source") != "vendor_arithmetic":
        return None
    parts, total = finding.get("parts"), finding.get("printed_total")
    if not isinstance(parts, list) or not isinstance(total, str) or not parts:
        return None
    off = arithmetic_off_by([str(p) for p in parts], total)
    if off is None:
        return None
    printed = to_inches(total)
    if printed is None:
        return None
    summed = printed - off
    if off == 0:
        return _Settle(
            "right",
            "vendor_sum",
            f"code adds the vendor's parts to {_inches_words(summed)}, the printed total.",
            {"arithmetic": "adds_up"},
        )
    if abs(off) <= SLIP_LIMIT:
        return _Settle(
            "wrong",
            "vendor_sum",
            f"code adds the vendor's parts to {_inches_words(summed)}; the vendor prints "
            f"{_inches_words(printed)} ({_inches_words(abs(off))} apart).",
            {"arithmetic": "does_not_add_up"},
        )
    return "vendor_disagrees"


def _witness(
    finding: Mapping[str, Any], side: str, value: str, ctx: ResolverContext
) -> dict[str, Any] | None:
    """`value` is printed at the finding's spot on `side`: the check fields that record it (empty
    when the finding's checks already do), else `None`. Code's own checks first (the text route,
    the drawn-ink route), else `verify_value` now. A text read is `printed_<side>: match`; a read of
    the vendor's drawn ink is kept as `verify_<side>` (`witness.py`: OCR confirms only with a
    second independent read)."""
    checks = finding.get("checks") or {}
    key = "architect" if side == "architect" else "vendor"
    wanted = to_inches(value)
    if checks.get(f"printed_{key}") == "match":
        return {}
    verified = checks.get(f"verify_{key}")
    if isinstance(verified, Mapping) and verified.get("result") == "match":
        claimed = to_inches(str(verified.get("value") or ""))
        if claimed is not None and claimed == wanted:
            return {}
    box = finding.get("arch_box" if side == "architect" else "vendor_box")
    if ctx.verify is None or not isinstance(box, Mapping):
        return None
    try:
        found = ctx.verify(box, value)
    except Exception as failed:  # noqa: BLE001 - an unread spot is simply not printed for us
        _log.warning("resolver verify_value failed: %s", type(failed).__name__)
        return None
    if isinstance(found, Mapping):
        if found.get("result") != "match":
            return None
        if found.get("route") == "drawn":
            return {f"verify_{key}": dict(found)}
        return {f"printed_{key}": "match", f"verify_{key}": dict(found)}
    return {f"printed_{key}": "match"} if found == "match" else None


def _printed(finding: Mapping[str, Any], side: str, value: str, ctx: ResolverContext) -> bool:
    return _witness(finding, side, value, ctx) is not None


def _side_value(finding: Mapping[str, Any], side: str) -> str | None:
    inches = finding.get("inches") or {}
    text = str(finding.get(side) or "")
    if _SCALED.search(text) or _GV_MARKUP.search(text):
        return None
    value = _leading_value(text)
    if value is None and inches.get(side) is not None and to_inches(text) is not None:
        value = text
    return value


def _splash_unlike(finding: Mapping[str, Any]) -> bool:
    """A splash item no rule here may settle on equal values (2026-10-23, like for like): the
    splash step's own outcome stands, and a splash's size is never settled against an architect
    size code does not see printed as the same splash's (`splash_views.unlike`)."""
    splash = import_module("app.review.assistant.splash_views")
    record = finding.get(splash.FIELD)
    if isinstance(record, Mapping) and record.get("outcome") in (
        "likely_problem",
        "not_like_for_like",
    ):
        return True
    return splash.unlike(finding) is not None


def _printed_vs_printed(finding: Mapping[str, Any], ctx: ResolverContext) -> _Settle | None:
    if finding.get("source") in ("brand_prototype", "vendor_arithmetic", "rules"):
        return None
    if finding.get("check_reason"):
        return None  # the lead said why two printed values still need a person (#1545)
    if _splash_unlike(finding):
        return None
    if _OTHER_MEASURE.search(_words(finding, "why", "architect", "vendor")):
        return None
    if _LAYOUT.search(str(finding.get("item") or "")) and not _measure(finding):
        return None
    architect, vendor = _side_value(finding, "architect"), _side_value(finding, "vendor")
    if architect is None or vendor is None:
        return None
    a, v = to_inches(architect), to_inches(vendor)
    if a is None or v is None:
        return None
    seen_architect = _witness(finding, "architect", architect, ctx)
    if seen_architect is None:
        return None
    seen_vendor = _witness(finding, "vendor", vendor, ctx)
    if seen_vendor is None:
        return None
    both = f"code reads both values at their spots: architect {architect}, vendor {vendor}"
    seen = {**seen_architect, **seen_vendor}
    if abs(a - v) <= PRECISION:
        return _Settle("right", "printed_vs_printed", f"{both}; the same size.", seen)
    apart = f"{both} ({_inches_words(abs(a - v))} apart)."
    return _Settle("wrong", "printed_vs_printed", apart, seen)


def _prototype_prints_it(finding: Mapping[str, Any], ctx: ResolverContext) -> _Settle | None:
    """The brand prototype prints the vendor's value and the lead says it matches: Right only when
    code also reads both values at their spots (the printed witness). Without that it is the AI's
    word alone, and the item stays (security audit 2026-10-18)."""
    if finding.get("source") != "brand_prototype":
        return None
    if not re.search(r"\bmatch(?:es|ed)?\b", str(finding.get("why") or ""), re.IGNORECASE):
        return None
    vendor = _main_value(finding.get("vendor"))
    if vendor is None:
        return None
    for token in dimension_tokens(finding.get("architect")):
        if to_inches(token) == vendor:
            seen_architect = _witness(finding, "architect", token, ctx)
            vendor_value = claim_value(finding.get("vendor"))
            seen_vendor = (
                _witness(finding, "vendor", vendor_value, ctx) if vendor_value is not None else None
            )
            if seen_architect is None or seen_vendor is None:
                return None
            return _Settle(
                "right",
                "prototype_prints_it",
                f"the brand prototype prints {token}, the vendor's {_inches_words(vendor)}; code "
                "reads both at their spots.",
                {**seen_architect, **seen_vendor},
            )
    return None


def _legend_tags(finding: Mapping[str, Any], ctx: ResolverContext) -> _Settle | KeepReason | None:
    """Both sides tag millwork with different codes: Right when a legend line on the drawings
    names both codes; else a person decides."""
    item = str(finding.get("item") or "")
    if not _TAG_WORD.search(item) or _LAYOUT.search(item):
        return None
    architect = [tag for family, tag in _codes(finding.get("architect")) if family in _MILLWORK]
    vendor = [
        tag
        for family, tag in _codes(_ROUND_ONE.sub(" ", str(finding.get("vendor") or "")))
        if family in _MILLWORK
    ]
    if not architect or not vendor or set(architect) == set(vendor):
        return None
    if ctx.legend is not None:
        pages = [p for p in (finding.get("page"), finding.get("vs_page")) if isinstance(p, int)]
        try:
            line = ctx.legend(pages, [*dict.fromkeys(architect), *dict.fromkeys(vendor)])
        except Exception as failed:  # noqa: BLE001 - no legend read: a person decides
            _log.warning("resolver legend read failed: %s", type(failed).__name__)
            line = None
        if line:
            return _Settle(
                "right",
                "legend",
                f'the drawings\' own legend names both tags on one line ("{_short(line, 90)}").',
            )
    return "layout"


def _sharper_read(finding: Mapping[str, Any], ctx: ResolverContext) -> _Settle | str | None:
    """The vendor's number is the only thing missing: one sharper read proposes it, code's
    witness must confirm it, then code compares. Returns the outcome, or the read's words for the
    question (`str`), or `None` when no read was made."""
    if ctx.read is None or ctx.reads >= ctx.read_limit:
        return None
    if not _UNREADABLE.search(_words(finding, "vendor", "why")) and not _GV_MARKUP.search(
        str(finding.get("vendor") or "")
    ):
        return None
    if _OTHER_MEASURE.search(_words(finding, "why")) or finding.get("source") == "brand_prototype":
        return None
    if not isinstance(finding.get("vendor_box"), Mapping):
        return None
    architect = claim_value(finding.get("architect"))
    if architect is None or _SCALED.search(str(finding.get("architect") or "")):
        return None
    a = to_inches(architect)
    seen_architect = _witness(finding, "architect", architect, ctx) if a is not None else None
    if a is None or seen_architect is None:
        return None
    ctx.reads += 1
    try:
        read = ctx.read(finding)
    except Exception as failed:  # noqa: BLE001 - no read: a person decides
        _log.warning("resolver sharper read failed: %s", type(failed).__name__)
        return None
    if read is None or not read.vendor:
        return None
    v = to_inches(read.vendor)
    if v is None:
        return None
    seen_vendor = _witness(finding, "vendor", read.vendor, ctx)
    if seen_vendor is None:
        return (
            f"a sharper read sees {read.vendor} on the vendor's drawing; code could not confirm it"
        )
    seen = {**seen_architect, **seen_vendor}
    both = (
        f"a sharper read (GV's markup removed) sees {read.vendor} on the vendor's drawing and code "
        f"confirms it is printed there; the architect prints {architect}"
    )
    if abs(a - v) <= PRECISION:
        return _Settle("right", "sharper_read", f"{both}: the same size.", seen)
    apart = f"{both} ({_inches_words(abs(a - v))} apart)."
    return _Settle("wrong", "sharper_read", apart, seen)


# ---- what stays, and its one question ----------------------------------------------------------

_CHECK_REASONS: Final[Mapping[str, KeepReason]] = {
    "different_extent": "measures_something_else",
    "which_governs": "which_governs",
    "pairing_not_certain": "architect_prints_nothing",
    "scaled": "architect_prints_nothing",
    "site_verify": "architect_prints_nothing",
}


_VENDOR_SUMS: Final = re.compile(
    r"\bleaves\b|not add up|doesn't add|does not add|don't add|do not add|not 2 x", re.IGNORECASE
)


def keep_reason(finding: Mapping[str, Any]) -> KeepReason:
    """Why a person must decide this item, from what the finding says (the first that fits)."""
    named = finding.get("check_reason")
    if isinstance(named, str) and named in _CHECK_REASONS:
        return _CHECK_REASONS[named]
    item = str(finding.get("item") or "")
    if finding.get("source") == "brand_prototype" or re.search(
        r"which governs|prototype", _words(finding, "why", "architect"), re.IGNORECASE
    ):
        return "which_governs"
    if finding.get("source") == "vendor_arithmetic" or _VENDOR_SUMS.search(
        str(finding.get("why") or "")
    ):
        return "vendor_disagrees"
    if _JOB_VALUE.search(_words(finding, "why")):
        return "needs_job_value"
    if _LAYOUT.search(item) and _measure(finding) is None:
        return "layout"
    if _NOTHING_PRINTED.search(str(finding.get("architect") or "")) or re.search(
        r"pairing not certain|nothing to compare|no architect",
        _words(finding, "architect", "why"),
        re.IGNORECASE,
    ):
        return "architect_prints_nothing"
    if _OTHER_MEASURE.search(_words(finding, "why", "architect")) and dimension_tokens(
        finding.get("architect")
    ):
        return "measures_something_else"
    if _UNREADABLE.search(_words(finding, "vendor", "why")) or _GV_MARKUP.search(
        str(finding.get("vendor") or "")
    ):
        return "unreadable"
    if _LAYOUT.search(item):
        return "layout"
    return "other"


def _vendor_value_words(finding: Mapping[str, Any]) -> str:
    tokens = dimension_tokens(_ROUND_ONE.sub(" ", str(finding.get("vendor") or "")))
    return tokens[0] if tokens else _short(finding.get("vendor"), 50)


def question_for(
    finding: Mapping[str, Any], reason: KeepReason, read_words: str | None = None
) -> str:
    """The ONE question a person answers for this item, written by code from its own words."""
    help_ = finding.get("decision_help")
    confirm = str(help_.get("confirm") or "").strip() if isinstance(help_, Mapping) else ""
    if confirm.endswith("?"):
        return confirm
    thing = _thing(finding.get("item"))
    architect = _short(finding.get("architect"), 60)
    vendor = _vendor_value_words(finding)
    if reason == "architect_prints_nothing":
        scaled = _SCALES_ABOUT.search(_words(finding, "architect", "why"))
        about = (
            f" (the architect's drawing scales about {scaled.group(1).strip()})" if scaled else ""
        )
        if re.search(r"pairing not certain", _words(finding, "why", "architect"), re.IGNORECASE):
            return (
                f"Which architect drawing shows {thing}? None of them prints its size, so the "
                f"vendor's {vendor} has nothing to be checked against."
            )
        return f"The architect prints no size for {thing}{about}: is the vendor's {vendor} right?"
    if reason == "measures_something_else":
        stated = _CONFIRM.search(str(finding.get("why") or ""))
        if stated is not None:
            return (
                f"Can you confirm {stated.group(1)}{stated.group(2).rstrip()}? (Architect "
                f"{architect}; vendor {_short(finding.get('vendor'), 60)}.)"
            )
        return (
            f"Does the architect's {architect} measure the same thing as the vendor's {vendor} "
            f"for {thing}?"
        )
    if reason == "which_governs":
        return (
            f"Which governs {thing}: the brand prototype ({architect}) or the vendor's drawing "
            f"({_short(finding.get('vendor'), 60)})?"
        )
    if reason == "unreadable":
        seen = f" ({read_words})" if read_words else ""
        return (
            f"What does the vendor print for {thing}? Code cannot read it{seen}; the architect "
            f"shows {architect}."
        )
    if reason == "needs_job_value":
        return f"Which job values apply to {thing}? {_short(finding.get('why'), 90)}"
    if reason == "vendor_disagrees":
        why = _short(finding.get("why"), 90) if finding.get("why") else ""
        parts = finding.get("parts")
        total = finding.get("printed_total")
        if isinstance(parts, list) and isinstance(total, str) and not why:
            off = arithmetic_off_by([str(p) for p in parts], total)
            printed = to_inches(total)
            if off is not None and printed is not None:
                why = (
                    f"code adds its parts to {_inches_words(printed - off)} against the printed "
                    f"{_inches_words(printed)}"
                )
        return f"Do the vendor's own numbers for {thing} belong to one string? {why}".rstrip()
    if reason == "outside_gv_rule":
        return (
            f"The vendor draws {vendor} for {thing}, outside GV's usual filler range (a GV note, "
            f"not an error): is that size what the job needs?"
        )
    if reason == "layout":
        return (
            f"Does the vendor's {_short(finding.get('item'), 50).lower()} match the architect's? "
            f"Architect: {architect}; vendor: {_short(finding.get('vendor'), 60)}."
        )
    why = f" {_short(finding.get('why'), 90)}" if finding.get("why") else ""
    return f"Is the vendor's {vendor} right for {thing}?{why}"


# ---- the pass ------------------------------------------------------------------------------------


def _readable(finding: Mapping[str, Any]) -> bool:
    """The vendor's value is the vendor's own and readable (not GV's markup, not unclear)."""
    vendor = str(finding.get("vendor") or "")
    return not _GV_MARKUP.search(vendor) and not _UNREADABLE.search(vendor)


def _choose_primary(group: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The copy that carries an item: a Wrong or Right one first, else the decision whose vendor
    value is readable and that compares with something, then the one that says why, then the
    newest round (the same order for every copy, so two copies never point at each other). A copy
    with a spot on the drawing comes before one without."""
    settled = [f for f in group if f.get("status") != "check"]
    pool = settled or list(group)
    # A copy for GV's reviewer only never carries an item the vendor may see (#1652).
    pool = [f for f in pool if not internal(f)] or pool

    def rank(f: Mapping[str, Any]) -> tuple[int, int, int, int, int, int, str]:
        compares = 0 if _NOTHING_PRINTED.search(str(f.get("architect") or "")) else 1
        # The copy with a spot on the drawing (its picture) carries the item: a copy with none
        # cannot be shown to anyone (#1650).
        spotted = 1 if f.get("vendor_box") or f.get("arch_box") else 0
        says_why = 1 if f.get("why") else 0
        readable = 1 if _readable(f) else 0
        return (readable, compares, spotted, says_why, _round(f), _page(f), str(f.get("id")))

    return max(pool, key=rank)


def _family(finding: Mapping[str, Any]) -> str:
    """What the item is compared with: the brand prototype, or the architect (and GV's rules)."""
    return "prototype" if finding.get("source") == "brand_prototype" else "architect"


def _may_join(duplicate: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """A decision joins an item compared with the same drawing; an item whose architect prints
    nothing to compare may also join the same size compared with the brand prototype. It never
    joins a Right item that only the AI calls right: that would make it Right on the AI's word."""
    if other.get("status") == "same" and not _code_backs(other):
        return False
    if _family(duplicate) == _family(other):
        return True
    return keep_reason(duplicate) == "architect_prints_nothing"


def _also(finding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": str(finding.get("id")),
        "page": finding.get("page"),
        "vs_page": finding.get("vs_page"),
        "round": finding.get("round"),
        "item": finding.get("item"),
        "vendor": finding.get("vendor"),
        "architect": finding.get("architect"),
        "vendor_box": finding.get("vendor_box"),
        "arch_box": finding.get("arch_box"),
    }


def _merge_how(duplicate: Mapping[str, Any], primary: Mapping[str, Any]) -> str:
    where = _pages_words(primary)
    if primary.get("status") == "same" and _round(primary) > _round(duplicate):
        return (
            f"the same item on {where}: round {_round(primary)} matches (round "
            f"{_round(duplicate)} showed {_short(duplicate.get('vendor'), 40)}), so the vendor "
            "fixed it."
        )
    if primary.get("status") in ("different", "same"):
        return f"the same item as {primary['id']} on {where}, which is {_status_words(primary)}."
    return f"the same item as {primary['id']} on {where}; it is decided there once."


#: Words that say where a thing is, not what it is ("outlet CL height AFF": the thing is
#: "outlet").
_PLACE_WORDS: Final = re.compile(
    r"\b(?:cl|c/l|centre|center|centerline|centreline|height|aff|from|to|above|below|width|"
    r"depth|distance|offset|spacing|left|right|edge|floor|end|top|bottom|location|position)\b",
    re.IGNORECASE,
)


def _missing_thing(finding: Mapping[str, Any]) -> str:
    """What a missing item is, without its area and the words that place it ("outlet")."""
    item = str(finding.get("item") or "")
    item = item.split(":", 1)[1] if ":" in item else item
    found = _PLACE_WORDS.search(item)
    thing = item[: found.start()] if found else item
    return " ".join(re.sub(r"[^a-z0-9]+", " ", thing.lower()).split())


def _place_words(finding: Mapping[str, Any]) -> str:
    """The words of a missing item that place it ("CL height AFF")."""
    item = str(finding.get("item") or "")
    item = item.split(":", 1)[1] if ":" in item else item
    found = _PLACE_WORDS.search(item)
    return " ".join(item[found.start() :].split()) if found else ""


#: An architect's two dimensions that place one missing thing lie this close on the drawing
#: (points between their labels): an outlet's height and its distance from an end (#1671).
_PLACE_PT: Final = 36.0


def _spot(box: object) -> tuple[float, float, float, float] | None:
    """A finding's box (`{x0, y0, x1, y1}`) or a label's (`[x0, y0, x1, y1]`) as a tuple."""
    try:
        if isinstance(box, Mapping):
            x0, y0, x1, y1 = (float(box[key]) for key in ("x0", "y0", "x1", "y1"))
        elif isinstance(box, Sequence) and len(box) == 4:
            x0, y0, x1, y1 = (float(value) for value in box)
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _shared(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """How much of the smaller box the two share (0 to 1)."""
    wide, tall = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    if wide <= 0 or tall <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return wide * tall / smaller


def _architect_label(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    dims = finding.get("dims")
    label = dims.get("architect") if isinstance(dims, Mapping) else None
    return label if isinstance(label, Mapping) and label.get("id") else None


def _places_one_thing(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Code's facts say two missing rows are one thing, whatever their words: the readings point
    at the same spot on the vendor's drawing, and the architect's two dimensions (code's labels,
    on one page) run different ways, one across and one up, close together (an outlet placed by
    its height and its distance from an end)."""
    vendor_a, vendor_b = _spot(a.get("vendor_box")), _spot(b.get("vendor_box"))
    label_a, label_b = _architect_label(a), _architect_label(b)
    if vendor_a is None or vendor_b is None or label_a is None or label_b is None:
        return False
    if _shared(vendor_a, vendor_b) < 0.5:
        return False
    if label_a.get("page") != label_b.get("page") or label_a.get("id") == label_b.get("id"):
        return False
    box_a, box_b = _spot(label_a.get("box")), _spot(label_b.get("box"))
    if box_a is None or box_b is None:
        return False
    across_a = box_a[2] - box_a[0] >= box_a[3] - box_a[1]
    across_b = box_b[2] - box_b[0] >= box_b[3] - box_b[1]
    if across_a == across_b:
        return False
    gap_x = max(box_a[0], box_b[0]) - min(box_a[2], box_b[2])
    gap_y = max(box_a[1], box_b[1]) - min(box_a[3], box_b[3])
    return max(gap_x, gap_y, 0.0) <= _PLACE_PT


def _one_missing_thing(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Two rows of one thing the vendor does not show (#1648, #1671): both missing on the vendor,
    on one page, and the same thing by their words or by code's facts (`_places_one_thing`)."""
    if not (missing_on_vendor(a) and missing_on_vendor(b)) or a.get("page") != b.get("page"):
        return False
    thing = _missing_thing(a)
    return (bool(thing) and thing == _missing_thing(b)) or _places_one_thing(a, b)


def _joined(primary: Mapping[str, Any], others: Sequence[Mapping[str, Any]]) -> list[Any]:
    """The rows among `others` one thing with `primary`, directly or through another of them."""
    reached = [primary]
    left = list(others)
    grew = True
    while grew:
        grew = False
        for other in list(left):
            if any(_one_missing_thing(other, found) for found in reached):
                reached.append(other)
                left.remove(other)
                grew = True
    return reached[1:]


def missing_together(
    primary: Mapping[str, Any], others: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The item that carries one missing thing: named by the thing, with every architect value
    that places it ("1' - 6\" (CL height AFF); 1' - 2\" (CL from left edge))"."""
    values = []
    for finding in [primary, *others]:
        where = _place_words(finding)
        value = " ".join(str(finding.get("architect") or "").split())
        values.append(f"{value} ({where})" if where else value)
    area = (
        str(primary.get("item") or "").split(":", 1)[0] if ":" in str(primary.get("item")) else ""
    )
    thing = _missing_thing(primary)
    inches = primary.get("inches")
    return {
        **primary,
        "item": f"{area}: {thing}" if area else thing,
        "architect": "; ".join(dict.fromkeys(values)),
        # Several values now: none of them alone is the architect's size.
        "inches": {**inches, "architect": None} if isinstance(inches, Mapping) else inches,
    }


def _same_value(d: Mapping[str, Any], p: Mapping[str, Any]) -> bool:
    """`d` and `p` are about one size: the same main vendor value (at least 6") and no clash of
    kind (width against height). An item whose architect prints nothing also matches an item that
    lists the same value among several sizes of the same kind (a prototype's "height, depth ...").
    """
    if keep_reason(d) != "architect_prints_nothing":
        return False  # an item with its own comparison is never folded by its value alone
    dv = _main_value(_ROUND_ONE.sub(" ", str(d.get("vendor") or "")))
    if dv is None or dv < SAME_VALUE_MIN:
        return False
    dm, pm = _measure(d), _measure(p)
    pv = _main_value(_ROUND_ONE.sub(" ", str(p.get("vendor") or "")))
    if pv == dv:
        return dm is None or pm is None or dm == pm
    if dm is None:
        return False
    listed = {to_inches(t) for t in dimension_tokens(p.get("vendor"))}
    kinds = dict(_MEASURE_WORDS)
    return dv in listed and bool(kinds[dm].search(str(p.get("item") or "")))


def _merge(
    findings: list[dict[str, Any]], areas: Mapping[int, str]
) -> tuple[dict[str, tuple[str, str]], dict[str, list[dict[str, Any]]]]:
    """Same items: {duplicate id: (primary id, how)} and {primary id: its `also_on` entries}."""
    absorbed: dict[str, tuple[str, str]] = {}
    also: dict[str, list[dict[str, Any]]] = {}
    by_id = {str(f["id"]): f for f in findings}

    def fold(duplicate: Mapping[str, Any], primary: Mapping[str, Any]) -> None:
        absorbed[str(duplicate["id"])] = (str(primary["id"]), _merge_how(duplicate, primary))
        also.setdefault(str(primary["id"]), []).append(_also(duplicate))

    # 1. The same item name in the same area.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for finding in findings:
        groups.setdefault((_area(finding, areas), _item_key(finding)), []).append(finding)
    for group in groups.values():
        if len(group) < 2:
            continue
        for finding in group:
            if finding.get("status") != "check" or finding.get("record_id"):
                continue  # a recorded difference is never folded away
            others = [o for o in group if o is not finding and _may_join(finding, o)]
            if not others:
                continue
            primary = _choose_primary([finding, *others])
            if primary is not finding:
                fold(finding, primary)
    # 2. The same main value of the same kind of size, in the same area.
    for finding in findings:
        if finding.get("status") != "check" or finding.get("record_id"):
            continue
        if str(finding["id"]) in absorbed:
            continue
        others = [
            o
            for o in findings
            if o is not finding
            and str(o["id"]) not in absorbed
            and _linked(finding, o, areas)
            and _same_value(finding, o)
            and _may_join(finding, o)
        ]
        if not others:
            continue
        primary = _choose_primary([finding, *others])
        if primary is not finding:
            fold(finding, primary)

    # 3. One thing the vendor does not show, reported once per dimension that places it (the
    # architect's outlet at a height and at a distance from the end): one item (#1648). Rows join
    # by their words or by code's facts, never only by how a reading words it (#1671).
    missing: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for finding in findings:
        if finding.get("status") != "check" or finding.get("record_id"):
            continue
        if str(finding["id"]) in absorbed or not missing_on_vendor(finding):
            continue
        missing.setdefault((finding.get("page"), _area(finding, areas)), []).append(finding)
    for group in missing.values():
        left = list(group)
        while left:
            primary = left.pop(0)
            joined = _joined(primary, left)
            for finding in joined:
                left.remove(finding)
                fold(finding, primary)

    # A primary that was itself folded hands its copies on, to the item that carries it.
    def carrier(finding_id: str) -> str:
        while finding_id in absorbed:
            finding_id = absorbed[finding_id][0]
        return finding_id

    for duplicate, (primary_id, _how) in list(absorbed.items()):
        target = carrier(primary_id)
        if target != primary_id:
            absorbed[duplicate] = (target, _merge_how(by_id[duplicate], by_id[target]))
    merged_also: dict[str, list[dict[str, Any]]] = {}
    for primary_id, listed in also.items():
        merged_also.setdefault(carrier(primary_id), []).extend(listed)
    also = merged_also
    return absorbed, also


def _same_question(kept: Sequence[tuple[dict[str, Any], KeepReason]]) -> dict[str, str]:
    """Kept "which governs" items on the same page pair ask one question: {id: primary id}."""
    first: dict[tuple[Any, Any], str] = {}
    folded: dict[str, str] = {}
    for finding, reason in kept:
        if reason != "which_governs":
            continue
        key = (finding.get("page"), finding.get("vs_page"))
        if key in first:
            folded[str(finding["id"])] = first[key]
        else:
            first[key] = str(finding["id"])
    return folded


def _backs_right(finding: Mapping[str, Any], settle: _Settle) -> bool:
    """A Right settle has code's witness (security audit 2026-10-18): both values printed at their
    spots, the vendor's sum, GV's rule, or the drawings' own legend line read by code; and no check
    disagrees. Otherwise it would be the AI's word alone."""
    if settle.method == "legend":
        return True
    checks = {**(finding.get("checks") or {}), **settle.checks}
    return bool(witnesses(checks, "same")) and disagreement(checks, "same") is None


def _code_backs(finding: Mapping[str, Any]) -> bool:
    """A finding's own status has code's witness and no check against it."""
    checks = finding.get("checks") or {}
    status = finding.get("status")
    return bool(witnesses(checks, status)) and disagreement(checks, status) is None


def _settle_one(
    finding: Mapping[str, Any], ctx: ResolverContext
) -> tuple[_Settle | None, KeepReason | None, str | None]:
    """Code's rules on one decision item, in order: the first that settles it wins. Else the
    reason a rule found to keep it (or `None`), and a sharper read's words for its question."""
    reason: KeepReason | None = None
    for got in (_other_trades_tags(finding), _out_of_scope(finding)):
        if got is not None:
            return got, None, None
    limits = _filler_limits(finding, ctx.settings)
    if isinstance(limits, _Settle):
        return limits, None, None
    if limits is not None:
        reason = limits  # outside GV's usual range: a note, the later rules may still settle it
    summed = _vendor_sum(finding)
    if isinstance(summed, _Settle):
        return summed, None, None
    if summed is not None and reason is None:
        reason = summed
    for later in (
        _printed_vs_printed(finding, ctx),
        _prototype_prints_it(finding, ctx),
    ):
        if later is not None:
            return later, reason, None
    tags = _legend_tags(finding, ctx)
    if isinstance(tags, _Settle):
        return tags, None, None
    if tags is not None and reason is None:
        reason = tags
    if _splash_unlike(finding):
        return None, reason, None  # no sharper read (paid) can make it like for like
    read = _sharper_read(finding, ctx)
    if isinstance(read, _Settle):
        return read, None, None
    return None, reason, read


def resolve(findings: Sequence[Mapping[str, Any]], ctx: ResolverContext) -> Resolution:
    """Settle what the drawings answer among `findings` (an answer's live findings). Only items
    with status `check` are settled or folded; a Wrong or Right item may gain `also_on`."""
    live = [dict(f) for f in findings]
    before = _counts(live)
    by_id = {str(f["id"]): f for f in live}
    absorbed_how, also = _merge(live, ctx.areas)
    changed: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []

    def entry(
        finding: Mapping[str, Any], outcome: Outcome, method: str, how: str, into: str | None
    ) -> None:
        entries.append(
            {
                "finding_id": str(finding["id"]),
                "page": finding.get("page"),
                "vs_page": finding.get("vs_page"),
                "item": finding.get("item"),
                "vendor": finding.get("vendor"),
                "architect": finding.get("architect"),
                "outcome": outcome,
                "method": method,
                "how": how,
                "into": into,
            }
        )

    absorbed: dict[str, str] = {}
    for duplicate, (primary_id, how) in absorbed_how.items():
        entry(by_id[duplicate], "merged", "same_item", how, primary_id)
        absorbed[duplicate] = f"Settled by GV Review: {how}"
    for primary_id, entries_also in also.items():
        primary = changed.get(primary_id) or dict(by_id[primary_id])
        primary["also_on"] = [*(primary.get("also_on") or []), *entries_also]
        together = _joined(
            primary, [by_id[str(e["finding_id"])] for e in entries_also if e["finding_id"] in by_id]
        )
        if together:
            primary = missing_together(primary, together)
        changed[primary_id] = primary

    kept: list[tuple[dict[str, Any], KeepReason, str | None]] = []
    for finding in live:
        finding_id = str(finding["id"])
        if finding.get("status") != "check" or finding_id in absorbed:
            continue
        if internal(finding):
            continue  # GV's reviewer only (#1652): no rule or reading ever settles it
        if finding.get("created_by") == "filler_range":
            continue  # GV's filler range already decided it, the architect first (`filler_range`)
        if finding.get("created_by") == "sink_offset":
            continue  # GV's sink front offset: code's own note, the architect first (#1807)
        current = changed.get(finding_id) or dict(finding)
        settle, reason, read_words = _settle_one(current, ctx)
        if settle is not None and settle.outcome == "right" and not _backs_right(current, settle):
            settle = None  # the AI's word alone never makes an item Right: it stays
        if settle is None:
            kept.append((current, reason or keep_reason(current), read_words))
            continue
        if settle.outcome == "not_a_difference":
            entry(current, "not_a_difference", settle.method, settle.how, None)
            absorbed[finding_id] = f"Settled by GV Review: {settle.how}"
            changed.pop(finding_id, None)
            continue
        current["status"] = "different" if settle.outcome == "wrong" else "same"
        if settle.checks:
            current["checks"] = {**(current.get("checks") or {}), **settle.checks}
        current["settled"] = {
            "outcome": settle.outcome,
            "method": settle.method,
            "how": settle.how,
            "was": "check",
        }
        current["question"] = None
        changed[finding_id] = current
        entry(current, settle.outcome, settle.method, settle.how, None)

    folded = _same_question([(f, r) for f, r, _w in kept])
    reasons: dict[str, int] = {}
    for finding, reason, read_words in kept:
        finding_id = str(finding["id"])
        if finding_id in folded:
            primary_id = folded[finding_id]
            how = f"the same question as {primary_id} (which drawing governs this page)."
            entry(finding, "merged", "same_question", how, primary_id)
            absorbed[finding_id] = f"Settled by GV Review: {how}"
            primary = changed.get(primary_id) or dict(by_id[primary_id])
            primary["also_on"] = [
                *(primary.get("also_on") or []),
                _also(finding),
                *(finding.get("also_on") or []),
            ]
            changed[primary_id] = primary
            changed.pop(finding_id, None)
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
        current = changed.get(finding_id) or dict(finding)
        current["question"] = question_for(current, reason, read_words)
        current["keep_reason"] = reason
        changed[finding_id] = current

    for finding_id in list(changed):
        if finding_id in absorbed:
            del changed[finding_id]
    # An item folded into one that was folded in turn names the item that carries it now.
    into = {str(e["finding_id"]): e["into"] for e in entries if e.get("into")}
    for listed in entries:
        target = listed.get("into")
        seen: set[str] = set()
        while isinstance(target, str) and target in into and target not in seen:
            seen.add(target)
            target = into[target]
        listed["into"] = target
    after_findings = [changed.get(str(f["id"]), f) for f in live if str(f["id"]) not in absorbed]
    block = {
        "before": before,
        "after": _counts(after_findings),
        "settled": entries,
        "kept": reasons,
        "reads": ctx.reads,
        "cost_usd": "0.00",
    }
    return Resolution(changed=list(changed.values()), absorbed=absorbed, block=block)


# ---- the counts in the answer's words -----------------------------------------------------------

_NUMBER_WORDS: Final = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
]
_COUNT_KIND: Final = (
    r"wrong|different|incorrect|need(?:s)?\s+(?:your|a|the\s+reviewer'?s)\s+(?:decision|call)"
    r"|your\s+decision|to\s+decide|to\s+check|right|correct|match(?:es)?"
)
_COUNT: Final = re.compile(
    rf"\b(?P<n>\d{{1,4}}|{'|'.join(_NUMBER_WORDS)})"
    r"(?P<mid>\s+(?:more\s+|other\s+)?(?:(?:items?|things?|findings?|differences?|values?)\s+)?"
    r"(?:(?:are|is|look|looks|were|was|that|which)\s+)?(?:still\s+)?)"
    rf"(?P<kind>{_COUNT_KIND})(?![-\w])",
    re.IGNORECASE,
)
#: A count word followed by one of these is a place, not a count ("the 3 right-hand doors").
_NOT_A_COUNT_AFTER: Final = re.compile(
    r"\s+(?:side|hand|end|edge|wall|bay|panel|door|drawer|corner|angle)s?\b", re.IGNORECASE
)
_NOT_A_COUNT_BEFORE: Final = re.compile(r"(?:\bp\.|\bpages?|\bround|#)\s*$", re.IGNORECASE)


def _kind_of(words: str) -> str:
    low = words.lower()
    if low.startswith(("wrong", "different", "incorrect")):
        return "wrong"
    if low.startswith(("right", "correct", "match")):
        return "right"
    return "decision"


def recount_words(text: str, counts: Mapping[str, int]) -> str:
    """`text` (the AI's own words, written before code settled anything) with every count of Wrong,
    Your decision or Right it states set to the final list's (#1554): "Six items are wrong" becomes
    "Four items are wrong" when four are. A number that names a page, a round or a place is left
    alone. Code never shows a count that differs from the list the screen counts."""

    def fix(match: re.Match[str]) -> str:
        before = text[: match.start()]
        after = text[match.end() :]
        if _NOT_A_COUNT_BEFORE.search(before) or _NOT_A_COUNT_AFTER.match(after):
            return match.group(0)
        kind = _kind_of(match.group("kind"))
        value = int(counts.get(kind, 0))
        given = match.group("n")
        if given.isdigit() or value >= len(_NUMBER_WORDS):
            number = str(value)
        else:
            number = _NUMBER_WORDS[value]
            if given[:1].isupper():
                number = number.capitalize()
        words = match.group("kind")
        if re.match(r"needs?\b", words, re.IGNORECASE):
            words = re.sub(
                r"^needs?", "needs" if value == 1 else "need", words, flags=re.IGNORECASE
            )
        return f"{number}{match.group('mid')}{words}"

    return _COUNT.sub(fix, text)


# ---- the engine's hook ---------------------------------------------------------------------------


def _verifier(pdf_paths: Mapping[str, Path], stop: Callable[[], bool] | None = None) -> Verify:
    """`verify_value` with its drawn-ink route ($0, local OCR): text first, then the vendor's ink.
    `stop`: Stop was pressed, so nothing more is read (2026-10-23)."""
    checks = import_module("app.review.assistant.finding_checks")
    paths = dict(pdf_paths)
    started = monotonic()

    def verify(box: Mapping[str, Any], value: str) -> str:
        if monotonic() - started > VERIFY_BUDGET_S or (stop is not None and stop()):
            return "cannot_read"  # the answer does not wait longer for code's witness
        name = box.get("file")
        path = paths.get(str(name)) if name is not None else None
        if path is None and len(paths) == 1:
            path = next(iter(paths.values()))
        if path is None or not path.is_file():
            return "cannot_read"
        page = box.get("page")
        if not isinstance(page, int):
            return "cannot_read"
        found = checks.verify_at_spot(path, page, box, value, drawn=True)
        return str(found.get("result") or "cannot_read")

    return verify


def _legend_reader(pdf_paths: Mapping[str, Path]) -> Legend:
    """A line of the drawings' own text (the pasted drawings, never GV's markup) naming every tag."""
    stamp = import_module("extraction.stamp_text")
    pdfplumber = import_module("pdfplumber")
    paths = [p for p in pdf_paths.values() if p.is_file()]

    def legend(pages: Sequence[int], tags: Sequence[str]) -> str | None:
        for path in paths:
            data = path.read_bytes()
            for page in pages:
                flattened = stamp.stamps_only(data, page - 1)
                with pdfplumber.open(io.BytesIO(flattened)) as document:
                    if not 0 < page <= len(document.pages):
                        continue
                    text = document.pages[page - 1].extract_text() or ""
                for line in text.splitlines():
                    if all(tag in line for tag in tags):
                        return line
        return None

    return legend


class _ResolverBudget:
    """The engine's budget for the resolver's reads, never more than `RESOLVER_CAP_MICROS`."""

    def __init__(self, engine: Any) -> None:
        review = import_module("app.review.assistant.chat_read_review")
        self.inner = review.Budget(engine)
        self.no_room = review.NoRoom
        self.used = 0

    def hold(self, estimate: int | None) -> int:
        amount = estimate or 0
        if self.used + amount > RESOLVER_CAP_MICROS:
            raise self.no_room
        held: int = self.inner.hold(amount)
        self.used += held
        return held

    def release(self, amount: int) -> None:
        self.inner.release(amount)


def _reader(engine: Any) -> Read | None:
    """One sharper read (`read_adjudication.third_read`: Sonnet at low effort, a 300 dpi crop
    without GV's markup) through the engine's own call path and caps."""
    trust = import_module("app.review.assistant.chat_trust")
    module = trust.load_module("app.review.assistant.read_adjudication")
    if module is None:
        return None
    review = import_module("app.review.assistant.chat_read_review")
    drawing = import_module("app.review.assistant.chat_drawing")
    budget = _ResolverBudget(engine)
    call = review._model_call(
        engine,
        budget,
        short=module.adjudication_model(),
        effort=module.ADJUDICATION_EFFORT,
        system=review.READ_SYSTEM,
        max_tokens=module.ADJUDICATION_MAX_TOKENS,
        prompt_tokens=module.ADJUDICATION_PROMPT_TOKENS,
        prompt_id=review._prompt_id(engine, "resolve"),
    )
    pdf_paths = engine.book.pdf_paths()

    def read(finding: Mapping[str, Any]) -> SharperRead | None:
        if engine.cancel.is_set():
            return None
        with drawing._PDFIUM_LOCK:
            crops = review._adjudication_crops(module, finding, pdf_paths)
        crops = {side: crop for side, crop in crops.items() if side == "vendor"}
        if not crops:
            return None
        try:
            got = module.third_read(crops, str(finding.get("item") or ""), call)
        except review.NoRoom:
            return None
        return SharperRead(architect=None, vendor=got.vendor, sure=bool(got.sure))

    return read


def resolve_answer(engine: Any) -> list[dict[str, Any]]:
    """The engine's hook (whole-set work, before the trust report): settle, fold and question the
    answer's "Your decision" items, show each change, add the `resolver` block. Returns the
    settled entries (the cloud drawing's and the report's "Settled automatically" lists)."""
    book = engine.book
    if not any(f.get("status") == "check" for f in book.live()):
        return []
    workspace = engine.tools.workspace
    scope = import_module("app.review.assistant.chat_scope")
    try:
        rules = (workspace.skills / "rules.md").read_text(encoding="utf-8")
    except OSError:
        rules = None
    pdf_paths = book.pdf_paths()
    ctx = ResolverContext(
        areas=page_areas(scope.load_page_map(workspace.set_folder), book.main_file),
        settings=settings_in_force(rules),
        verify=_verifier(pdf_paths, engine.cancel.is_set),
        read=_reader(engine),
        legend=_legend_reader(pdf_paths),
    )
    spent = int(engine.spent)
    trust = import_module("app.review.assistant.chat_trust")
    with engine.finding_lock:
        result = resolve(book.live(), ctx)
        for finding_id, reason in result.absorbed.items():
            if book.absorb(finding_id):
                engine._show_dropped(finding_id, reason)
        for finding in result.changed:
            if isinstance(finding.get("checks"), Mapping):
                # Trust, evidence kind and witnesses again, from the checks the resolver added.
                finding = trust.judged(finding)
            if book.replace(finding):
                engine._show_finding(finding)
    block = dict(result.block)
    block["cost_usd"] = f"{max(0, int(engine.spent) - spent) / 1_000_000:.2f}"
    index = engine._add_block({"type": "resolver", "resolver": block})
    engine.emit("resolver", {"index": index, "resolver": block})
    return list(block["settled"])
