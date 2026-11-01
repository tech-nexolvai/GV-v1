"""Reading coverage: how many of the dimension numbers printed on a set's drawings code read, per
set and per page, and where the rest are (#1862). Made by code when the set pack is built
(`reading_coverage.json` beside `dims.json`), shown on every full check, $0.

Why: a check can only compare what it read. A number code never read can hide a mistake, so the
check says how much it read ("Numbers read: 1,200 of about 1,400 printed on the vendor's
drawings (86%)") and lists every number it did not read, with its box and why, so a reviewer can
look at it. It only COUNTS: it never makes, changes or drops a label, a finding or a verdict.

**What counts as printed** (Y, "about": counted by code from the drawing's own text and vectors):

- typed numbers: every text label (`dims.json`), plus every number with an inch mark printed in
  a note that became no label (`24 1/4"W x 18"D`: reason "inside a note");
- pen-stroke numbers: every drawn label, plus the stroke ink on a drawing that looks like a
  number and became no label (`reading_ink`: an inch mark or millimetres over [inches] of its
  own, or a glyph shaped like a digit this set's vendor drew in a number code did read);
- numbers in pasted pictures: every picture label, plus every number two OCR engines did not
  confirm there (`pictures.unconfirmed`);
- numbers in text with no letter map (Type3, or Type0 with no `ToUnicode`; #1750): every label
  read there by OCR at its spot (`unmapped_text.FIELD`), plus every number two OCR engines did not
  confirm there (`ocr.codes.unconfirmed`).

**Read** (X): those that became labels. EQ parts code worked out are not printed, so not counted.

**How each page prints its numbers** (`printed_as`, per side, final-plan change #5): `typed` (a
text layer), `codes` (text whose font has no letter map: Type3 or a broken map, from the page's
health check), `drawn` (lettering drawn as lines or shapes, SHX pen strokes or outlined text),
`typed_and_drawn` (both on one page), `picture` (pasted pictures or a scan). Each kind has its own
reader (the text route, the picture route, the drawn and lettering routes); the coverage line
says which kinds a side's pages use, so a low count names its cause.

This module is light (no PDF, no numpy): the set pack writes the file, the check and the screens
read it (`load`, `lines`, `headline`, `sheet_rows`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

__all__ = [
    "FILE",
    "PRINTED_AS",
    "REASONS",
    "SCHEMA",
    "build",
    "headline",
    "lines",
    "load",
    "page_rows",
    "printed_as",
    "printed_as_line",
    "report",
    "sheet_rows",
    "summary_line",
]

FILE: Final = "reading_coverage.json"
SCHEMA: Final = "gv-reading-coverage/1"

#: Why a printed number was not read, in plain words (the order is the order shown).
REASONS: Final[dict[str, str]] = {
    "gv_sticker": "GV's sticker or markup over it",
    "picture": "in a pasted picture, not confirmed",
    "no_letter_map": "in text with no letter map, not confirmed",
    "mm_row": "mm over [inches] row",
    "stacked_fraction": "stacked fraction",
    "sideways": "written sideways",
    "no_inch_mark": "no inch mark of its own",
    "lines_through": "lines drawn through it",
    "too_small": "too small",
    "crowded": "crowded by other ink",
    "in_note": "inside a note",
    "other": "no agreed reading or no dimension line",
}

#: How a page prints its numbers (`printed_as`), in plain words (the order is the order shown).
PRINTED_AS: Final[dict[str, str]] = {
    "typed": "typed text",
    "codes": "text with no letter map",
    "drawn": "drawn as lines or shapes",
    "typed_and_drawn": "typed text and drawn lettering together",
    "picture": "pasted pictures",
}
#: A page whose text is more than this share of letter codes (`health.json`'s `code_share`).
_CODES_SHARE: Final = 0.2
#: A page prints typed and drawn numbers together when each kind is at least this many numbers
#: and this share of the page's.
_MIXED_MIN: Final = 2
_MIXED_SHARE: Final = 0.1

#: The sides counted, and how a sentence names their drawings.
SIDES: Final[dict[str, str]] = {
    "vendor": "the vendor's drawings",
    "architect": "the architect's drawings",
    "brand": "the brand prototype's drawings",
    "unknown": "drawings code could not tell apart",
}

#: A side's name in a table cell.
SIDE_NAMES: Final[dict[str, str]] = {
    "vendor": "Vendor",
    "architect": "Architect",
    "brand": "Brand prototype",
    "unknown": "Not told apart",
}

#: How the pieces of a count are named (the set pack's routes; `codes`: text with no letter map).
KINDS: Final = ("typed", "strokes", "pictures", "codes")
#: A label read in text with no letter map (`unmapped_text.FIELD`; this module imports nothing).
_NO_LETTER_MAP: Final = "no_letter_map"

#: A number with an inch mark in a typed note: `24 1/4"W`, `12-1/2"H`, `9"`.
_NOTE_NUMBER: Final = re.compile(r"\d+(?:[- ]\d+/\d+)?\s*[\"”″]|\d+/\d+\s*[\"”″]")
_SCALE_WORDS: Final = re.compile(r"\bSCALE\b|=", re.IGNORECASE)
#: GV's own markup kinds that never cover a number (the stamp holds the whole pasted drawing).
_NOT_STICKERS: Final = frozenset({"Stamp", "Link", "Popup"})
_STICKER_AREA: Final = 40000.0


def _box(value: object) -> tuple[float, float, float, float] | None:
    if isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 4:
        try:
            x0, y0, x1, y1 = (float(v) for v in value)
        except (TypeError, ValueError):
            return None
        return (x0, y0, x1, y1)
    return None


def _share(a: Sequence[float], b: Sequence[float]) -> float:
    """How much of `a` lies inside `b` (0 to 1)."""
    across = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    down = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2] - a[0]) * (a[3] - a[1])
    return across * down / area if area > 0 else 0.0


def _meets(a: Sequence[float], b: Sequence[float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _kind(label: Mapping[str, Any]) -> str | None:
    if label.get("derived"):
        return None  # worked out by code (EQ parts), not printed
    if label.get("origin") == "picture":
        return "pictures"
    if label.get(_NO_LETTER_MAP):
        return "codes"
    return "strokes" if label.get("route") == "drawn" else "typed"


def _empty() -> dict[str, Any]:
    return {"read": 0, "printed": 0, **{kind: {"read": 0, "printed": 0} for kind in KINDS}}


def _add(counts: dict[str, Any], kind: str, read: bool) -> None:
    counts["printed"] += 1
    counts[kind]["printed"] += 1
    if read:
        counts["read"] += 1
        counts[kind]["read"] += 1


def _stickers(annotations: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int], list[Any]]:
    out: dict[tuple[str, int], list[Any]] = {}
    for item in annotations:
        box = _box(item.get("box"))
        if box is None or str(item.get("kind")) in _NOT_STICKERS:
            continue
        if (box[2] - box[0]) * (box[3] - box[1]) >= _STICKER_AREA:
            continue
        out.setdefault((str(item.get("file")), int(item.get("page") or 0)), []).append(box)
    return out


def printed_as(counts: Mapping[str, Any], facts: Mapping[str, Any] | None = None) -> str | None:
    """How one side of a page prints its numbers (`PRINTED_AS`), from its counts (`_empty`) and
    the page's health facts (`health.json`); `None` when it prints none."""
    typed = int(counts["typed"]["printed"])
    drawn = int(counts["strokes"]["printed"])
    pictures = int(counts["pictures"]["printed"])
    codes = int((counts.get("codes") or {}).get("printed") or 0)
    if not typed + drawn + pictures + codes:
        return None
    health = facts or {}
    if codes or health.get("no_letter_map") or float(health.get("code_share") or 0) > _CODES_SHARE:
        return "codes"
    if pictures > typed + drawn:
        return "picture"
    # Both kinds only when each is more than a stray count (a spot of ink taken for a number).
    least = max(_MIXED_MIN, _MIXED_SHARE * (typed + drawn + pictures))
    if typed >= least and drawn >= least:
        return "typed_and_drawn"
    return "drawn" if drawn > typed else "typed"


