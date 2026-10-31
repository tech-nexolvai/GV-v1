"""Questions code asks the page reviewer: what one view of a page says and another does not
(plan rows M3, M1 and M9, Anant 2026-11-01 "build it").

Why: on the four sheets GV marked for the test, the reviewer looked at the very spot of about
half of GV's issues and said nothing (the failure map of 2026-10-31), and after the tiles came in
its first message it stopped after 7-11 calls with money left (the run of 2026-11-01): its coverage was met
and nothing told it what to compare. GV's reviewer compares views: the toe kick tagged in the
section and not in the elevation, "BIN BY GC/OWNER" in the front view and only "BIN" in the
section, the overall height in two elevations and not the third, a section with fewer depth parts
than the same cabinet's section on another sheet, a detail's rail thickness missing from the
elevation's height string. Models are poor at finding such mismatches alone and good at answering
them when handed the expected list (the "what is missing" study: recall 0.32 -> 0.92).

So code reads the page's text layer (and its line work, when it can be read) and builds:

1. **Views** (`views_of`): each view is one connected drawing of the line work (`page_views`, the
   one splitter `dim_parts` uses too), named by the view title under or in it ("FRONT VIEW",
   "SECTION VIEW" with its "S1" bubble, "TOP PLAN", "DETAIL 01"); an untitled drawing is named by
   where it is. Without line work, the titles alone
   split the page. Each phrase belongs to the drawing it lies in or next to (`ASSIGN_PT`); the
   title block, the material table and loose notes belong to none.
2. **A part x view ledger** (`Ledger`): per view, the part callouts (top, splash, toe kick, end
   panel, filler, door, drawer, shelf, panel, bin, blocking...), the finish tag on each (or none),
   the "who supplies it" words on each (by GV, GC, owner, others), and the inch values printed,
   with their direction (across the page, or turned).
3. **Questions** (`questions`), ranked by how often such a difference was a real issue, at most
   `MAX_QUESTIONS`, numbered Q1..Qn, each with the boxes it concerns:
   - a part tagged in one callout and not in another (another view, or the same one);
   - a part with "who supplies it" words in one view and none (or other words) in another;
   - a section or elevation with no finish tag while the page's other views tag their parts;
   - a section's depth make-up (`Makeup`: the printed depth parts that add up to its printed
     overall depth) with fewer parts than a sister section with the same overall: the same item's
     other drawing, the same item family, or another sheet of the set (`sister_makeups`), or no
     printed parts that add up to its overall;
   - a detail's thickness missing from an elevation's height dimensions;
   - the tallest overall height printed in some elevations and not in another;
   - a tag printed bare on this page that other sheets print with what it is and who supplies it;
   - a size in one view only, with a size within an inch of it in another view only;
   - a part tagged or supplied in one view and not called out in another elevation or section.

The reviewer settles each question: `report_issue` with `question: "Qn"`, or `answer_question`
"ok" with a short reason (`answer`, `mark_reported`); while questions are open and its money is
not used, the stop-hook sends it back with their numbers, at most `MAX_QUESTION_NUDGES` times
(`still_open`, `nudge`). The run's log counts them (`log_fields`).

**Never red.** A question is a lead from the vendor's own drawing text (never GV's answers or
markup: the drawings-only copy); what the reviewer reports from it is a please-check like every
other page-review issue. Code's arithmetic on code-read numbers stays the only red path.

General, not tuned to one job: no project names, sheet numbers or values; made-up values in tests
(`tests/review/assistant/test_view_questions.py`).
"""

from __future__ import annotations

import itertools
import logging
import math
import re
import threading
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from typing import Any, Final

from app.review.assistant import page_views
from app.review.assistant.set_context import SCOPE, family_of, item_key, tags_in
from app.review.assistant.untrusted import fence

__all__ = [
    "ANSWER_QUESTION",
    "MAX_QUESTIONS",
    "MAX_QUESTION_NUDGES",
    "Callout",
    "Ledger",
    "Makeup",
    "Question",
    "Value",
    "View",
    "answer",
    "inch_values",
    "ledger_of",
    "listing",
    "log_fields",
    "makeups",
    "mark_reported",
    "nudge",
    "opening_blocks",
    "questions",
    "still_open",
    "views_of",
]

_log = logging.getLogger(__name__)

Box = tuple[float, float, float, float]

#: Questions in the first message at most (the most likely first), and the stop-hook's sends.
MAX_QUESTIONS: Final = 25
MAX_QUESTION_NUDGES: Final = 3
#: The page's views come from `page_views` (one splitter for every check, 2026-11-01): a phrase
#: belongs to the view it lies in, else to the nearest one within `ASSIGN_PT` (points).
ASSIGN_PT: Final = page_views.ASSIGN_PT
#: Without line work, a phrase belongs to the nearest title within this (points).
TITLE_REACH_PT: Final = 400.0
#: An untitled drawing at most this big on both sides is a detail.
DETAIL_PT: Final = 220.0
#: A detail's thickness is at most this (inches); a size in one view only is compared with sizes
#: at least `NEAR_LEAST` and at most `NEAR_INCH` apart (inches), each at least `NEAR_FROM` long.
THICKNESS_IN: Final = Decimal(3)
NEAR_FROM: Final = Decimal(6)
NEAR_LEAST: Final = Decimal(1) / Decimal(16)
NEAR_INCH: Final = Decimal(1)
#: Parts that add up to an overall: within this (inches), at most `MAX_PARTS` of them, from at most
#: `MAX_DEPTH_VALUES` printed values of one section.
SUM_ROUNDING: Final = Decimal(1) / Decimal(32)
MAX_PARTS: Final = 4
MAX_DEPTH_VALUES: Final = 14
#: Other sheets looked at for a sister section, at most (`sister_makeups`).
MAX_SISTER_PAGES: Final = 8
#: A part callout is at most this many words (a longer phrase is a note).
CALLOUT_WORDS: Final = 10

#: How likely each kind of question was a real issue on GV's marked sheets (the order they are
#: listed in, most likely first; ties by place on the page).
_RANK: Final[Mapping[str, float]] = {
    "tag": 0.90,
    "supplier": 0.85,
    "untagged_view": 0.80,
    "depth": 0.75,
    "thickness": 0.70,
    "height": 0.70,
    "bare_tag": 0.60,
    "near": 0.55,
    "missing_tagged": 0.50,
    "missing_supplied": 0.45,
}

