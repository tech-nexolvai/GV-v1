"""The full check's helper brief and the per-page checklist a helper must answer (#1515).

Why this exists: a live full check of a 60-page test set found 14 of 22 known differences. The
misses were not pages nobody opened; they were items a helper had on its screen and did not
compare one by one:

- an architect dimension chain added up and compared only as a total (each segment locates an
  outlet the vendor moved);
- a dimension that locates an outlet read as a width, the missing outlet left as a remark;
- the architect's note ("pull-out drawers") never compared, only the drawn door count;
- a vendor number under GV's markup left "unreadable" without trying the text layer or the mm;
- the strings around an item GV Review had already recorded skipped, as if the area were done.

So a helper's brief now names its pages with their areas and pairs (from the set pack's page map),
GV's recorded results on those pages (not to report again, but never closing the area), the leads
GV's own page review could not settle there, and a checklist it answers in its report, one line
per page. Code reads those lines back (`checklist_gaps`) and can send the helper back once.

Later (#1543), given the same test set, claude.ai flagged kinds of differences the helpers rarely
report because the checklist was mostly about dimensions: hardware and accessories, construction
notes the vendor contradicts, position (centred, aligned, colliding, fitting its opening), extent
and count, the ID set against the brand prototype, and sizes scaled where none is printed. The
checklist names each of them; the report line code reads back is unchanged.

Then (#1545), on items that need a person, claude.ai did the reviewer's reasoning (what a dimension
measures, an estimate with its method, a likely answer, what to confirm) where ours said only
"confirm the bay sizes"; and four heights were called Wrong although the architect's dimension ran
to the top of a box above the unit. So every Your-decision item carries `decision_help`, and the
extent of a dimension is measured (`measure_on_drawing`) before a height or width is called Wrong.

Then (#1569), claude.ai and our full check TOGETHER found 18 of 22 known differences on that set,
against 14 and 6 alone: claude.ai compares what is drawn, ours compares printed numbers, and one
long checklist made a helper do mostly one of the two. So each page group can get two helpers with
short briefs: the NUMBERS lens (dimensions, strings, scaled and spanned sizes) and the DRAWN lens
(notes against the drawing, doors / drawers / bins / shelves and their arrangement, hardware,
construction notes, position, extent, the ID set against the brand prototype, material and finish
tags). Each lens has its own checklist line; code merges their duplicates (`full_check_lenses`).
`lens="both"` is the single helper's brief, unchanged.

Products are data (#1547): the shared checklist above holds for every product, and each product in
focus adds its own items from the product registry (`vocabulary/products.yaml`): a countertop's
sink and splashes, a cabinet's fronts and fillers, a light fixture's lamp base. A product added to
the registry reaches the brief with no code change.

Pure code, no AI call and no I/O: the engine calls `helper_brief` for each helper it plans and
`checklist_gaps` on each helper's report.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from app.review.assistant.chat_trust import recorded_from
from app.review.assistant.dim_index import checklist_label_gaps
from vocabulary.product_registry import Product, registry

__all__ = [
    "CHECKLIST_FIELDS",
    "DRAWN_BRIEF",
    "HELPER_BRIEF",
    "LEAD_RESULTS",
    "LENSES",
    "LENS_FIELDS",
    "LENS_SCOPE",
    "NUMBERS_NOTE",
    "PRINCIPLE",
    "checklist_gaps",
    "checklist_send_back",
    "helper_brief",
    "label_summary",
    "leads_on_pages",
    "page_lines",
    "product_checklist",
    "recorded_on_pages",
    "unreached_pages",
    "unreached_send_back",
    "with_label_gaps",
]

#: The AI page review results that are leads for a full check (GV's page review could not settle
#: them, or found the item on one side only). Never recorded differences: nothing is lost if the
#: helper finds they match.
LEAD_RESULTS: Final = ("unsure", "not_shown")

#: What to compare (#1515, #1543, #1569), as one principle (prompts design 2026-10-18, W2). It
#: replaced a 13-item checklist that grew one incident at a time; the usual misses and how to spot
#: each now live in the knowledge pack (`knowledge/vendor-mistakes.md`), which each helper kind
#: carries in its own system prompt (`full_check_specialists`).
PRINCIPLE: Final = (
    "Treat every thing the architect shows for your area as its own comparison: each dimension "
    "(each segment of a chain on its own), each note and tag, each drawn piece. A dimension that "
    "locates a thing (an outlet, a sink centre line) is that thing's position: if the vendor leaves "
    "the thing out, that is its own item, with the architect's values. A recorded GV result never "
    "closes its area: still compare everything around it. A round-2 page is compared with the "
    "architect again and with round 1 (Fixed, or Still wrong). Your instructions list the usual "
    "misses and how to spot each."
)

#: What each lens covers, in one line (#1569): `both` is the single helper of a page group.
LENS_SCOPE: Final[dict[str, str]] = {
    "both": (
        "Yours: every size and dimension string, every note and tag, and what is drawn (fronts, "
        "shelves, hardware, position, extent, the ID set against the brand prototype)."
    ),
    "numbers": (
        "Yours: every size and dimension string, scaled sizes (only ever Not compared), and what "
        "each dimension spans."
    ),
    "drawn": (
        "Yours: notes, callouts and tags, doors, drawers, bins and shelves and how they are "
        "arranged, hardware, construction notes, position, extent, the ID set against the brand "
        "prototype, finish tags. Never a size."
    ),
}

#: The lenses a helper can have: `both` is the single helper of a page group.
LENSES: Final = ("both", "numbers", "drawn")

#: The counted fields of each lens's report line; `not compared` follows them.
LENS_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "both": ("architect", "notes", "strings"),
    "numbers": ("architect", "strings"),
    "drawn": ("notes", "counts", "hardware"),
}

#: The counted fields of the single helper's report line.
CHECKLIST_FIELDS: Final = LENS_FIELDS["both"]

HELPER_BRIEF: Final = """OBJECTIVE: a full GV review of the vendor's drawings on pages {pages} of {file}, against the architect's drawing of the same area. Your pages:
{page_lines}
{scope}
Your instructions hold the trade knowledge for this job. products/<id>.md for each product in focus and the settings table of rules.md are in /mnt/skills/gv-review: read them before you judge a value against a GV rule (a setting nobody entered is missing: say so, never assume one).