def build(
    document: Mapping[str, Any],
    strokes: Mapping[tuple[str, int], Sequence[Mapping[str, Any]] | None],
    annotations: Iterable[Mapping[str, Any]] = (),
    *,
    seconds: float | None = None,
    health: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The reading coverage of a set from its `dims.json` (`document`) and the pen-stroke spots
    the drawn pass found unread on each page (`strokes`: `{box, side, reasons}` each; a page
    missing, or `None`, was not counted for pen strokes). `annotations`: the pack's GV markup (a
    small one over a spot makes it "GV's sticker or markup over it"). `health`: each page's
    health facts (`(file, page)`: `facts`), for text stored as codes."""
    stickers = _stickers(annotations)
    totals: dict[str, dict[str, Any]] = {}
    reasons: dict[str, dict[str, int]] = {}
    pages: list[dict[str, Any]] = []
    not_counted: list[dict[str, Any]] = []
    kinds: dict[str, dict[str, int]] = {}
    for entry in document.get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        name, number = str(entry.get("file")), int(entry.get("page") or 0)
        sides: dict[str, dict[str, Any]] = {}
        spots: list[dict[str, Any]] = []
        labels = [label for label in entry.get("labels") or [] if isinstance(label, Mapping)]
        label_boxes = [b for b in (_box(label.get("box")) for label in labels) if b is not None]
        for label in labels:
            kind, side = _kind(label), str(label.get("side") or "unknown")
            if kind is None or side not in SIDES:
                continue
            _add(sides.setdefault(side, _empty()), kind, True)
        # Typed numbers printed in notes that became no label.
        for phrase in entry.get("phrases") or []:
            box = _box(phrase.get("box")) if isinstance(phrase, Mapping) else None
            side = str(phrase.get("side") or "unknown") if isinstance(phrase, Mapping) else ""
            text = str(phrase.get("text") or "") if isinstance(phrase, Mapping) else ""
            if box is None or side not in SIDES or _SCALE_WORDS.search(text):
                continue
            found = _NOTE_NUMBER.findall(text)
            if not found or any(_share(box, b) > 0.2 or _share(b, box) > 0.2 for b in label_boxes):
                continue
            for _ in found:
                _add(sides.setdefault(side, _empty()), "typed", False)
            spots.append(
                {
                    "kind": "typed",
                    "side": side,
                    "reason": "in_note",
                    "box": list(box),
                    "count": len(found),
                }
            )
        # Numbers in pasted pictures two engines did not confirm.
        pictures = entry.get("pictures")
        for item in (pictures.get("unconfirmed") or []) if isinstance(pictures, Mapping) else []:
            box = _box(item.get("box")) if isinstance(item, Mapping) else None
            side = str(item.get("side") or "unknown") if isinstance(item, Mapping) else ""
            if box is None or side not in SIDES:
                continue
            _add(sides.setdefault(side, _empty()), "pictures", False)
            spots.append(
                {
                    "kind": "pictures",
                    "side": side,
                    "reason": "picture",
                    "box": list(box),
                    "count": 1,
                }
            )
        # Numbers in text with no letter map two engines did not confirm (#1750).
        ocr = entry.get("ocr")
        codes = ocr.get("codes") if isinstance(ocr, Mapping) else None
        for item in (codes.get("unconfirmed") or []) if isinstance(codes, Mapping) else []:
            box = _box(item.get("box")) if isinstance(item, Mapping) else None
            side = str(item.get("side") or "unknown") if isinstance(item, Mapping) else ""
            if box is None or side not in SIDES:
                continue
            _add(sides.setdefault(side, _empty()), "codes", False)
            spots.append(
                {
                    "kind": "codes",
                    "side": side,
                    "reason": "no_letter_map",
                    "box": list(box),
                    "count": 1,
                }
            )
        # Pen-stroke numbers the drawn pass found unread.
        found_strokes = strokes.get((name, number))
        counted = found_strokes is not None
        for item in found_strokes or []:
            box, side = _box(item.get("box")), str(item.get("side") or "unknown")
            if box is None or side not in SIDES:
                continue
            if any(_share(box, b) > 0.3 or _share(b, box) > 0.3 for b in label_boxes):
                continue  # read after all (the labels moved on since the pass counted)
            why = [str(r) for r in item.get("reasons") or [] if str(r) in REASONS]
            if any(_meets(box, s) for s in stickers.get((name, number), [])):
                why = ["gv_sticker", *[r for r in why if r != "gv_sticker"]]
            _add(sides.setdefault(side, _empty()), "strokes", False)
            spots.append(
                {
                    "kind": "strokes",
                    "side": side,
                    "reason": why[0] if why else "other",
                    "also": why[1:],
                    "box": [round(v, 2) for v in box],
                    "count": 1,
                }
            )
        if not counted:
            not_counted.append({"file": name, "page": number, "drawn": entry.get("drawn")})
        for side, counts in sides.items():
            into = totals.setdefault(side, _empty())
            for key in ("read", "printed"):
                into[key] += counts[key]
            for kind in KINDS:
                for key in ("read", "printed"):
                    into[kind][key] += counts[kind][key]
        for spot in spots:
            by = reasons.setdefault(spot["side"], {})
            by[spot["reason"]] = by.get(spot["reason"], 0) + int(spot["count"])
        spots.sort(
            key=lambda s: (list(SIDES).index(s["side"]), round(s["box"][1] / 4), s["box"][0])
        )
        facts = (health or {}).get((name, number))
        page_kinds = {
            side: kind for side, counts in sides.items() if (kind := printed_as(counts, facts))
        }
        for side, kind in page_kinds.items():
            by_kind = kinds.setdefault(side, {})
            by_kind[kind] = by_kind.get(kind, 0) + 1
        pages.append(
            {
                "file": name,
                "page": number,
                "strokes_counted": counted,
                "sides": sides,
                "printed_as": page_kinds,
                "not_read": spots,
            }
        )
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "sides": {side: totals[side] for side in SIDES if side in totals},
        "not_read_by_reason": {
            side: {r: reasons[side][r] for r in REASONS if r in reasons[side]}
            for side in SIDES
            if side in reasons
        },
        "printed_as": {
            side: {k: kinds[side][k] for k in PRINTED_AS if k in kinds[side]}
            for side in SIDES
            if side in kinds
        },
        "pages": pages,
        "pages_not_counted": not_counted,
    }
    if seconds is not None:
        out["seconds"] = round(seconds, 2)
    return out


def write(folder: Path, coverage: Mapping[str, Any]) -> None:
    """`reading_coverage.json` in the pack folder, written to a temporary name and renamed."""
    target = folder / FILE
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(coverage, indent=1), encoding="utf-8")
    temporary.replace(target)


def load(folder: Path | None) -> dict[str, Any] | None:
    """A pack's reading coverage, or `None` (no pack, or a pack made before it was counted)."""
    if folder is None:
        return None
    try:
        value = json.loads((Path(folder) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


# ---- words ------------------------------------------------------------------------------------


def _percent(read: int, printed: int) -> int:
    return round(100 * read / printed) if printed else 100


def summary_line(coverage: Mapping[str, Any] | None, side: str) -> str | None:
    """`Numbers read: 1,200 of about 1,400 printed on the vendor's drawings (86%)`, or `None`
    when the side prints none."""
    counts = (coverage or {}).get("sides", {}).get(side)
    if not isinstance(counts, Mapping) or not counts.get("printed"):
        return None
    read, printed = int(counts["read"]), int(counts["printed"])
    return (
        f"Numbers read: {read:,} of about {printed:,} printed on {SIDES[side]} "
        f"({_percent(read, printed)}%)"
    )


def printed_as_line(coverage: Mapping[str, Any] | None, side: str) -> str | None:
    """`How the architect's drawings print their numbers: typed text on 20 pages, drawn as lines
    or shapes on 4, ...`, or `None` when every page of the side prints typed text (or none)."""
    kinds = ((coverage or {}).get("printed_as") or {}).get(side) or {}
    if side == "unknown" or not kinds or set(kinds) == {"typed"}:
        return None
    parts = []
    for kind, words in PRINTED_AS.items():
        if count := int(kinds.get(kind) or 0):
            parts.append(f"{words} on {count} page{'s' if count != 1 else ''}")
    return f"How {SIDES[side]} print their numbers: " + ", ".join(parts)


def lines(coverage: Mapping[str, Any] | None) -> list[str]:
    """The coverage in plain sentences: one per side that prints numbers, how each side's pages
    print them (unless all typed), the biggest reasons for the vendor's unread ones, and the
    pages not counted for pen strokes."""
    if not coverage:
        return []
    out = [line for side in SIDES if (line := summary_line(coverage, side))]
    out += [line for side in SIDES if (line := printed_as_line(coverage, side))]
    vendor = (coverage.get("not_read_by_reason") or {}).get("vendor") or {}
    if vendor:
        top = sorted(vendor.items(), key=lambda kv: -kv[1])[:4]
        out.append(
            "Not read on the vendor's drawings, mostly: "
            + ", ".join(f"{REASONS.get(r, r)} {n}" for r, n in top)
        )
    missing = coverage.get("pages_not_counted") or []
    if missing:
        out.append(
            f"Pen-stroke numbers not counted on {len(missing)} page"
            f"{'s' if len(missing) != 1 else ''} (the drawn reader did not reach "
            f"{'them' if len(missing) != 1 else 'it'})"
        )
    return out


def headline(coverage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The coverage as flat numbers for a check's summary and funnel, so tests and scorers can
    track it over time: read / printed / percent per side, and the vendor's unread by reason."""
    if not coverage:
        return None
    out: dict[str, Any] = {}
    for side in ("vendor", "architect"):
        counts = (coverage.get("sides") or {}).get(side) or _empty()
        out[f"{side}_read"] = int(counts["read"])
        out[f"{side}_printed"] = int(counts["printed"])
        out[f"{side}_percent"] = _percent(int(counts["read"]), int(counts["printed"]))
        out[f"{side}_strokes_read"] = int(counts["strokes"]["read"])
        out[f"{side}_strokes_printed"] = int(counts["strokes"]["printed"])
        codes = counts.get("codes") or {}
        out[f"{side}_codes_read"] = int(codes.get("read") or 0)
        out[f"{side}_codes_printed"] = int(codes.get("printed") or 0)
    for side in ("vendor", "architect"):
        out[f"{side}_printed_as"] = dict((coverage.get("printed_as") or {}).get(side) or {})
    out["vendor_not_read_by_reason"] = dict(
        (coverage.get("not_read_by_reason") or {}).get("vendor") or {}
    )
    out["pages_not_counted"] = len(coverage.get("pages_not_counted") or [])
    return out


def page_rows(
    coverage: Mapping[str, Any] | None, pages: Iterable[int] | None = None
) -> list[dict[str, Any]]:
    """One row per page that prints numbers (optionally only `pages`): the vendor's and the
    architect's read / printed, and the unread spots grouped by reason."""
    wanted = set(pages) if pages is not None else None
    out: list[dict[str, Any]] = []
    for entry in (coverage or {}).get("pages") or []:
        number = int(entry.get("page") or 0)
        if wanted is not None and number not in wanted:
            continue
        sides = entry.get("sides") or {}
        if not any((sides.get(side) or {}).get("printed") for side in SIDES):
            continue
        groups: dict[str, list[dict[str, Any]]] = {}
        for spot in entry.get("not_read") or []:
            groups.setdefault(str(spot.get("reason")), []).append(spot)
        out.append(
            {
                "file": entry.get("file"),
                "page": number,
                "strokes_counted": bool(entry.get("strokes_counted")),
                **{
                    f"{side}_{key}": int((sides.get(side) or {}).get(key) or 0)
                    for side in ("vendor", "architect")
                    for key in ("read", "printed")
                },
                "not_read": [
                    {
                        "reason": reason,
                        "words": REASONS.get(reason, reason),
                        "count": sum(int(s.get("count") or 1) for s in spots),
                        "spots": [
                            {"side": s.get("side"), "box": s.get("box"), "kind": s.get("kind")}
                            for s in spots
                        ],
                    }
                    for reason in REASONS
                    if (spots := groups.get(reason))
                ],
            }
        )
    return out


#: The Not-compared workbook's sheet of unread numbers.
SHEET: Final = "Not read"
SHEET_COLUMNS: Final = (
    "Page",
    "Drawing",
    "Why not read",
    "How many",
    "Where (points: x0, y0, x1, y1)",
    "Kind",
)


def sheet_rows(coverage: Mapping[str, Any] | None) -> list[list[str]]:
    """The "Not read" sheet: a total row per side, then one row per unread spot."""
    if not coverage:
        return []
    out: list[list[str]] = []
    for side in SIDES:
        line = summary_line(coverage, side)
        if line:
            out.append(["", SIDE_NAMES[side], line, "", "", ""])
    for entry in coverage.get("pages") or []:
        for spot in entry.get("not_read") or []:
            box = spot.get("box") or []
            out.append(
                [
                    str(entry.get("page")),
                    SIDE_NAMES.get(str(spot.get("side")), str(spot.get("side"))),
                    REASONS.get(str(spot.get("reason")), str(spot.get("reason"))),
                    str(spot.get("count") or 1),
                    ", ".join(f"{float(v):.0f}" for v in box),
                    {
                        "typed": "typed text",
                        "strokes": "pen strokes",
                        "pictures": "pasted picture",
                        "codes": "text with no letter map",
                    }.get(str(spot.get("kind")), str(spot.get("kind"))),
                ]
            )
    return out


def report(coverage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The coverage as the check's trust report shows it (`ChatReadingOut`): the plain lines,
    each side's read / printed / percent, and the per-page table (pages of the set's first file
    can be opened on the page picture)."""
    if not coverage:
        return None
    sides = coverage.get("sides") or {}
    pages = coverage.get("pages") or []
    first = str(pages[0].get("file")) if pages else None

    def side(name: str) -> dict[str, int] | None:
        counts = sides.get(name)
        if not isinstance(counts, Mapping) or not counts.get("printed"):
            return None
        read, printed = int(counts["read"]), int(counts["printed"])
        return {"read": read, "printed": printed, "percent": _percent(read, printed)}

    return {
        "lines": lines(coverage),
        "vendor": side("vendor"),
        "architect": side("architect"),
        "pages": [{**row, "viewable": row.get("file") == first} for row in page_rows(coverage)],
        "pages_not_counted": len(coverage.get("pages_not_counted") or []),
    }