#: Part words, the more specific first (each match is taken out before the next is looked for).
_PARTS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (name, re.compile(rf"(?<![A-Z]){pattern}(?![A-Z])"))
    for name, pattern in (
        ("TOE KICK", r"TOE\s*-?\s*KICK"),
        ("END PANEL", r"(?:END\s*PANEL|FINISHED\s*END)S?"),
        ("SPLASH", r"(?:BACK\s*|SIDE\s*)?SPLASH(?:ES)?"),
        ("TOP", r"(?:COUNTER\s*-?\s*|STONE\s*|QUARTZ\s*)?TOPS?"),
        ("FILLER", r"(?:FILLERS?|SCRIBE)"),
        ("VALANCE", r"VALANCES?"),
        ("SHELF", r"(?:SHELF|SHELVES)"),
        ("DOOR", r"DOORS?"),
        ("DRAWER", r"DRAWERS?(?!\s*SLIDES?)"),
        ("PANEL", r"PANELS?"),
        ("FASCIA", r"FASCIAS?"),
        ("APRON", r"APRONS?"),
        ("BIN", r"BINS?"),
        ("BLOCKING", r"(?:BLOCKING|BACKING)"),
        ("CLEAT", r"CLEATS?"),
        ("LEG", r"LEGS?"),
        ("SINK", r"SINKS?"),
        ("FAUCET", r"FAUCETS?"),
    )
)
_TITLE: Final = re.compile(r"\b(VIEW|ELEVATION|SECTION|PLAN|DETAIL)\b", re.IGNORECASE)
_TITLE_BLOCK: Final = re.compile(
    r"\b(?:SCALE|SHEET|DATE|DRAWN|CHECKED|APPROVED|PROJECT|PHONE|FAX|PAGE|DWG|REVISIONS?|"
    r"COPYRIGHT)\b",
    re.IGNORECASE,
)
_FINISH_ITEM_NUMBER: Final = re.compile(r"^[A-Z]{2,5}-\d{3}")
_SUPPLIER: Final = re.compile(
    r"\bBY\s+((?:G\.?C\.?|G\.?V\.?|OWNER|OTHERS?|OTHER\s+TRADES?|GENERAL\s+CONTRACTOR|"
    r"MILLWORK|VENDOR)(?:\s*/\s*(?:OWNER|G\.?C\.?|OTHERS?))?)",
    re.IGNORECASE,
)
#: A bracketed value (inches after millimetres) and a phrase that is only a quoted size.
_BRACKET: Final = re.compile(r"\[([^\[\]]*\d[^\[\]]*)\]")
_QUOTED_ONLY: Final = re.compile(r"""^\s*\d[\d\s/'-]*(?:"|'')\s*$""")


# ---- reading the text layer ---------------------------------------------------------------------


def _box(phrase: Mapping[str, Any]) -> Box | None:
    raw = phrase.get("box")
    if isinstance(raw, Mapping):
        try:
            return (float(raw["x0"]), float(raw["y0"]), float(raw["x1"]), float(raw["y1"]))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, Sequence) and not isinstance(raw, str) and len(raw) == 4:
        try:
            return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        except (TypeError, ValueError):
            return None
    return None


def _turned(box: Box, text: str = "") -> bool:
    """Printed up the page: a tall box, or brackets read backwards (`]4/3[`: the text layer reads a
    turned size from its far end)."""
    if (box[3] - box[1]) > 1.5 * (box[2] - box[0]):
        return True
    first = min((text.find(c) for c in "[]" if c in text), default=-1)
    return first >= 0 and text[first] == "]"


def _squash(text: str) -> str:
    """Letter-spaced text joined ("E N D P ANEL" -> "ENDPANEL", "W D - 0 1" -> "WD-01"): when at
    least three of its pieces, and at least half, are one character."""
    pieces = text.split()
    single = sum(1 for p in pieces if len(p) == 1)
    if single >= 3 and single * 2 >= len(pieces):
        return "".join(pieces)
    return " ".join(pieces)


def _unbalanced(text: str) -> bool:
    return text.count("[") != text.count("]")


