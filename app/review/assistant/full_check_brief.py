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

Pure code, no AI call and no I/O: the engine calls `helper_brief` for each helper it plans and
`checklist_gaps` on each helper's report.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from app.review.assistant.chat_trust import recorded_from

__all__ = [
    "CHECKLIST_FIELDS",
    "HELPER_BRIEF",
    "LEAD_RESULTS",
    "PAGE_CHECKLIST",
    "checklist_gaps",
    "checklist_send_back",
    "helper_brief",
    "leads_on_pages",
    "page_lines",
    "recorded_on_pages",
]

#: The AI page review results that are leads for a full check (GV's page review could not settle
#: them, or found the item on one side only). Never recorded differences: nothing is lost if the
#: helper finds they match.
LEAD_RESULTS: Final = ("unsure", "not_shown")

#: What a helper answers for each of its pages, in this order. The report line names each field
#: (`CHECKLIST p<N>: architect a/b; notes a/b; strings a/b; not compared: ...`).
PAGE_CHECKLIST: Final[tuple[tuple[str, str], ...]] = (
    (
        "architect",
        (
            "Every dimension the architect prints for this area, EACH SEGMENT of a chain on its own "
            "(wall to outlet, outlet to outlet, floor to a J-box or counter): the vendor's value at "
            'the same spot, or "not shown on the vendor". A dimension that locates a thing (an '
            "outlet, a sink or faucet centre line) is that thing's position: a thing the vendor left "
            "out is its own finding with the architect's values, never a remark in a width finding."
        ),
    ),
    (
        "notes",
        (
            "Every architect note, callout and tag (drawers or doors, pull-outs, bins, shelves, "
            'finishes, sink and appliance models, splashes, "by others"): what the vendor shows '
            "for it. A note the vendor does not follow is a difference, even when the drawn count "
            "matches."
        ),
    ),
    (
        "strings",
        (
            "Every vendor dimension string recorded with record_string (parts and printed overall, "
            "as printed, notes left out of the numbers), and every vendor value matched to an "
            'architect value, a brand prototype value (its own group), or "not on the architect".'
        ),
    ),
    (
        "drawn",
        (
            "Where the architect prints no size: counts and layout compared (bays, doors vs "
            'drawers, shelves, uppers, splashes, sink and appliance positions), "about" when '
            "scaled."
        ),
    ),
    (
        "rounds",
        (
            "A round-2 page: each item compared with the architect again and with round 1 (fixed, "
            "still different, or changed)."
        ),
    ),
)

#: The counted fields of a report line; `not compared` follows them.
CHECKLIST_FIELDS: Final = ("architect", "notes", "strings")

HELPER_BRIEF: Final = """OBJECTIVE: a full GV review of the vendor's drawings on pages {pages} of {file}, against the architect's drawing of the same area. Your pages:
{page_lines}
Read /mnt/skills/gv-review/SKILL.md sections 4, 5, 7, 9 and 10 and the settings table of rules.md before you judge a number (a setting nobody entered is missing: say so, never assume one).

HOW: zoom the architect's and the vendor's drawing of each page at 300 dpi for small print (up to 4 crops per call). Numbers drawn as pen strokes are read from the picture; `pdftotext -f N -l N -bbox` of the drawings-only copy gives the boxes of text numbers. Anything coloured is GV's markup, never a value; a vendor number under it is still to be found (drawings-only text, 300 dpi, its mm against its inches) before you call it unreadable.

ALREADY RECORDED by GV Review on your pages (the lead checks these: do not report them again, but still compare everything around them, the strings that build them too; a recorded item never closes its area):
{recorded}

LEADS from GV's own page review on your pages (it could not settle them, or saw the item on one side only): settle each on the drawing, with a finding or a reading.
{leads}

CHECKLIST, for every page:
{checklist}

OUTPUT: report_finding for EVERY item you compare, the same ones too (status different, check or same), with vendor_box and arch_box, values as printed in inches, round on vendor findings, vs_page when the architect is on another page. Name each item starting with its area from the list above ("<area>: counter height"), the same way on every page, view and round. record_string for every dimension string and record_reading for every value you read, both sides; never add up yourself. set_coverage for each page. verified: false (the lead verifies).

REPORT BACK (short): per page the differences (item, vendor vs architect, finding ids), anything unreadable and where, then one line per page exactly like
CHECKLIST p<N>: architect a/b; notes a/b; strings a/b; not compared: <what and why, or none>
(a/b = items done of items on the page).

BOUNDARIES: only your pages; never guess a number; GV's markup is never a value; no pass/fail words."""

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