HOW: view your pages, then zoom with marks: true on what you compare, the architect's and the vendor's side by side (the app picks the sharpest dpi that fits; up to 8 crops per call). Numbers drawn as pen strokes have no label: read them from the picture and give their box. A vendor number under GV's markup is still to be found (read_under_markup on that spot, its mm against its inches) before you call it unreadable.

ALREADY RECORDED by GV Review on your pages (they are checked separately: do not report them again, but still compare everything around them, the strings that build them too; a recorded item never closes its area):
{recorded}

LEADS from GV's own page review on your pages (it could not settle them, or saw the item on one side only): settle each on the drawing, with a finding or a reading.
{leads}

WHAT TO COMPARE: {principle}

{product_checklist}

DIMENSION LABELS: code numbered every printed dimension it found on your pages (zoom with marks: true shows the ids; set pack dims.json lists them):
{labels}
Every vendor label on your pages must be handled: matched in a finding (vendor_dim, with architect_dim for the architect's), part of a string (record_string part_dims and total_dim), or named on a line LABELS p<N> not on architect: D3, D9. Code counts the vendor labels left over.

OUTPUT: code, not you, decides what is wrong, and only a value code can find on the drawing can become a red cloud, so give code the spot, not your reading. For every item you compare, the Right ones too, call report_finding with the labels: vendor_dim and architect_dim (p28.D5 for another page). Type a value with its box only when no label covers it; code will try to read it there. For every dimension string, record_string with part_dims and total_dim; code adds them. Before you pair two heights or widths, say in why where each end lands (the spans words of what-dimensions-measure). When the ends differ, or you are not sure two labels measure the same thing, still report it: status check (Not compared) with decision_help and one line why. A miss is never found again; a doubtful item is only listed. Status different = Wrong, check = Not compared, same = Right. Name each item starting with its area from the list above ("<area>: counter height"), the same way on every page, view and round; round on vendor findings; vs_page when the architect is on another page. record_reading for a value you read without a label. set_coverage for each page. verified: false (a separate check verifies).

Record each item as soon as you have read it (report_finding, record_string), page by page: a reading only in your report is lost, because your report records nothing.

REPORT BACK (short): per page the differences (item, vendor vs architect, finding ids), anything unreadable and where; for a difference you read but could not record, one line exactly like
UNRECORDED p<N> | <area: item> | architect <value> | vendor <value> | different or check | <where each end lands, or why>
then one line per page exactly like
CHECKLIST p<N>: architect a/b; notes a/b; strings a/b; not compared: <what and why, or none>
(a/b = items done of items on the page).

BOUNDARIES: only your pages; never guess a number; GV's markup is never a value; no pass/fail words (code records results, and a reviewer signs off)."""

#: What the numbers lens adds to `HELPER_BRIEF` (#1569).
NUMBERS_NOTE: Final = (
    """LENS: numbers. Another helper has the same pages for what is drawn and written (notes, doors and drawers, hardware, extent, the brand prototype, finish tags): leave those to it, unless a number depends on them."""
)

#: The drawn-things lens's brief (#1569): what is drawn and written, not sizes.
DRAWN_BRIEF: Final = """OBJECTIVE: the drawn things on pages {pages} of {file}: what the vendor drew and wrote, against the architect's drawing of the same area (a brand prototype is its own group). Another helper compares every number on these pages; you compare what is drawn and written, never a size. Your pages:
{page_lines}
{scope}
Your instructions hold the trade knowledge for this job; products/<id>.md for each product in focus is in /mnt/skills/gv-review.

HOW: view your pages' overview pictures, then zoom (up to 8 crops per call) on each unit's notes, tags, fronts and hardware, the architect's and the vendor's side by side. Anything coloured is GV's markup, never the vendor's drawing.

ALREADY RECORDED by GV Review on your pages (they are checked separately: do not report them again):
{recorded}

WHAT TO COMPARE: {principle} A note the vendor does not follow is a difference, even when the count of fronts matches.

{product_checklist}

OUTPUT: report_finding for every drawn thing you compare, the same ones too (status different, check or same; every check with decision_help), with vendor_box and arch_box. `architect` and `vendor` say in a few words what each side shows ("pull-out drawers (note)", "2 doors drawn", "no grommet"); vs_page when the architect is on another page. When you are not sure the two sides show the same thing, still report it, as check with one line why: a miss is never found again. Name each item starting with its area from the list above ("<area>: bin drawer fronts"), the same way on every page, view and round. Never report a dimension or a dimension string, and do not call set_coverage: the numbers helper records both. verified: false (a separate check verifies).

REPORT BACK (short): per page the differences (item, vendor vs architect, finding ids), then one line per page exactly like
CHECKLIST p<N>: notes a/b; counts a/b; hardware a/b; not compared: <what and why, or none>
(a/b = items done of items on the page; notes = the architect's notes, callouts and tags; counts = doors, drawers, bins, shelves and sections; hardware = pulls, locks, legs, grommets, power units).

BOUNDARIES: only your pages; never guess; GV's markup is never the vendor's drawing; no pass/fail words (code records results, and a reviewer signs off)."""

_NONE: Final = "- none"
_MAX_ROW: Final = 160


def _short(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= _MAX_ROW else f"{text[: _MAX_ROW - 1]}…"


def _pages_of(page_map: Mapping[str, Any] | Sequence[Any] | None) -> dict[int, Mapping[str, Any]]:
    """The page map's entries by page number (the set pack's `page_map.json`, or its `pages`)."""
    entries: object = page_map.get("pages") if isinstance(page_map, Mapping) else page_map
    out: dict[int, Mapping[str, Any]] = {}
    if isinstance(entries, Sequence) and not isinstance(entries, str):
        for entry in entries:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                out[entry["page"]] = entry
    return out


def _role_word(half: Mapping[str, Any]) -> str:
    role = str(half.get("role") or "unlabelled").replace("_", " ")
    if role == "vendor" and isinstance(half.get("round"), int):
        role = f"vendor round {half['round']}"
    return role


def _centre(box: object) -> tuple[float, float] | None:
    if not isinstance(box, Sequence) or isinstance(box, str) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    return (x0 + x1) / 2, (y0 + y1) / 2


def _places(entry: Mapping[str, Any]) -> dict[str, str]:
    """Where each of a page's two kinds of drawing is ("top" / "bottom", "left" / "right"), so a
    helper never takes one for the other (#1644: a page holding the architect's drawing over a
    brand prototype was read as the prototype alone). Empty unless the page holds exactly two
    kinds of drawing, each with its box."""
    centres: dict[str, list[tuple[float, float]]] = {}
    for half in entry.get("halves") or []:
        if not isinstance(half, Mapping):
            continue
        centre = _centre(half.get("box"))
        if centre is None:
            return {}
        centres.setdefault(_role_word(half), []).append(centre)
    if len(centres) != 2:
        return {}
    (first, a), (second, b) = (
        (role, (sum(x for x, _ in c) / len(c), sum(y for _, y in c) / len(c)))
        for role, c in centres.items()
    )
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dy) >= abs(dx):
        return {first: "top", second: "bottom"} if dy > 0 else {first: "bottom", second: "top"}
    return {first: "left", second: "right"} if dx > 0 else {first: "right", second: "left"}


def _drawings(entry: Mapping[str, Any]) -> str:
    """`architect (top) + brand prototype (bottom)`, from the page's labelled drawings."""
    words: list[str] = []
    for half in entry.get("halves") or []:
        if not isinstance(half, Mapping):
            continue
        role = _role_word(half)
        if role not in words:
            words.append(role)
    if not words:
        return str(entry.get("role") or "no labelled drawing")
    places = _places(entry)
    return " + ".join(f"{w} ({places[w]})" if w in places else w for w in words)


def _has_architect(entry: Mapping[str, Any] | None) -> bool:
    return any(
        isinstance(h, Mapping) and h.get("role") == "architect"
        for h in (entry or {}).get("halves") or []
    )


def _area_of(entry: Mapping[str, Any], known: Mapping[int, Mapping[str, Any]]) -> str | None:
    """The page's area within its section: its own title, else the title of the page it takes its
    area from (two sections may reuse an area's title)."""
    owner: Mapping[str, Any] = entry
    if not entry.get("area") and isinstance(entry.get("area_from_page"), int):
        owner = known.get(entry["area_from_page"]) or {}
    area = owner.get("area")
    if not area:
        return None
    return " ".join(f"{owner.get('section') or ''} / {area}".split()).lower()


def _pages_words(pages: Sequence[int]) -> str:
    named = [f"p{p}" for p in pages]
    return named[0] if len(named) == 1 else ", ".join(named[:-1]) + f" and {named[-1]}"


def page_lines(
    pages: Iterable[int], page_map: Mapping[str, Any] | Sequence[Any] | None = None
) -> list[str]:
    """One line per page: its area, whose drawings it holds (and where each is, when two kinds
    share the page), and where the area's architect drawings are. A page with no architect drawing
    is pointed to the area's pages that have one (never just to the page its area title came from:
    that page may hold only the vendor's drawing); a page with one is told of the area's other
    architect drawings, since the larger scale or detail governs (R5). A page the map does not know
    is still listed."""
    known = _pages_of(page_map)
    architect_pages: dict[str, list[int]] = {}
    for number, listed in sorted(known.items()):
        key = _area_of(listed, known)
        if key and _has_architect(listed):
            architect_pages.setdefault(key, []).append(number)
    lines: list[str] = []
    for page in sorted(set(pages)):
        entry = known.get(page)
        if entry is None:
            lines.append(f"- p{page}: not in the page map; look at it")
            continue
        area = entry.get("area") or entry.get("section") or "area not named"
        line = f"- p{page}: {_short(area)}; {_drawings(entry)}"
        key = _area_of(entry, known)
        others = [p for p in architect_pages.get(key or "", []) if p != page]
        pair = entry.get("pair_page")
        if isinstance(pair, int) and pair != page:
            line += f"; the architect's drawing is on p{pair}"
        elif _has_architect(entry):
            if others:
                line += (
                    f"; the area's other architect drawing is on {_pages_words(others)} (the "
                    "larger scale or the detail governs)"
                )
        elif others:
            line += f"; the architect's drawing is on {_pages_words(others)}"
        if entry.get("pair_page") or any(
            isinstance(h, Mapping) and h.get("heading_by") == "reviewer"
            for h in entry.get("halves") or []
        ):
            line += " (the reviewer said so in the plan check)"
        lines.append(line)
    return lines


def recorded_on_pages(document: Mapping[str, Any] | None, pages: Iterable[int]) -> list[str]:
    """GV Review's recorded differences (code FAILs, evaluator-checked AI page review rows) on these
    pages, one line each, from `review_results`' whole-set document."""
    wanted = set(pages)
    lines: list[str] = []
    for item in recorded_from(document).differences:
        page = item.get("page")
        if page not in wanted:
            continue
        what = _short(item.get("what") or "recorded difference")
        sides = [
            f"{side} {_short(item[side])}"
            for side in ("architect", "vendor")
            if item.get(side) not in (None, "")
        ]
        tail = f" ({'; '.join(sides)})" if sides else ""
        lines.append(f"- p{page}: {what}{tail} [{item.get('record_id')}]")
    return lines


def leads_on_pages(document: Mapping[str, Any] | None, pages: Iterable[int]) -> list[str]:
    """GV's AI page review rows on these pages that it could not settle (`unsure`) or saw on one
    side only (`not_shown`), one line each. The whole-set `review_results` lists them per page under
    `leads`; a one-page document lists every row under `rows`, each with its `result`."""
    if not isinstance(document, Mapping):
        return []
    wanted = set(pages)
    lines: list[str] = []
    for sheet in document.get("ai_page_review") or []:
        if not isinstance(sheet, Mapping) or sheet.get("page") not in wanted:
            continue
        rows = [
            row
            for row in [*(sheet.get("leads") or []), *(sheet.get("rows") or [])]
            if isinstance(row, Mapping) and row.get("result") in LEAD_RESULTS
        ]
        for row in rows:
            what = _short(row.get("measures") or row.get("key") or "item")
            sides = [
                f"{side} {_short(row[f'{side}_text'])}"
                for side in ("architect", "vendor")
                if row.get(f"{side}_text") not in (None, "")
            ]
            tail = f" ({'; '.join(sides)})" if sides else ""
            lines.append(f"- p{sheet['page']}: {row['result'].replace('_', ' ')}: {what}{tail}")
    return lines


def _lens(lens: str) -> str:
    if lens not in LENSES:
        raise ValueError(f"unknown lens {lens!r}")
    return lens


def _focus(products: Sequence[str] | None) -> tuple[Product, ...]:
    """The products in focus from the registry, in its order; none named: every active one."""
    known = registry()
    if not products:
        return known.active()
    return tuple(p for pid in known.ordered(products) if (p := known.get(pid)) is not None)


def product_checklist(products: Sequence[str] | None = None) -> str:
    """The products' own checklist items, after the shared ones: per product in focus, its
    registry checklist and the kinds of GV standard it has (their values are in rules.md)."""
    focus = _focus(products)
    words = registry().words([p.id for p in focus]).lower()
    lines = [f"FOR EACH PRODUCT IN FOCUS ({words}), also on every page:"]
    for product in focus:
        draft = ", draft" if product.draft else ""
        lines.append(f"{product.plural} (category {product.id}{draft}; products/{product.id}.md):")
        lines.extend(f"- {item.name.replace('_', ' ')}: {item.text}" for item in product.checklist)
        if "global_rules" in product.check_kinds and product.global_rule_hints:
            hints = "; ".join(hint.rstrip(".") for hint in product.global_rule_hints)
            lines.append(f"- GV standards not on the drawing (values in rules.md): {hints}.")
    if {p.id for p in focus} >= set(registry().active_ids()):
        lines.append(
            "An item of no product in focus is category other. report_finding's category is the "
            "product id."
        )
    else:
        # A strict scope (Anant, 2026-10-18): only the picked products are checked.
        lines.append(
            f"ONLY {words} are checked: leave out every item of another product or of no product "
            "(code does not record it). report_finding's category is the product id."
        )
    return "\n".join(lines)


def helper_brief(
    pages: Sequence[int],
    *,
    file_name: str,
    page_map: Mapping[str, Any] | Sequence[Any] | None = None,
    review_document: Mapping[str, Any] | None = None,
    lens: str = "both",
    labels: Mapping[int, Sequence[Mapping[str, Any]] | None] | None = None,
    products: Sequence[str] | None = None,
) -> str:
    """The brief for one full-check helper: its pages (with areas and pairs from the page map),
    GV's recorded results and its page review's leads on them, the per-page checklist, and the
    numbered dimension labels on its pages (`labels`: per page, #1558; `None`: not listed yet).

    `review_document` is `review_results`' whole-set document (what the lead's `review_results`
    call returns); `None` when it could not be read, and the brief says so. `lens` (#1569):
    `both` (one helper per page group), `numbers` (sizes and strings) or `drawn` (what is drawn
    and written; GV's page-review leads are the numbers helper's). `products`: the products in
    focus (registry ids); none: every active product (`product_checklist`)."""
    if not pages:
        raise ValueError("a helper needs at least one page")
    lens = _lens(lens)
    recorded = recorded_on_pages(review_document, pages)
    unread = "- GV Review's results could not be read: compare everything"
    words = {
        "pages": ", ".join(str(page) for page in sorted(set(pages))),
        "file": file_name,
        "page_lines": "\n".join(page_lines(pages, page_map)),
        "recorded": "\n".join(recorded) or (unread if review_document is None else _NONE),
        "scope": LENS_SCOPE[lens],
        "principle": PRINCIPLE,
        "product_checklist": product_checklist(products),
        "labels": (
            "\n".join(label_summary(pages, labels, page_map))
            if labels is not None
            else "- not listed yet: zoom with marks: true to see them"
        ),
    }
    if lens == "drawn":
        return DRAWN_BRIEF.format(**words)
    leads = leads_on_pages(review_document, pages)
    brief = HELPER_BRIEF.format(**words, leads="\n".join(leads) or _NONE)
    if lens == "numbers":
        brief = brief.replace(
            "CHECKLIST p<N>: architect a/b; notes a/b; strings a/b;",
            "CHECKLIST p<N>: architect a/b; strings a/b;",
        )
        brief = f"{brief}\n\n{NUMBERS_NOTE}"
    return brief


def label_summary(
    pages: Iterable[int],
    labels: Mapping[int, Sequence[Mapping[str, Any]] | None],
    page_map: Mapping[str, Any] | Sequence[Any] | None = None,
) -> list[str]:
    """One line per page: how many dimension labels code found on each drawing (#1558), and
    (#1641) each architect or vendor drawing of the page where code found none: its numbers are
    pen strokes or a picture, to read from the picture and give with their box. The paid run of
    2026-10-19 left such pages alone ("the vendor's values are unlabelled, I did not compare
    them"; a page whose architect half had no labels was taken for a brand prototype)."""
    entries = _pages_of(page_map)
    lines: list[str] = []
    for page in sorted(set(pages)):
        found = labels.get(page)
        if found is None:
            lines.append(f"- p{page}: not listed yet (zoom with marks: true)")
            continue
        counts: dict[str, int] = {}
        for label in found:
            side = str(label.get("side") or "unknown")
            counts[side] = counts.get(side, 0) + 1
        words = ", ".join(f"{side} {count}" for side, count in sorted(counts.items()))
        line = f"- p{page}: {len(found)} labels ({words or 'none'})"
        bare = _unlabelled_halves(entries.get(page), found)
        if bare:
            line += (
                f"; NO labels on the {' and the '.join(bare)}: pen strokes or a picture, still "
                "to compare (read each number from the zoom, report it with its tight box)"
            )
        lines.append(line)
    return lines


def _unlabelled_halves(
    entry: Mapping[str, Any] | None, labels: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The page's architect and vendor drawings (from the page map) with no label inside."""
    out: list[str] = []
    for half in (entry or {}).get("halves") or []:
        if not isinstance(half, Mapping) or half.get("role") not in ("architect", "vendor"):
            continue
        box = half.get("box")
        if not isinstance(box, Sequence) or len(box) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        inside = False
        for label in labels:
            lx0, ly0, lx1, ly1 = (float(v) for v in label.get("box") or (0, 0, 0, 0))
            cx, cy = (lx0 + lx1) / 2, (ly0 + ly1) / 2
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                inside = True
                break
        if inside:
            continue
        heading = str(half.get("heading") or "").strip()
        name = "architect's drawing" if half["role"] == "architect" else "vendor's drawing"
        if half.get("round"):
            name += f" (round {half['round']})"
        out.append(f"{name}{f' {heading!r}' if heading else ''}")
    return out


def with_label_gaps(
    gaps: Mapping[int, str],
    report: str,
    pages: Iterable[int],
    labels: Mapping[int, Sequence[Mapping[str, Any]] | None],
    findings: Iterable[Mapping[str, Any]],
    strings: Iterable[Mapping[str, Any]] = (),
    readings: Iterable[Mapping[str, Any]] = (),
) -> dict[int, str]:
    """`checklist_gaps` plus the vendor dimension labels nobody handled (#1558), per page."""
    wanted = set(pages)
    known = {page: found for page, found in labels.items() if page in wanted and found is not None}
    left = checklist_label_gaps(report, known, findings, strings, readings)
    out = dict(gaps)
    for page, why in left.items():
        out[page] = f"{out[page]}; {why}" if page in out else why
    return out


# ---- reading the checklist back ------------------------------------------------------------------

_LINE: Final = re.compile(r"CHECKLIST\s+p\.?\s*(\d+)\s*:(.*)", re.IGNORECASE)
_COUNT: Final = re.compile(
    r"\b(architect|notes|strings|counts|hardware)\b\s*:?\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE
)
_NOT_COMPARED: Final = re.compile(r"not compared\s*:\s*(.*)", re.IGNORECASE)
_NOTHING: Final = frozenset({"", "none", "nothing", "-", "n/a", "no"})


def checklist_gaps(report: str, pages: Iterable[int], lens: str = "both") -> dict[int, str]:
    """The pages whose checklist line is missing or shows work left, with why in plain words.

    A line is complete when it gives the lens's counts (`LENS_FIELDS`) and either every count is
    full (a/a) or the `not compared` part names what was left and why. A count short of full with
    `not compared: none` is a gap: something was skipped without saying what."""
    fields = LENS_FIELDS[_lens(lens)]
    found: dict[int, str] = {}
    for match in _LINE.finditer(report or ""):
        found[int(match.group(1))] = match.group(2)
    gaps: dict[int, str] = {}
    for page in sorted(set(pages)):
        body = found.get(page)
        if body is None:
            gaps[page] = "no CHECKLIST line"
            continue
        counts = {
            name.lower(): (int(done), int(of))
            for name, done, of in _COUNT.findall(body)
            if name.lower() in fields
        }
        missing = [name for name in fields if name not in counts]
        if missing:
            gaps[page] = f"no count for {', '.join(missing)}"
            continue
        short = [name for name, (done, of) in counts.items() if done < of]
        said = _NOT_COMPARED.search(body)
        left = said.group(1).strip().strip(".;").lower() if said else ""
        if said is None:
            gaps[page] = "no 'not compared' part"
        elif short and left in _NOTHING:
            gaps[page] = f"{', '.join(short)} not all done, and 'not compared' says none"
    return gaps


_LEFT: Final = {
    "both": "each architect dimension segment by segment, each architect note, each vendor string",
    "numbers": "each architect dimension segment by segment, each vendor string",
    "drawn": "each architect note and tag, each door, drawer, bin and shelf, each piece of hardware",
}


#: A checklist's "not compared" reason that says the page was never compared (not a reason the
#: drawing gives: a prototype, nothing on the architect's side).
_UNREACHED: Final = re.compile(
    r"not reached|not opened|budget|ran out|out of (?:money|time)|did not (?:get|reach|open|zoom)",
    re.IGNORECASE,
)


def unreached_pages(report: str, pages: Iterable[int]) -> list[int]:
    """The pages whose CHECKLIST line (or none at all) says the helper never got to them."""
    found: dict[int, str] = {}
    for match in _LINE.finditer(report or ""):
        found[int(match.group(1))] = match.group(2)
    out = []
    for page in sorted(set(pages)):
        body = found.get(page)
        said = _NOT_COMPARED.search(body) if body is not None else None
        if body is None or (said is not None and _UNREACHED.search(said.group(1))):
            out.append(page)
    return out


def unreached_send_back(pages: Sequence[int]) -> str | None:
    """The message that sends a roster helper back to pages it stopped short of while its money
    and tool calls were not used up (#1634: helpers stopped at a fifth of their money)."""
    if not pages:
        return None
    listed = ", ".join(f"p{page}" for page in pages)
    return (
        f"You still have money and tool calls left for your pages: {listed} are not compared yet. "
        "Compare them now, one page at a time (zoom the labels, record the findings and strings), "
        "set_coverage for them, then report with an updated CHECKLIST line for every page."
    )


def checklist_send_back(gaps: Mapping[int, str], lens: str = "both") -> str | None:
    """The one message that sends a helper back to finish its checklist (`None`: nothing to do)."""
    if not gaps:
        return None
    lines = "\n".join(f"- p{page}: {why}" for page, why in sorted(gaps.items()))
    return (
        "Your checklist is not finished on these pages:\n"
        f"{lines}\n"
        f"Compare what is left ({_LEFT[_lens(lens)]}), report the findings, then end with the "
        "CHECKLIST lines again. If something truly cannot be compared, name it and why under "
        "'not compared'."
    )