def _joined(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The phrases with their broken pieces put back: pieces on one line that touch (the text layer
    splits a spaced word), and a turned value's pieces one above the other when a bracket is left
    open (`]2/1` over `23[` is `[32 1/2]` read backwards)."""
    items: list[dict[str, Any]] = []
    for phrase in found:
        box = _box(phrase)
        if box is not None and str(phrase.get("text") or "").strip():
            items.append({"text": str(phrase["text"]).strip(), "box": box})
    items.sort(key=lambda p: (p["box"][1], p["box"][0]))
    out: list[dict[str, Any]] = []
    for item in items:
        b = item["box"]
        for last in reversed(out[-12:]):
            lb = last["box"]
            same_line = abs((lb[1] + lb[3]) / 2 - (b[1] + b[3]) / 2) <= 2.0
            if same_line and 0 <= b[0] - lb[2] <= 1.5 and not _turned(b):
                last["text"] += " " + item["text"]
                last["box"] = (lb[0], min(lb[1], b[1]), b[2], max(lb[3], b[3]))
                break
        else:
            out.append(dict(item))
    # A turned value's pieces: one under the other, sharing their column, a bracket left open.
    merged: list[dict[str, Any]] = []
    used: set[int] = set()
    for i, item in enumerate(out):
        if i in used:
            continue
        b = item["box"]
        if _turned(b) and _unbalanced(item["text"]):
            for j in range(i + 1, len(out)):
                if j in used:
                    continue
                o = out[j]["box"]
                overlap = min(b[2], o[2]) - max(b[0], o[0])
                if overlap >= 0.5 * min(b[2] - b[0], o[2] - o[0]) and 0 <= o[1] - b[3] <= 3.0:
                    if _unbalanced(out[j]["text"]):
                        item = {
                            "text": item["text"] + " " + out[j]["text"],
                            "box": (min(b[0], o[0]), b[1], max(b[2], o[2]), o[3]),
                        }
                        used.add(j)
                    break
        merged.append(item)
    for item in merged:
        item["text"] = _squash(item["text"])
    return merged


def inch_values(text: str) -> list[Decimal]:
    """The inch sizes a phrase prints: in brackets after millimetres (`610 [24]`, read backwards
    when the text layer turned it: `]4/3[` is 3/4, `]2/1 23[` is 32 1/2), or a phrase that is only
    a quoted size (`24"`). A size inside a note (`5" filler`) is not one."""
    from app.review.assistant.finding_checks import to_inches

    raw = str(text or "").replace("''", '"').replace("″", '"')
    first = min((raw.find(c) for c in "[]" if c in raw), default=-1)
    if first >= 0 and raw[first] == "]":
        raw = raw[::-1]
    out: list[Decimal] = []
    for found in _BRACKET.findall(raw):
        value = to_inches(" ".join(found.replace('"', " ").replace("'", " ").split()))
        if value is not None and value > 0:
            out.append(value)
    if not out and "[" not in raw and _QUOTED_ONLY.match(raw):
        value = to_inches(raw.strip())
        if value is not None and value > 0:
            out.append(value)
    return out


def _parts_in(text: str) -> list[str]:
    upper = text.upper()
    found: list[str] = []
    for name, pattern in _PARTS:
        if pattern.search(upper):
            found.append(name)
            upper = pattern.sub(" ", upper)
    return found


def _finish_tags(text: str) -> list[str]:
    """Finish codes in a text (WD-01, ST-01, MT-001.C), not item numbers (ABC-801.C)."""
    return [
        tag
        for tag in tags_in(text)
        if "-" in tag and len(family_of(tag) or "") >= 2 and not _FINISH_ITEM_NUMBER.match(tag)
    ]


def _supplier(text: str) -> str | None:
    found = _SUPPLIER.search(text)
    if found is not None:
        return re.sub(r"[\s.]", "", found.group(1)).upper()
    found = SCOPE.search(text)
    return re.sub(r"\s+", " ", found.group(0)).upper() if found is not None else None


def _inch(value: Decimal) -> str:
    from app.review.assistant.chat_trust import inch_words

    return inch_words(value).rstrip('"')


def _b(box: Sequence[float]) -> str:
    return ",".join(f"{v:.0f}" for v in box)


# ---- the views ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class View:
    """One drawing on the page: its name (its printed title, or where it is), its kind (elevation,
    section, plan, detail, or view when untitled) and its box (page points)."""

    name: str
    kind: str
    box: Box
    title: Box | None = None


def _gap(a: Sequence[float], b: Sequence[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _centre(b: Sequence[float]) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _kind_of(title: str) -> str:
    upper = title.upper()
    if "SECTION" in upper:
        return "section"
    if "DETAIL" in upper:
        return "detail"
    if "PLAN" in upper or re.search(r"\bTOP\s+VIEW\b", upper):
        return "plan"
    return "elevation"


def names_part(text: str) -> bool:
    """A part callout or a finish tag: never a view's title (`page_views.titles`)."""
    return bool(_parts_in(text) or _finish_tags(text))


@dataclass(frozen=True, slots=True)
class Split:
    """One page split into views (`page_split`): the drawings (each piece of line's view), the
    titles, which view each title names (`owners`, view index -> titles) and the phrases the
    titles are made of (`members`, `id`s of `items`)."""

    drawings: page_views.Drawings
    titles: list[tuple[str, Box]]
    owners: dict[int, list[tuple[str, Box]]]
    members: set[int]


def phrase_items(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The page's phrases as `{text, box}` with their broken pieces put back (`_joined`)."""
    return _joined(found)


def page_split(
    items: Sequence[Mapping[str, Any]],
    segments: Sequence[Any] | None,
    size: tuple[float, float] | None = None,
) -> Split:
    """The one way a page is split into views, for these questions and for `dim_parts` (2026-11-01):
    `page_views`' drawings from the line work, its titles read from `items` (`phrase_items`; a part
    callout or a finish tag is no title), and each title's view, a view's box grown by its own
    sizes, tags and part words first (so a title under that text is still under the view). Without
    line work there are no drawings, only the titles."""
    found_titles, members = page_views.titles(items, names_part)
    drawn = page_views.drawings(segments, size) if segments else page_views.Drawings()
    texts = [
        item["box"]
        for item in items
        if id(item) not in members
        and not _TITLE_BLOCK.search(str(item["text"]))
        and (
            inch_values(str(item["text"]))
            or _finish_tags(str(item["text"]))
            or _parts_in(str(item["text"]))
        )
    ]
    owners = page_views.owners(drawn.boxes, found_titles, texts) if drawn.boxes else {}
    return Split(drawn, found_titles, owners, members)


def views_of(
    found: Sequence[Mapping[str, Any]],
    segments: Sequence[Any] | None = None,
    size: tuple[float, float] | None = None,
) -> tuple[list[View], list[tuple[dict[str, Any], int | None]]]:
    """The page's views (`View`) and each phrase (`_joined`) with the view it belongs to (`None`:
    the title block, the material table, a loose note)."""
    items = _joined(found)
    split = page_split(items, segments, size)
    free = {
        id(item)
        for item in items
        if id(item) not in split.members and not _TITLE_BLOCK.search(item["text"])
    }
    blobs = split.drawings.boxes
    if not blobs:
        titled = [View(name, _kind_of(name), tbox, tbox) for name, tbox in split.titles]
        by_title = [
            (item, _by_title(item["box"], titled) if id(item) in free else None) for item in items
        ]
        return [_classified(v, by_title, n) for n, v in enumerate(titled)], by_title
    taken = split.owners
    views: list[View] = []
    of_blob: list[int] = []
    for i, blob in enumerate(blobs):
        named = taken.get(i, [])
        for name, tbox in named:
            views.append(View(name, _kind_of(name), blob, tbox))
            of_blob.append(i)
        if not named:
            views.append(View(f"the untitled view at {_b(blob)}", "view", blob))
            of_blob.append(i)
    assigned: list[tuple[dict[str, Any], int | None]] = []
    for item in items:
        if id(item) not in free:
            assigned.append((item, None))
            continue
        n = page_views.nearest(item["box"], blobs)
        if n is None:
            # Between a drawing and its title (an overall dimension under the view).
            c = _centre(item["box"])
            n = next(
                (
                    of_blob[k]
                    for k, v in enumerate(views)
                    if v.title is not None
                    and v.box[0] <= c[0] <= v.box[2]
                    and v.box[3] <= c[1] <= v.title[1]
                ),
                None,
            )
        if n is None:
            assigned.append((item, None))
            continue
        mine = [k for k, b in enumerate(of_blob) if b == n]
        if len(mine) > 1:  # two titled views on one drawing: the nearer title
            mine.sort(key=lambda k: _gap(item["box"], views[k].title or views[k].box))
        assigned.append((item, mine[0]))
    # An untitled drawing with no size or part printed in it is a picture or a logo, not a view.
    used = {
        v
        for item, v in assigned
        if v is not None and (inch_values(item["text"]) or _parts_in(item["text"]))
    }
    keep = [i for i, view in enumerate(views) if view.title is not None or i in used]
    renumber = {old: new for new, old in enumerate(keep)}
    views = [views[i] for i in keep]
    assigned = [(item, renumber.get(v) if v is not None else None) for item, v in assigned]
    return [_classified(view, assigned, n) for n, view in enumerate(views)], assigned


def _by_title(box: Box, views: Sequence[View]) -> int | None:
    """Without line work: the nearest title, a phrase above its title counting half as far (a
    title sits under its drawing), within `TITLE_REACH_PT`."""
    centre = _centre(box)
    best: tuple[float, int] | None = None
    for i, view in enumerate(views):
        t = view.box
        dx = max(0.0, t[0] - centre[0], centre[0] - t[2])
        above = t[1] - centre[1]
        dy = above / 2 if above > 0 else max(0.0, centre[1] - t[3])
        distance = math.hypot(dx, dy)
        if best is None or distance < best[0]:
            best = (distance, i)
    return best[1] if best is not None and best[0] <= TITLE_REACH_PT else None


def _classified(view: View, assigned: Sequence[tuple[dict[str, Any], int | None]], n: int) -> View:
    """An untitled drawing's kind: small = a detail; long and shallow with no tall size = a plan."""
    if view.title is not None:
        return view
    w, h = view.box[2] - view.box[0], view.box[3] - view.box[1]
    if w <= DETAIL_PT and h <= DETAIL_PT:
        return View(f"the untitled detail at {_b(view.box)}", "detail", view.box)
    tall = [
        value
        for item, v in assigned
        if v == n and _turned(item["box"], str(item["text"]))
        for value in inch_values(item["text"])
    ]
    if h < 0.3 * w and max(tall, default=Decimal(0)) <= 30:
        return View(f"the untitled plan at {_b(view.box)}", "plan", view.box)
    return view


# ---- the ledger ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Callout:
    """A part callout in one view: its words, its parts, its finish tags and who supplies it."""

    text: str
    box: Box
    view: int
    parts: tuple[str, ...]
    tags: tuple[str, ...]
    supplier: str | None


@dataclass(frozen=True, slots=True)
class Value:
    """An inch size printed in one view, across the page or turned (`turned`)."""

    inches: Decimal
    box: Box
    view: int
    turned: bool


@dataclass(slots=True)
class Ledger:
    """One page's views and, per view, its part callouts, finish tags and inch sizes."""

    views: list[View]
    callouts: list[Callout] = field(default_factory=list)
    values: list[Value] = field(default_factory=list)
    tags: list[tuple[str, Box, int]] = field(default_factory=list)
    """Every finish tag printed in a view (callout or not): (tag, box, view)."""
    bare: list[tuple[str, Box, int]] = field(default_factory=list)
    """Finish tags printed alone (no part word, no other word)."""

    def in_view(self, n: int) -> list[Callout]:
        return [c for c in self.callouts if c.view == n]

    def sizes(self, n: int, turned: bool | None = None) -> list[Value]:
        return [v for v in self.values if v.view == n and (turned is None or v.turned == turned)]


def ledger_of(
    found: Sequence[Mapping[str, Any]],
    segments: Sequence[Any] | None = None,
    size: tuple[float, float] | None = None,
) -> Ledger:
    """The part x view ledger of one page (code only, no AI)."""
    views, assigned = views_of(found, segments, size)
    ledger = Ledger(views)
    placed = [(item, v) for item, v in assigned if v is not None]
    for item, v in placed:
        assert v is not None
        text, box = str(item["text"]), item["box"]
        for value in inch_values(text):
            ledger.values.append(Value(value, box, v, _turned(box, text)))
        for tag in _finish_tags(text):
            ledger.tags.append((tag, box, v))
            if text.strip() == tag:
                ledger.bare.append((tag, box, v))
    # A callout is its first line and the lines right under it (a lone tag right above it too),
    # wherever the drawings' boxes fall: its view is its first line's.
    consumed: set[int] = set()
    for item, v in placed:
        assert v is not None
        text, box = str(item["text"]), item["box"]
        words = len(text.split())
        if id(item) in consumed or not _parts_in(text) or words > CALLOUT_WORDS:
            continue
        if _TITLE.search(text) and words <= 3:
            continue
        lines = [text]
        last: Box | None = box
        tagged = bool(_finish_tags(text))
        for other, _ov in placed:
            ob, words_there = other["box"], str(other["text"])
            if other is item or id(other) in consumed or len(lines) > 3:
                continue
            lone_tag = _finish_tags(words_there) == [words_there.strip()]
            below = (
                last is not None
                and 0 <= ob[1] - last[3] <= 4.0
                and (abs(ob[0] - box[0]) <= 40 or _gap(ob, last) <= 2)
                and len(words_there.split()) <= CALLOUT_WORDS
                and not (lone_tag and tagged)
            )
            above = 0 <= box[1] - ob[3] <= 4.0 and lone_tag and not tagged
            if below or above:
                lines.append(words_there)
                consumed.add(id(other))
                tagged = tagged or bool(_finish_tags(words_there))
                # One tag ends a callout: a column of tags under it belongs to other parts.
                last = None if lone_tag else (ob if below else last)
        joined = " ".join(lines)
        ledger.callouts.append(
            Callout(
                " / ".join(lines)[:90],
                box,
                v,
                tuple(dict.fromkeys(_parts_in(joined))),
                tuple(dict.fromkeys(_finish_tags(joined))),
                _supplier(joined),
            )
        )
    return ledger


# ---- depth make-up (M1) -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Makeup:
    """A section's depth: its printed overall (the longest size across it), the printed parts that
    add up to it (the most of them, `parts`; empty when none do) and every depth size it prints."""

    view: str
    overall: Decimal
    parts: tuple[Decimal, ...]
    values: tuple[Decimal, ...]
    box: Box
    page: int | None = None
    item: str | None = None
    tier: int = 0
    """How near its kin is (`_kin_pages`): 0 the same item, 1 the same family, 2 another sheet."""


def makeups(
    ledger: Ledger, page: int | None = None, item: str | None = None, tier: int = 0
) -> list[Makeup]:
    """Each section's depth make-up. In a section, depth runs across the page."""
    out: list[Makeup] = []
    for n, view in enumerate(ledger.views):
        if view.kind != "section":
            continue
        across = ledger.sizes(n, turned=False)
        if not across:
            continue
        overall = max(v.inches for v in across)
        if overall < NEAR_FROM:
            continue
        smaller = [v.inches for v in across if v.inches < overall][:MAX_DEPTH_VALUES]
        best: tuple[Decimal, ...] = ()
        for count in range(min(MAX_PARTS, len(smaller)), 1, -1):
            for combo in itertools.combinations(smaller, count):
                if abs(sum(combo, Decimal(0)) - overall) <= SUM_ROUNDING:
                    best = tuple(sorted(combo, reverse=True))
                    break
            if best:
                break
        out.append(
            Makeup(
                view.name,
                overall,
                best,
                tuple(sorted(set(smaller), reverse=True)),
                view.box,
                page,
                item,
                tier,
            )
        )
    return out


# ---- the questions ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Question:
    """One question for the reviewer: its number (Qn), the kind of issue it would be, its words,
    the boxes it concerns, and what made it (`made_by`, for the log and the ranking)."""

    n: int
    kind: str
    text: str
    boxes: tuple[Box, ...]
    made_by: str

    @property
    def label(self) -> str:
        return f"Q{self.n}"


def _where(view: View) -> str:
    """A view's name with its box (an untitled view's name already says where it is)."""
    return view.name if view.title is None else f"{view.name} at {_b(view.box)}"


def _names(ledger: Ledger, views: Sequence[int]) -> str:
    return ", ".join(ledger.views[v].name for v in dict.fromkeys(views))


def _list(values: Sequence[Decimal]) -> str:
    return ", ".join(_inch(v) for v in values)


def _draft(
    kind: str, made_by: str, text: str, boxes: Sequence[Sequence[float]], at: Sequence[float]
) -> tuple[float, float, float, str, str, tuple[Box, ...]]:
    shown = tuple(
        (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        for b in list(dict.fromkeys(map(tuple, boxes)))[:4]
    )
    return (-_RANK[made_by], at[1], at[0], kind, made_by + "\x00" + text, shown)


def questions(
    ledger: Ledger,
    sisters: Sequence[Makeup] = (),
    elsewhere: Mapping[str, tuple[str, int]] | None = None,
    kinds: Collection[str] | None = None,
) -> list[Question]:
    """The questions for one page, most likely first, at most `MAX_QUESTIONS`, numbered from 1.
    `sisters`: other sheets' sections (`sister_makeups`, the nearest kin first); `elsewhere`: for a
    finish tag, how another sheet prints it in full and on which page (`full_callouts`); `kinds`:
    only questions of these kinds (`None`: every kind), chosen before the cap, so a reviewer of one
    kind (the sizes lens, `sizes_lens`) gets all of that kind's questions."""
    drafts = (
        _tag_questions(ledger)
        + _supplier_questions(ledger)
        + _untagged_views(ledger)
        + _depth_questions(ledger, sisters)
        + _thickness_questions(ledger)
        + _height_questions(ledger)
        + _bare_tag_questions(ledger, elsewhere or {})
        + _near_questions(ledger)
        + _missing_questions(ledger)
    )
    if kinds is not None:
        drafts = [d for d in drafts if d[3] in kinds]
    drafts.sort(key=lambda d: d[:3])
    out: list[Question] = []
    seen: set[str] = set()
    for _rank, _y, _x, kind, keyed, boxes in drafts:
        made_by, text = keyed.split("\x00", 1)
        if text in seen:
            continue
        seen.add(text)
        out.append(Question(len(out) + 1, kind, text, boxes, made_by))
        if len(out) >= MAX_QUESTIONS:
            break
    return out


_Draft = tuple[float, float, float, str, str, tuple[Box, ...]]


def _by_part(ledger: Ledger) -> dict[str, list[Callout]]:
    out: dict[str, list[Callout]] = {}
    for callout in ledger.callouts:
        for part in callout.parts:
            out.setdefault(part, []).append(callout)
    return out


def _tag_questions(ledger: Ledger) -> list[_Draft]:
    """A part tagged in one callout and not in another (another view, or the same one)."""
    out: list[_Draft] = []
    for part, callouts in _by_part(ledger).items():
        tagged = [c for c in callouts if c.tags]
        bare = [c for c in callouts if not c.tags]
        if not tagged or not bare:
            continue
        tags = sorted({t for c in tagged for t in c.tags})
        where = "; ".join(
            f"{ledger.views[c.view].name} '{c.text}' at {_b(c.box)}" for c in tagged[:2]
        )
        for c in bare[:3]:
            text = (
                f"{part}: tagged {', '.join(tags)} in {where}; in {ledger.views[c.view].name} its "
                f"callout '{c.text}' at {_b(c.box)} has no finish tag. The same part: tag it the "
                "same here, or is it another part?"
            )
            out.append(_draft("D", "tag", text, [c.box, tagged[0].box], c.box))
    return out


def _supplier_questions(ledger: Ledger) -> list[_Draft]:
    """A part with "who supplies it" words in one view and none (or others) in another."""
    out: list[_Draft] = []
    for part, callouts in _by_part(ledger).items():
        views: dict[int, set[str | None]] = {}
        for c in callouts:
            views.setdefault(c.view, set()).add(c.supplier)
        said = {v: {s for s in who if s} for v, who in views.items()}
        saying = {v: s for v, s in said.items() if s}
        if not saying:
            continue
        words = sorted({s for s in itertools.chain.from_iterable(saying.values())})
        source = next(c for c in callouts if c.supplier)
        for v, who in said.items():
            if who and set(who) == set(words):
                continue
            here = [c for c in callouts if c.view == v]
            if not who:
                text = (
                    f"{part}: {ledger.views[source.view].name} says who supplies it "
                    f"('{source.text}' at {_b(source.box)}); in {ledger.views[v].name} "
                    f"'{here[0].text}' at {_b(here[0].box)} does not. Say it here too?"
                )
            elif len(words) > 1:
                text = (
                    f"{part}: the views name different suppliers ({' / '.join(words)}); "
                    f"{ledger.views[v].name} '{here[0].text}' at {_b(here[0].box)}. Which is it?"
                )
            else:
                continue
            out.append(_draft("E", "supplier", text, [here[0].box, source.box], here[0].box))
    return out


def _untagged_views(ledger: Ledger) -> list[_Draft]:
    """A section or elevation with no finish tag while the page's other views tag their parts."""
    out: list[_Draft] = []
    tagged = {v for _t, _b2, v in ledger.tags}
    if not tagged:
        return out
    for n, view in enumerate(ledger.views):
        if view.kind not in ("section", "elevation", "view") or n in tagged:
            continue
        if not ledger.sizes(n) and not ledger.in_view(n):
            continue
        others = sorted({t for t, _b2, v in ledger.tags if v != n})
        text = (
            f"{_where(view)} prints no finish tag; the page's other views tag "
            f"{', '.join(others[:6])}. Are its top, panels and other visible parts tagged?"
        )
        out.append(_draft("D", "untagged_view", text, [view.box], view.box))
    return out


def _depth_questions(ledger: Ledger, sisters: Sequence[Makeup]) -> list[_Draft]:
    """A section's depth make-up against a sister section with the same overall, or against its own
    overall (M1)."""
    out: list[_Draft] = []
    for here in makeups(ledger):
        kin = [s for s in sisters if s.overall == here.overall and len(s.parts) > len(here.parts)]
        if kin:
            # The same item's other drawing, or a make-up at least two other sections print (the
            # house's way of building it; one odd section is no pattern).
            common = Counter(s.parts for s in kin)
            kin = [s for s in kin if s.tier == 0 or common[s.parts] >= 2]
            if not kin:
                continue
            nearest = min(s.tier for s in kin)
            kin = [s for s in kin if s.tier == nearest]
            common = Counter(s.parts for s in kin)
            parts = max(common, key=lambda p: (common[p], len(p)))
            best = next(s for s in kin if s.parts == parts)
            more = common[parts] - 1
            missing = [v for v in best.parts if v not in here.values]
            place = f"p{best.page} " if best.page is not None else ""
            item = f" ({best.item})" if best.item else ""
            mine = (
                " + ".join(_inch(v) for v in here.parts) + f" = {_inch(here.overall)}"
                if here.parts
                else f"no printed parts that add up to {_inch(here.overall)}"
            )
            also = f" and {more} more" if more else ""
            text = (
                f"Depth make-up of {here.view} at {_b(here.box)}: here {mine} "
                f"(depth sizes printed: {_list(here.values) or 'none'}); the sister section "
                f"{place}{best.view}{item}{also} with the same {_inch(best.overall)} overall print "
                + " + ".join(_inch(v) for v in best.parts)
                + f" = {_inch(best.overall)}"
                + (f" (not printed here: {_list(missing)})" if missing else "")
                + ". Are depth parts missing here?"
            )
            out.append(_draft("A", "depth", text, [here.box], here.box))
        elif not here.parts and here.values:
            text = (
                f"Depth of {here.view} at {_b(here.box)}: overall {_inch(here.overall)}; no printed "
                f"depth sizes add up to it (printed: {_list(here.values)}). Is a depth part "
                "missing?"
            )
            out.append(_draft("A", "depth", text, [here.box], here.box))
    return out


def _thickness_questions(ledger: Ledger) -> list[_Draft]:
    """A detail's thickness missing from an elevation's height sizes (both on this page)."""
    out: list[_Draft] = []
    details = [n for n, v in enumerate(ledger.views) if v.kind == "detail"]
    rising = [n for n, v in enumerate(ledger.views) if v.kind in ("elevation", "view")]
    for d in details:
        thin = sorted({v.inches for v in ledger.sizes(d) if v.inches <= THICKNESS_IN}, reverse=True)
        if not thin:
            continue
        for e in rising:
            heights = [v for v in ledger.sizes(e, turned=True)]
            if not heights:
                continue
            printed = {v.inches for v in heights}
            lacking = [t for t in thin if t not in printed]
            if not lacking:
                continue
            shown = sorted(printed)
            text = (
                f"{_where(ledger.views[d])} prints "
                f"{_list(thin)}; {ledger.views[e].name}'s height sizes print {_list(shown)}, "
                f"not {_list(lacking)}. Is the detail's thickness carried into the height string "
                "(do its parts still add up to the overall)?"
            )
            box = min(heights, key=lambda v: -v.inches).box
            out.append(
                _draft("A", "thickness", text, [box, ledger.views[d].box], ledger.views[e].box)
            )
    return out


def _height_questions(ledger: Ledger) -> list[_Draft]:
    """The tallest overall height printed in some elevations and not in another."""
    rising = [n for n, v in enumerate(ledger.views) if v.kind in ("elevation", "view")]
    tallest: dict[int, Decimal] = {}
    for n in rising:
        heights = [v.inches for v in ledger.sizes(n, turned=True)]
        if heights:
            tallest[n] = max(heights)
    if len(tallest) < 2:
        return []
    top = max(tallest.values())
    having = [n for n, h in tallest.items() if h == top]
    out: list[_Draft] = []
    for n, h in tallest.items():
        if h == top or top in {v.inches for v in ledger.sizes(n)}:
            continue
        box = max(ledger.sizes(n, turned=True), key=lambda v: v.inches).box
        text = (
            f"Overall height: {_names(ledger, having)} print {_inch(top)}; "
            f"{ledger.views[n].name}'s tallest height size is {_inch(h)} (at {_b(box)}). Is its "
            "overall height missing?"
        )
        out.append(_draft("A", "height", text, [ledger.views[n].box], ledger.views[n].box))
    return out


def _bare_tag_questions(ledger: Ledger, elsewhere: Mapping[str, tuple[str, int]]) -> list[_Draft]:
    """A finish tag printed alone here that another sheet prints with what it is and who supplies
    it."""
    out: list[_Draft] = []
    worded = {t for c in ledger.callouts for t in c.tags}
    for tag in dict.fromkeys(t for t, _b2, _v in ledger.bare):
        if tag in worded or tag not in elsewhere:
            continue
        boxes = [b for t, b, _v in ledger.bare if t == tag]
        wording, page = elsewhere[tag]
        text = (
            f"{tag} is printed alone here ({len(boxes)} callout{'s' if len(boxes) > 1 else ''}, "
            f"first at {_b(boxes[0])}); p{page} prints '{wording}'. Should the callouts here say "
            "what it is and who supplies it?"
        )
        out.append(_draft("D", "bare_tag", text, boxes, boxes[0]))
    return out


#: What a size measures by the view's kind and its direction (across the page, turned): a plan's
#: turned sizes are depths, a section's sizes across are depths, an elevation's turned are heights.
_AXIS: Final[Mapping[tuple[str, bool], str]] = {
    ("plan", False): "width",
    ("plan", True): "depth",
    ("elevation", False): "width",
    ("elevation", True): "height",
    ("view", False): "width",
    ("view", True): "height",
    ("section", False): "depth",
    ("section", True): "height",
}


def _near_questions(ledger: Ledger) -> list[_Draft]:
    """A size printed in one view only, with a size of the same measure (width, depth, height)
    within an inch of it in another view only."""
    out: list[_Draft] = []
    kinds = [n for n, v in enumerate(ledger.views) if v.kind != "detail"]
    for a, b in itertools.combinations(kinds, 2):

        def measured(n: int) -> dict[tuple[str, Decimal], Value]:
            kind = ledger.views[n].kind
            return {
                (_AXIS[(kind, v.turned)], v.inches): v
                for v in ledger.sizes(n)
                if v.inches >= NEAR_FROM and (kind, v.turned) in _AXIS
            }

        here, there = measured(a), measured(b)
        printed_a = {v.inches for v in ledger.sizes(a)}
        printed_b = {v.inches for v in ledger.sizes(b)}
        pairs = [
            (here[x], there[y])
            for x in here
            for y in there
            if x[0] == y[0]
            and x[1] not in printed_b
            and y[1] not in printed_a
            and NEAR_LEAST <= abs(x[1] - y[1]) <= NEAR_INCH
        ]
        if not pairs:
            continue
        pairs.sort(key=lambda p: -p[0].inches)
        said = "; ".join(
            f"{_inch(p.inches)} at {_b(p.box)} vs {_inch(q.inches)} at {_b(q.box)}"
            for p, q in pairs[:3]
        )
        text = (
            f"Sizes in one view only: {ledger.views[a].name} vs {ledger.views[b].name}: {said}. "
            "The same thing sized two ways, or two different things?"
        )
        boxes = [box for p, q in pairs[:2] for box in (p.box, q.box)]
        out.append(_draft("A", "near", text, boxes, pairs[0][0].box))
    return out


def _missing_questions(ledger: Ledger) -> list[_Draft]:
    """A part tagged or supplied in one view and not called out in another elevation or section
    (the parts of one callout that lack the same views asked together)."""
    out: list[_Draft] = []
    targets = [n for n, v in enumerate(ledger.views) if v.kind in ("section", "elevation", "view")]
    grouped: dict[tuple[Callout, tuple[int, ...]], list[str]] = {}
    for part, callouts in _by_part(ledger).items():
        sources = [c for c in callouts if c.tags] or [c for c in callouts if c.supplier]
        if not sources:
            continue
        having = {c.view for c in callouts}
        missing = tuple(n for n in targets if n not in having)
        if missing:
            grouped.setdefault((sources[0], missing), []).append(part)
    for (source, lacking), parts in grouped.items():
        tagged = bool(source.tags)
        what = f"tagged {', '.join(source.tags)}" if tagged else f"by {source.supplier}"
        named = ", ".join(parts)
        text = (
            f"{named}: called out ({what}) in {ledger.views[source.view].name} ('{source.text}' "
            f"at {_b(source.box)}); not called out in {_names(ledger, lacking)}. Shown there and "
            "left without its callout?"
        )
        boxes = [ledger.views[n].box for n in lacking[:2]] + [source.box]
        out.append(
            _draft(
                "D" if tagged else "E",
                "missing_tagged" if tagged else "missing_supplied",
                text,
                boxes,
                source.box,
            )
        )
    return out


def listing(made: Sequence[Question]) -> str:
    """The questions, one a line, with their boxes."""
    return "\n".join(
        f"{q.label} [{q.kind}] {q.text} Boxes: " + "; ".join(_b(b) for b in q.boxes) for q in made
    )


# ---- the rest of the set ------------------------------------------------------------------------

_CACHE_LOCK: Final = threading.Lock()
#: Each other sheet's sections, read once per check (key: the set's context and the page).
_MAKEUP_CACHE: dict[tuple[int, str, int], list[Makeup]] = {}
_MAX_CACHED: Final = 400


def _kin_pages(context: Any, page: int) -> list[tuple[int, str | None, int]]:
    """The other sheets worth a sister section, nearest kin first: the item's other drawings
    (`pair_pages`), the same item family's sheets, then any other sheet that prints a section; at
    most `MAX_SISTER_PAGES`, each with the item tag it prints."""
    out: dict[int, tuple[str | None, int]] = {}
    for other, tag, _mine in context.pair_pages(page):
        out.setdefault(other, (tag, 0))
    mine = context.item_tags(page)
    families = {family_of(t) for t in mine if family_of(t)}
    keys = {item_key(t) for t in mine}
    sections: list[int] = []
    for number in sorted(context.pages):
        if number == page or number in out:
            continue
        found = context.pages.get(number) or []
        if not any("SECTION" in str(p.get("text") or "").upper() for p in found):
            continue
        theirs = context.item_tags(number)
        same = [t for t in theirs if family_of(t) in families and item_key(t) not in keys]
        if same:
            out.setdefault(number, (same[0], 1))
        else:
            sections.append(number)
    for number in sections:
        theirs = context.item_tags(number)
        out.setdefault(number, (theirs[0] if theirs else None, 2))
    return [(number, tag, tier) for number, (tag, tier) in out.items()][:MAX_SISTER_PAGES]


def sister_makeups(context: Any, page: int, segments_of: Any | None = None) -> list[Makeup]:
    """The sections of the other sheets kin to `page` (`_kin_pages`), nearest kin first.
    `segments_of(page)`: that page's line work (`None`: titles alone split it)."""
    out: list[Makeup] = []
    for other, tag, tier in _kin_pages(context, page):
        key = (id(context), str(getattr(context, "file", "")), other)
        with _CACHE_LOCK:
            known = _MAKEUP_CACHE.get(key)
        if known is None:
            segments = None
            if segments_of is not None:
                try:
                    segments = segments_of(other)
                except Exception:  # noqa: BLE001 - its titles split it instead
                    segments = None
            known = makeups(ledger_of(context.pages.get(other) or [], segments), other, tag, tier)
            with _CACHE_LOCK:
                if len(_MAKEUP_CACHE) > _MAX_CACHED:
                    _MAKEUP_CACHE.clear()
                _MAKEUP_CACHE[key] = known
        out += known
    return out


def full_callouts(context: Any, page: int, tags: Sequence[str]) -> dict[str, tuple[str, int]]:
    """For each tag, the commonest way another sheet prints it with a part word and who supplies it
    (the vendor's own words), and the first page that does."""
    out: dict[str, tuple[str, int]] = {}
    for tag in dict.fromkeys(tags):
        seen: Counter[str] = Counter()
        first: dict[str, int] = {}
        for number, found in sorted(context.pages.items()):
            if number == page:
                continue
            for phrase in found:
                text = " ".join(str(phrase.get("text") or "").split())
                if (
                    tag in tags_in(text)
                    and _parts_in(text)
                    and _supplier(text)
                    and len(text.split()) <= CALLOUT_WORDS
                ):
                    seen[text] += 1
                    first.setdefault(text, number)
        if seen:
            wording = seen.most_common(1)[0][0]
            out[tag] = (wording, first[wording])
    return out


# ---- in the page reviewer -----------------------------------------------------------------------

_HEAD: Final = (
    "Questions code made by comparing this page's views (and the same parts on the set's other "
    "sheets), from the vendor's own text. Each is a lead to check, not a finding. Settle every "
    'one as you go: an issue -> report_issue with question "Qn" (box where it shows on this '
    "page); fine -> answer_question with a short reason."
)


def _segments(run: Any, page: int) -> Any:
    """`page`'s line work, read by the reviewer's own reader (`_Run._lines`)."""
    if getattr(run, "_line_work", None) is None:
        consistency = import_module("app.review.assistant.vendor_consistency")
        workspace = run.engine.tools.workspace
        run._line_work = consistency.LineWork(workspace.uploads, workspace.set_pack)
    return run._line_work(run.review.file, page)


def opening_blocks(run: Any) -> list[dict[str, Any]]:
    """The question list for the reviewer's first message (`_Run.opening`), kept on its review for
    the stop-hook and the log; nothing when there is none or the page cannot be read."""
    review = run.review
    try:
        try:
            segments = _segments(run, review.page)
        except Exception:  # noqa: BLE001 - the titles split the page instead
            segments = None
        ledger = ledger_of(review.found, segments, review.size)
        sisters: list[Makeup] = []
        elsewhere: dict[str, tuple[str, int]] = {}
        context = run._context()
        if context.ready(0, run.engine.cancel):
            sisters = sister_makeups(context, review.page, lambda p: _segments(run, p))
            elsewhere = full_callouts(context, review.page, [t for t, _b2, _v in ledger.bare])
        made = questions(ledger, sisters, elsewhere)
    except Exception:  # noqa: BLE001 - the review goes on without questions
        _log.exception("page reviewer p%s: the view questions were not made", review.page)
        return []
    try:
        if context.ready(0, run.engine.cancel) and context.project is not None:
            # Where the page and GV's project schedule differ (`project_schedule`).
            made += import_module("app.review.assistant.project_schedule").question_list(
                context.project, context, review.page, len(made) + 1, MAX_QUESTIONS - len(made)
            )
    except Exception:  # noqa: BLE001 - the view questions stand without them
        _log.exception("page reviewer p%s: the schedule's questions were not made", review.page)
    review.questions = made
    if not made:
        return []
    return [{"type": "text", "text": f"{_HEAD}\n" + fence(listing(made), "view_questions")}]


ANSWER_QUESTION: Final[Mapping[str, Any]] = {
    "name": "answer_question",
    "description": (
        "Settle code's questions (Q1, Q2... in your first message) that are fine on this page: "
        "say why in a few words (e.g. 'the wall cabinets have no toe kick'). A question that is "
        "an issue is settled by report_issue with its question number instead."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["questions", "why"],
        "properties": {
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": 'The question numbers, e.g. ["Q3"]',
            },
            "why": {"type": "string", "description": "Why it is fine, short"},
        },
    },
}
#: report_issue's field naming the questions it settles.
QUESTION_FIELD: Final[Mapping[str, Any]] = {
    "type": "string",
    "description": 'Optional: the question(s) of code\'s list this issue settles, e.g. "Q3"',
}

_NUMBER: Final = re.compile(r"Q?\s*(\d{1,3})", re.IGNORECASE)


def _numbers(raw: object) -> list[int]:
    items = raw if isinstance(raw, list) else [raw]
    return [int(n) for item in items for n in _NUMBER.findall(str(item or ""))]


def _known(review: Any) -> set[int]:
    return {q.n for q in getattr(review, "questions", None) or []}


def mark_reported(review: Any, given: Mapping[str, Any]) -> None:
    """A recorded report_issue settles the questions it names (`question`)."""
    for n in _numbers(given.get("question")):
        if n in _known(review):
            review.answers[f"Q{n}"] = "issue"


def answer(review: Any, given: Mapping[str, Any]) -> str:
    """answer_question: the named questions settled as fine (one already reported stays an
    issue). Raises `ValueError` when it names no question of the list."""
    numbers = [n for n in _numbers(given.get("questions")) if n in _known(review)]
    why = " ".join(str(given.get("why") or "").split())
    if not numbers:
        raise ValueError('name questions of the list, e.g. ["Q3"]')
    if not why:
        raise ValueError("say why in a few words")
    done = []
    for n in numbers:
        key = f"Q{n}"
        if review.answers.get(key) != "issue":
            review.answers[key] = "ok"
        done.append(key)
    left = open_questions(review)
    return f"Settled {', '.join(done)}." + (f" Still open: {', '.join(left)}." if left else "")


def open_questions(review: Any) -> list[str]:
    return [
        q.label for q in getattr(review, "questions", None) or [] if q.label not in review.answers
    ]


def still_open(review: Any) -> str:
    """A sentence naming the open questions, or nothing."""
    left = open_questions(review)
    if not left:
        return ""
    return (
        f" Questions still open: {', '.join(left)} (report_issue with question \"Qn\", or "
        "answer_question)."
    )


def nudge(review: Any) -> str | None:
    """The stop-hook's message while questions are open (at most `MAX_QUESTION_NUDGES` times)."""
    left = open_questions(review)
    if not left or review.question_nudges >= MAX_QUESTION_NUDGES:
        return None
    review.question_nudges += 1
    listed = {q.label: q for q in review.questions}
    lines = "\n".join(f"{key}: {listed[key].text}" for key in left[:12])
    return (
        f"{len(left)} of code's {len(review.questions)} questions are not settled yet. Look at "
        'each one\'s boxes, then settle it: report_issue with question "Qn" when it is an issue, '
        "answer_question with a short reason when it is fine.\n" + lines
    )


def log_fields(review: Any) -> dict[str, int]:
    """For the run's log: questions made, settled as issues, settled as fine, left open."""
    answers = list((getattr(review, "answers", None) or {}).values())
    made = len(getattr(review, "questions", None) or [])
    return {
        "questions_made": made,
        "questions_answered_issue": answers.count("issue"),
        "questions_answered_ok": answers.count("ok"),
        "questions_left": made - len(answers),
    }
