"""The plan check before a paid full check: ask only where code is unsure of the page map (#1569).

One wrong pairing poisons a whole helper: a live full check once took a page's ID set for a brand
prototype and dropped three real differences. Code reads the set pack's `page_map.json` (GV's
typed headings, `set_pack.py`) before any money is spent and finds the few places it cannot be
sure of:

- **role**: a sheet with labelled drawings and one drawing with no heading ("whose is the lower
  drawing?");
- **prototype**: a drawing headed as a brand prototype in an area with no architect's drawing
  anywhere (is it really the prototype, or the architect's?);
- **pair**: a vendor page whose area has no architect's drawing (compare it with the nearest
  architect page before it, or skip it?);
- **round**: a vendor drawing with no round in an area that has more than one;
- **heading**: a page with pasted drawings and no heading or area at all.

The Run popover shows at most `MAX_DOUBTS` of them as one-click questions; with none it runs at
once, so the one-button path stays. **Nobody has to answer (#1581, owner 2026-10-18: no human
step)**: a full check lays code's own likely answer (each question's first option) over every
question left unanswered (`with_assumptions`), runs on, and writes each one on the cloud drawing's
cover ("Assumed: page 28, the lower drawing is the architect's drawing"). An answer given in the
popover still wins. Each answer is kept append-only in the pack's
`plan_answers.jsonl` (the pack is shared by every chat on the same PDFs, so it is never asked
again) and laid over the page map whenever code reads it (`apply_answers`): the plan, the helpers'
briefs and their footers all see the answered map. Later, the set notes can carry them.

Pure code on the page map, plus the small answers file: no PDF opened, no AI call.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

__all__ = [
    "ANSWERS_FILE",
    "MAX_DOUBTS",
    "Doubt",
    "answer_note",
    "apply_answers",
    "architect_in_area",
    "architect_paired",
    "assumptions",
    "doubts",
    "plan_summary",
    "read_answers",
    "record_answers",
    "valid_answer",
    "with_assumptions",
]

ANSWERS_FILE: Final = "plan_answers.jsonl"
#: Questions shown before a full check, at most.
MAX_DOUBTS: Final = 3
#: The kinds of doubt, in the order they are asked (the most harmful wrong pairing first).
KINDS: Final = ("prototype", "role", "pair", "round", "heading")
ROLE_WORDS: Final = {
    "architect": "Architect's drawing",
    "vendor": "Vendor's drawing",
    "brand_prototype": "Brand prototype",
    "general": "Not a drawing to compare",
}
SKIP_NOTE: Final = "the reviewer chose to skip it (plan check)"
_ID: Final = re.compile(
    r"^(prototype|role|round):f(\d{1,2}):p(\d{1,4}):h(\d{1,2})$|^(pair|heading):f(\d{1,2}):p(\d{1,4})$"
)
_PAIR: Final = re.compile(r"^pair:(\d{1,4})$")


@dataclass(frozen=True, slots=True)
class Doubt:
    """One question code asks before a full check."""

    id: str
    kind: str
    page: int
    file: str
    question: str
    options: tuple[tuple[str, str], ...]
    """(value, label) pairs, the likely answer first."""
    box: tuple[float, float, float, float] | None = None
    """The drawing it is about (PDF points, origin top-left), to mark on the page picture."""
    about: str = ""
    """What the question is about, for an assumption's words ("page 28, the lower drawing")."""

    def out(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "page": self.page,
            "file": self.file,
            "question": self.question,
            "options": [{"value": value, "label": label} for value, label in self.options],
            "box": None if self.box is None else list(self.box),
        }


# ---- reading the map --------------------------------------------------------------------------


def _main_entries(page_map: Mapping[str, Any], main_file: str | None) -> list[dict[str, Any]]:
    entries = [e for e in page_map.get("pages") or [] if isinstance(e, Mapping)]
    files = [str(e.get("file")) for e in entries]
    if main_file is None or main_file not in files:
        if len(set(files)) != 1:
            return []
        main_file = files[0]
    chosen = [
        dict(e)
        for e in entries
        if str(e.get("file")) == main_file and isinstance(e.get("page"), int)
    ]
    return sorted(chosen, key=lambda e: int(e["page"]))


def _halves(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]