def _drawings(entry: Mapping[str, Any]) -> str:
    """`architect + vendor round 1 + brand prototype`, from the page's labelled drawings."""
    words: list[str] = []
    for half in entry.get("halves") or []:
        if not isinstance(half, Mapping):
            continue
        role = str(half.get("role") or "unlabelled").replace("_", " ")
        if role == "vendor" and isinstance(half.get("round"), int):
            role = f"vendor round {half['round']}"
        if role not in words:
            words.append(role)
    return " + ".join(words) if words else str(entry.get("role") or "no labelled drawing")


def page_lines(
    pages: Iterable[int], page_map: Mapping[str, Any] | Sequence[Any] | None = None
) -> list[str]:
    """One line per page: its area, whose drawings it holds, and where its architect drawing is
    when it is on another page (`area_from_page`). A page the map does not know is still listed."""
    known = _pages_of(page_map)
    lines: list[str] = []
    for page in sorted(set(pages)):
        entry = known.get(page)
        if entry is None:
            lines.append(f"- p{page}: not in the page map; look at it")
            continue
        area = entry.get("area") or entry.get("section") or "area not named"
        line = f"- p{page}: {_short(area)}; {_drawings(entry)}"
        pair = entry.get("area_from_page")
        if isinstance(pair, int) and pair != page:
            line += f"; the architect's drawing is on p{pair}"
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


def helper_brief(
    pages: Sequence[int],
    *,
    file_name: str,
    page_map: Mapping[str, Any] | Sequence[Any] | None = None,
    review_document: Mapping[str, Any] | None = None,
) -> str:
    """The brief for one full-check helper: its pages (with areas and pairs from the page map),
    GV's recorded results and its page review's leads on them, and the per-page checklist.

    `review_document` is `review_results`' whole-set document (what the lead's `review_results`
    call returns); `None` when it could not be read, and the brief says so."""
    if not pages:
        raise ValueError("a helper needs at least one page")
    recorded = recorded_on_pages(review_document, pages)
    leads = leads_on_pages(review_document, pages)
    unread = "- GV Review's results could not be read: compare everything"
    return HELPER_BRIEF.format(
        pages=", ".join(str(page) for page in sorted(set(pages))),
        file=file_name,
        page_lines="\n".join(page_lines(pages, page_map)),
        recorded="\n".join(recorded) or (unread if review_document is None else _NONE),
        leads="\n".join(leads) or _NONE,
        checklist="\n".join(f"- {name}: {text}" for name, text in PAGE_CHECKLIST),
    )


# ---- reading the checklist back ------------------------------------------------------------------

_LINE: Final = re.compile(r"CHECKLIST\s+p\.?\s*(\d+)\s*:(.*)", re.IGNORECASE)
_COUNT: Final = re.compile(r"\b(architect|notes|strings)\b\s*:?\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_NOT_COMPARED: Final = re.compile(r"not compared\s*:\s*(.*)", re.IGNORECASE)
_NOTHING: Final = frozenset({"", "none", "nothing", "-", "n/a", "no"})


def checklist_gaps(report: str, pages: Iterable[int]) -> dict[int, str]:
    """The pages whose checklist line is missing or shows work left, with why in plain words.

    A line is complete when it gives the three counts and either every count is full (a/a) or the
    `not compared` part names what was left and why. A count short of full with `not compared:
    none` is a gap: something was skipped without saying what."""
    found: dict[int, str] = {}
    for match in _LINE.finditer(report or ""):
        found[int(match.group(1))] = match.group(2)
    gaps: dict[int, str] = {}
    for page in sorted(set(pages)):
        body = found.get(page)
        if body is None:
            gaps[page] = "no CHECKLIST line"
            continue
        counts = {name.lower(): (int(done), int(of)) for name, done, of in _COUNT.findall(body)}
        missing = [name for name in CHECKLIST_FIELDS if name not in counts]
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


def checklist_send_back(gaps: Mapping[int, str]) -> str | None:
    """The one message that sends a helper back to finish its checklist (`None`: nothing to do)."""
    if not gaps:
        return None
    lines = "\n".join(f"- p{page}: {why}" for page, why in sorted(gaps.items()))
    return (
        "Your checklist is not finished on these pages:\n"
        f"{lines}\n"
        "Compare what is left (each architect dimension segment by segment, each architect note, "
        "each vendor string), report the findings, then end with the CHECKLIST lines again. If "
        "something truly cannot be compared, name it and why under 'not compared'."
    )