def _box(half: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    box = half.get("box")
    if isinstance(box, Sequence) and len(box) == 4:
        try:
            x0, y0, x1, y1 = (float(v) for v in box)
        except (TypeError, ValueError):
            return None
        return (x0, y0, x1, y1)
    return None


def _position(halves: Sequence[Mapping[str, Any]], index: int) -> str:
    """ "upper" / "lower" / "left" / "right" for one of two drawings, else "drawing n of m"."""
    if len(halves) == 1:
        return "pasted"
    boxes = [_box(h) for h in halves]
    if len(halves) == 2 and boxes[0] is not None and boxes[1] is not None:
        a, b = boxes[0], boxes[1]
        dy = abs((a[1] + a[3]) - (b[1] + b[3])) / 2
        dx = abs((a[0] + a[2]) - (b[0] + b[2])) / 2
        mine, other = (a, b) if index == 0 else (b, a)
        if dy >= dx:
            return "upper" if mine[1] + mine[3] < other[1] + other[3] else "lower"
        return "left" if mine[0] + mine[2] < other[0] + other[2] else "right"
    return f"drawing {index + 1} of {len(halves)}"


def _area_key(entry: Mapping[str, Any]) -> tuple[Any, Any] | None:
    area = entry.get("area")
    if not isinstance(area, str) or not area.strip():
        return None
    return (entry.get("section"), " ".join(area.split()).lower())


def _roles_by_area(page_map: Mapping[str, Any]) -> dict[tuple[Any, Any], set[str]]:
    """Every role drawn in each area, across all the uploads (an architect's PDF counts)."""
    out: dict[tuple[Any, Any], set[str]] = {}
    for entry in page_map.get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        key = _area_key(entry)
        if key is None:
            continue
        roles = out.setdefault(key, set())
        roles.update(str(h.get("role")) for h in _halves(entry))
        # An architect's PDF's page of the same area name, in another section, still counts.
        out.setdefault((None, key[1]), set()).update(str(h.get("role")) for h in _halves(entry))
    return out


def _area_roles(entry: Mapping[str, Any], by_area: Mapping[tuple[Any, Any], set[str]]) -> set[str]:
    key = _area_key(entry)
    if key is None:
        return {str(h.get("role")) for h in _halves(entry)}
    return set(by_area.get(key, set())) | set(by_area.get((None, key[1]), set()))


def architect_in_area(page_map: Mapping[str, Any] | None, file: str | None, page: int) -> bool:
    """Whether the architect gives an elevation of the area of `file`'s page `page` (R1, Anant
    2026-10-20): an architect drawing on the page or anywhere in its typed area (an architect's
    PDF counts), or a paired architect page. `True` when code cannot tell (no page map, the page not in
    it): the architect then governs, as before; only a page the map places in an area with no
    architect drawing makes the brand prototype the reference."""
    if not page_map:
        return True
    entries = [e for e in page_map.get("pages") or [] if isinstance(e, Mapping)]
    mine = [
        e for e in entries if e.get("page") == page and (file is None or str(e.get("file")) == file)
    ]
    if len(mine) != 1:
        return True
    entry = mine[0]
    if _area_roles(entry, _roles_by_area(page_map)) & {"architect"}:
        return True
    if _area_key(entry) is None and any(
        h.get("role") == "architect" and h.get("from_upload") for e in entries for h in _halves(e)
    ):
        return True  # separate architect files: code pairs across the set, the architect governs
    # A page paired with an architect page (the plan check's answer) is compared with it.
    return isinstance(entry.get("pair_page"), int)


def architect_paired(
    page_map: Mapping[str, Any] | None, file: str | None, page: int
) -> bool | None:
    """Whether a vendor page has an architect drawing paired with it (#1802): an architect
    drawing on the page or in its typed area, or an architect page paired by hand (`pair_page`).
    Unlike `architect_in_area`, separate architect files alone do not count: code pairs those
    across the set, which is no pair of this page. `None` when code cannot tell (no page map, or
    the page is not in it)."""
    if not page_map:
        return None
    entries = [e for e in page_map.get("pages") or [] if isinstance(e, Mapping)]
    mine = [
        e for e in entries if e.get("page") == page and (file is None or str(e.get("file")) == file)
    ]
    if len(mine) != 1:
        return None
    entry = mine[0]
    if _area_roles(entry, _roles_by_area(page_map)) & {"architect"}:
        return True
    return isinstance(entry.get("pair_page"), int)


# ---- the doubts --------------------------------------------------------------------------------


def doubts(
    page_map: Mapping[str, Any] | None,
    main_file: str | None,
    answered: Iterable[str] = (),
    limit: int | None = MAX_DOUBTS,
) -> list[Doubt]:
    """Where code is unsure of the page map, the most harmful first, at most `limit` (`None`:
    all). Questions already `answered` (by id) are not asked again. Nothing when the map is
    missing or code is sure everywhere."""
    if not page_map:
        return []
    entries = _main_entries(page_map, main_file)
    if not entries:
        return []
    files = [str(name) for name in page_map.get("files") or []]
    done = set(answered)
    by_area = _roles_by_area(page_map)
    architect_pages = [
        int(e["page"]) for e in entries if any(h.get("role") == "architect" for h in _halves(e))
    ]
    vendor_rounds: dict[tuple[Any, Any] | int, set[int]] = {}
    for entry in entries:
        key: tuple[Any, Any] | int = _area_key(entry) or int(entry["page"])
        for half in _halves(entry):
            if half.get("role") == "vendor" and isinstance(half.get("round"), int):
                vendor_rounds.setdefault(key, set()).add(int(half["round"]))
    found: list[Doubt] = []
    for entry in entries:
        if entry.get("skip"):
            continue
        page = int(entry["page"])
        file = str(entry.get("file"))
        tag = f"f{files.index(file) if file in files else 0}:p{page}"
        halves = _halves(entry)
        if not halves:
            continue
        roles = [str(h.get("role")) for h in halves]
        labelled = [r for r in roles if r != "general"]
        area_roles = _area_roles(entry, by_area)
        for index, half in enumerate(halves):
            where = _position(halves, index)
            role = roles[index]
            if half.get("heading_by") == "reviewer":
                continue
            if role == "general" and labelled:
                found.append(
                    Doubt(
                        f"role:{tag}:h{index}",
                        "role",
                        page,
                        file,
                        f"Page {page}: whose is the {where} drawing? It has no heading.",
                        tuple(
                            (value, ROLE_WORDS[value])
                            for value in ("vendor", "architect", "brand_prototype", "general")
                        ),
                        _box(half),
                        f"page {page}, the {where} drawing (no heading)",
                    )
                )
            elif role == "brand_prototype" and "architect" not in area_roles:
                found.append(
                    Doubt(
                        f"prototype:{tag}:h{index}",
                        "prototype",
                        page,
                        file,
                        f"Page {page}: is the {where} drawing the brand prototype or the "
                        "architect's? No other architect's drawing of this area was found.",
                        (
                            ("brand_prototype", ROLE_WORDS["brand_prototype"]),
                            ("architect", ROLE_WORDS["architect"]),
                        ),
                        _box(half),
                        f"page {page}, the {where} drawing",
                    )
                )
            elif role == "vendor" and half.get("round") is None:
                key = _area_key(entry) or page
                rounds = sorted(vendor_rounds.get(key, set()))
                if len(rounds) >= 2:
                    found.append(
                        Doubt(
                            f"round:{tag}:h{index}",
                            "round",
                            page,
                            file,
                            f"Page {page}: which round is the {where} vendor drawing?",
                            tuple((str(r), f"Round {r}") for r in rounds),
                            _box(half),
                            f"page {page}, the {where} vendor drawing",
                        )
                    )
        if (
            "vendor" in roles
            and "architect" not in area_roles
            and "brand_prototype" not in roles
            and entry.get("pair_page") is None
        ):
            before = [p for p in architect_pages if p < page]
            if before:
                found.append(
                    Doubt(
                        f"pair:{tag}",
                        "pair",
                        page,
                        file,
                        f"Page {page}: no architect's drawing was found for it. Compare it with "
                        f"page {before[-1]}, or skip it?",
                        (
                            (f"pair:{before[-1]}", f"Compare with page {before[-1]}"),
                            ("skip", "Skip it"),
                        ),
                        _box(halves[roles.index("vendor")]),
                        f"page {page} (no architect's drawing found for it)",
                    )
                )
        if not labelled and _area_key(entry) is None and not entry.get("section"):
            found.append(
                Doubt(
                    f"heading:{tag}",
                    "heading",
                    page,
                    file,
                    f"Page {page} has drawings but no heading: is it the vendor's drawing to "
                    "check?",
                    (("vendor", "Yes, check it"), ("skip", "No, skip it")),
                    _box(halves[0]),
                    f"page {page} (drawings, no heading)",
                )
            )
    found = [d for d in found if d.id not in done]
    found.sort(key=lambda d: (KINDS.index(d.kind), d.page, d.id))
    return found if limit is None else found[:limit]


def plan_summary(page_map: Mapping[str, Any] | None, main_file: str | None) -> str | None:
    """Code's plan in one line: "60 pages · 22 pairs · rounds 1-2 · 3 general pages"."""
    if not page_map:
        return None
    entries = _main_entries(page_map, main_file)
    if not entries:
        return None
    pairs = sum(
        1
        for e in entries
        if {"architect", "vendor"} <= {str(h.get("role")) for h in _halves(e)}
        or ("vendor" in {str(h.get("role")) for h in _halves(e)} and e.get("pair_page"))
    )
    rounds = sorted(
        {int(h["round"]) for e in entries for h in _halves(e) if isinstance(h.get("round"), int)}
    )
    general = sum(1 for e in entries if not _halves(e) or e.get("skip"))
    parts = [f"{len(entries)} pages", f"{pairs} pair{'s' if pairs != 1 else ''}"]
    if rounds:
        parts.append(
            f"round {rounds[0]}" if len(rounds) == 1 else f"rounds {rounds[0]}-{rounds[-1]}"
        )
    if general:
        parts.append(f"{general} general page{'s' if general != 1 else ''}")
    return " · ".join(parts)


# ---- the answers ---------------------------------------------------------------------------------


def valid_answer(doubt_id: str, value: str) -> bool:
    """Whether an answer has the shape its question allows."""
    found = _ID.match(doubt_id)
    if found is None:
        return False
    kind = found.group(1) or found.group(5)
    if kind in ("prototype", "role"):
        return value in ROLE_WORDS
    if kind == "round":
        return value.isdigit() and 1 <= int(value) <= 9
    if kind == "pair":
        return value == "skip" or _PAIR.match(value) is not None
    return value in ("vendor", "skip")


def read_answers(folder: Path | None) -> dict[str, str]:
    """The pack's answers, the latest per question (`plan_answers.jsonl`, append-only)."""
    if folder is None:
        return {}
    try:
        lines = (folder / ANSWERS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and isinstance(row.get("answer"), str)
            and valid_answer(row["id"], row["answer"])
        ):
            out[row["id"]] = row["answer"]
    return out


def record_answers(folder: Path, answers: Mapping[str, str], by: str) -> int:
    """The reviewer's answers added to the pack's answers file (never rewritten). Returns how
    many were kept (an answer of the wrong shape is left out)."""
    at = datetime.now(UTC).isoformat()
    rows = [
        json.dumps({"id": doubt_id, "answer": value, "by": by, "at": at}, ensure_ascii=False)
        for doubt_id, value in answers.items()
        if valid_answer(doubt_id, value)
    ]
    if rows:
        with (folder / ANSWERS_FILE).open("a", encoding="utf-8") as out:
            out.write("".join(f"{row}\n" for row in rows))
    return len(rows)


def answer_note(doubt_id: str, value: str) -> tuple[str, int] | None:
    """An answer in plain words for the set notes, with its page: "Plan check: on page 28,
    drawing 2 is the architect's drawing." `None` when it does not fit its question."""
    found = _ID.match(doubt_id)
    if found is None or not valid_answer(doubt_id, value):
        return None
    kind = found.group(1) or found.group(5)
    page = int(found.group(3) or found.group(7))
    drawing = f"drawing {int(found.group(4)) + 1}" if found.group(4) is not None else "the page"
    if kind in ("prototype", "role"):
        words = f"{drawing} is: {ROLE_WORDS[value].lower()}"
    elif kind == "round":
        words = f"{drawing} is round {value}"
    elif value == "skip":
        words = "skip it in a full check"
    elif kind == "pair":
        words = f"compare it with page {value.split(':', 1)[1]}"
    else:
        words = "the vendor's drawing, to check"
    return f"Plan check: on page {page}, {words}.", page


# ---- no human step: code's own likely answers (#1581) ---------------------------------------------


def assumptions(
    page_map: Mapping[str, Any] | None, main_file: str | None, answers: Mapping[str, str]
) -> list[tuple[Doubt, str, str]]:
    """Every question nobody answered, with code's likely answer (its first option) and the
    assumption in plain words: (question, answer, words)."""
    out: list[tuple[Doubt, str, str]] = []
    for doubt in doubts(page_map, main_file, answers, limit=None):
        if not doubt.options:
            continue
        value, label = doubt.options[0]
        out.append((doubt, value, _assumed_words(doubt, value, label)))
    return out


def _assumed_words(doubt: Doubt, value: str, label: str) -> str:
    about = doubt.about or f"page {doubt.page}"
    if doubt.kind in ("role", "prototype"):
        return f"{about} is the {ROLE_WORDS.get(value, label).lower()}."
    if doubt.kind == "round":
        return f"{about} is {label.lower()}."
    if doubt.kind == "pair":
        return f"{about} is compared with {label.removeprefix('Compare with ').lower()}."
    return f"{about} is the vendor's drawing, checked."


def with_assumptions(
    page_map: Mapping[str, Any], main_file: str | None, answers: Mapping[str, str]
) -> dict[str, Any]:
    """The page map with the reviewer's answers and, for every question left, code's likely
    answer laid over it (a drawing set this way carries `heading_by: code`)."""
    answered = apply_answers(page_map, answers)
    assumed = {doubt.id: value for doubt, value, _w in assumptions(answered, main_file, answers)}
    return apply_answers(answered, assumed, by="code") if assumed else answered


def _entry_role(halves: Sequence[Mapping[str, Any]]) -> str:
    roles = sorted({str(h.get("role")) for h in halves} - {"general"})
    return "+".join(roles) if roles else "general"


def apply_answers(
    page_map: Mapping[str, Any], answers: Mapping[str, str], *, by: str = "reviewer"
) -> dict[str, Any]:
    """A copy of the page map with the reviewer's answers laid over it. A drawing's role or round
    set by the reviewer carries `heading_by: reviewer` (`by`: `code` for code's own assumption); a
    page paired by hand carries `pair_page`; a page skipped carries `skip` (its reason)."""
    out = copy.deepcopy(dict(page_map))
    if not answers:
        return out
    files = [str(name) for name in out.get("files") or []]
    by_page: dict[tuple[str, int], dict[str, Any]] = {}
    for entry in out.get("pages") or []:
        if isinstance(entry, dict) and isinstance(entry.get("page"), int):
            by_page.setdefault((str(entry.get("file")), entry["page"]), entry)
    only = {file for file, _page in by_page}
    for doubt_id, value in answers.items():
        found = _ID.match(doubt_id)
        if found is None or not valid_answer(doubt_id, value):
            continue
        kind = found.group(1) or found.group(5)
        number = int(found.group(2) or found.group(6))
        page = int(found.group(3) or found.group(7))
        file = (
            files[number] if number < len(files) else (next(iter(only)) if len(only) == 1 else None)
        )
        entry = by_page.get((str(file), page))
        if entry is None:
            continue
        halves = [h for h in entry.get("halves") or [] if isinstance(h, dict)]
        if kind in ("prototype", "role", "round"):
            index = int(found.group(4))
            if index >= len(halves):
                continue
            if kind == "round":
                halves[index]["round"] = int(value)
                entry["rounds"] = sorted(
                    {int(h["round"]) for h in halves if isinstance(h.get("round"), int)}
                )
            else:
                halves[index]["role"] = value
            halves[index]["heading_by"] = by
            entry["role"] = _entry_role(halves)
        elif value == "skip":
            entry["skip"] = SKIP_NOTE if by == "reviewer" else "code assumed it is not to check"
        elif kind == "pair":
            paired = _PAIR.match(value)
            if paired is not None:
                entry["pair_page"] = int(paired.group(1))
        elif kind == "heading":
            for half in halves:
                half["role"] = "vendor"
                half["heading_by"] = by
            entry["role"] = _entry_role(halves)
    return out
