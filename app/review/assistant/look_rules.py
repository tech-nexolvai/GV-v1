"""Look rules: code's checks on the whole-drawing look's words-only claims (2026-10-27).

A by-eye check of a real 17-page set found three ways the whole-drawing look (`whole_look`) raised a
"Likely problem" that was not the vendor's: it read GV's own markup, flattened into the vendor's
pasted drawing, as the vendor's (an outlet GV drew, shelves GV pasted), it claimed things drawn
beside the unit (a bracket on the wall, the side of the next unit) as missing millwork, and it put
two differences in one card, so a wrong half rode on a real one. This step runs in the escalation
right after the claim rules (`escalation._claims_step`), on every open item the whole-drawing look
raised with no label (`likely_problems.likely_only`). Like the claim rules it only ever withdraws a
claim or makes it Not compared (a question for the reviewer); it never makes anything red or a
Likely problem.

1. **GV's symbol** (`gv_markup`): an extra piece whose vendor spot is one of GV's framed symbols
   (`gv_stickers`: GV's yellow box and red frame round a drawn outlet, hidden from the pictures
   since) is GV's markup, never the vendor's piece: **withdrawn**. A count whose spot holds one is
   **Not compared** (the count may include GV's symbol).
2. **An unsure overlay** (`unsure_overlay`): the vendor spot lies on black line work flattened into
   the vendor's drawing as markup (`gv_stickers` overlays), which may be GV's or the vendor's: what
   the look says the vendor drew there is not raised as the vendor's: **Not compared**.
3. **A piece beside the unit** (`piece_place`): a missing or extra piece is a Likely problem only
   when it is part of this unit on the drawing that has it: code finds a millwork tag printed on it
   (a unit code like B30 or a finish tag like WD-01), or the look places it inside the unit's
   outline (`whole_look`'s `piece.place`). A piece drawn beside the unit (a bracket on the wall,
   the side of the next unit) with no tag code can read, or one the look gave no spot for, is
   **Not compared**, asking whether it is part of the millwork. A look's own word only ever takes
   a claim back to a question, never makes it more. Code does not read the unit's outline itself:
   the dimension lines it reads are often only the short pieces beside their numbers, so their
   span is no outline (measured on a real set: the floating shelves and the outlets of real
   missing-piece claims fell outside it). Claims from looks made before the look gave the piece's
   place are left as they were.
4. **A claim to ask** (`look_question`): the look marked it to ask (`whole_look`: two differences
   in one claim, or a count of different things on the two sides): **Not compared**.

Pure code: no I/O, no AI call. Verification: `tests/review/assistant/test_look_rules.py` (made-up
drawings and values).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from app.review.assistant import likely_problems

__all__ = [
    "ASK",
    "PIECE",
    "changes",
    "has_tag",
]

type Box = tuple[float, float, float, float]
type ByPage = Mapping[tuple[str, int], Sequence[Mapping[str, Any]]]

#: The look's mark keys this step reads (`whole_look`): where the piece is, and why to ask.
PIECE: Final = "piece"
ASK: Final = "ask"
#: A GV symbol is the claimed piece when at least this share of it lies in the vendor spot and the
#: spot is at most `_SPOT_PER_SYMBOL` times its size (the spot is round the symbol, not a run).
_SYMBOL_INSIDE: Final = 0.5
_SPOT_PER_SYMBOL: Final = 10.0
#: An overlay covers a spot when it covers at least this share of the spot, or this share of
#: the overlay lies in the spot.
_OVERLAY_SHARE: Final = 0.5
#: A tag printed this close round the piece's box is the piece's (points).
_TAG_SLACK_PT: Final = 6.0
#: A millwork tag as drawings print it: a unit code (B30, BIN18R, W3036L) or a finish tag (WD-01).
_TAG: Final = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,4}-?\d{1,4}[A-Z]{0,2}(?![A-Za-z0-9])")

_SYMBOL_WHY: Final = (
    "the vendor spot is GV's own symbol (GV's markup tool, flattened into the vendor's drawing), "
    "not something the vendor drew"
)
_COUNT_WHY: Final = (
    "one of GV's own symbols sits in this spot (GV's markup flattened into the vendor's drawing), "
    "so the count may include GV's mark"
)
_OVERLAY_WHY: Final = (
    "the vendor's drawing here carries line work flattened into it as markup, which code cannot "
    "tell apart from GV's own, so what the vendor drew there is not known"
)
_OVERLAY_NEEDS: Final = "the vendor's own drawing of {thing} without GV's markup"
_PLACE_NEEDS: Final = "whether {thing} is part of this millwork unit"
_ASK_NEEDS: Final = "each difference of {thing} checked on its own"


def _box(value: object) -> Box | None:
    if isinstance(value, Mapping):
        try:
            x0, y0, x1, y1 = (float(value[k]) for k in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            return None
    elif isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 4:
        try:
            x0, y0, x1, y1 = (float(v) for v in value)
        except (TypeError, ValueError):
            return None
    else:
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _overlap(a: Box, b: Box) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _where(finding: Mapping[str, Any], main_file: str | None) -> tuple[str, int, Box] | None:
    """The vendor spot's file, page and box."""
    spot = finding.get("vendor_box")
    box = _box(spot)
    if box is None or not isinstance(spot, Mapping):
        return None
    page = spot.get("page", finding.get("page"))
    if isinstance(page, bool) or not isinstance(page, int):
        return None
    return str(spot.get("file") or main_file or ""), page, box


def _on(by_page: ByPage, file: str, page: int) -> list[Box]:
    return [b for b in (_box(row.get("box")) for row in by_page.get((file, page), [])) if b]


# ---- 1. GV's symbol, 2. unsure overlays ----------------------------------------------------------


def _symbol_in(spot: Box, stickers: Iterable[Mapping[str, Any]]) -> bool:
    for sticker in stickers:
        box = _box(sticker.get("box"))
        if sticker.get("kind") != "symbol" or box is None or _area(box) <= 0:
            continue
        if _overlap(spot, box) >= _SYMBOL_INSIDE * _area(box) and _area(
            spot
        ) <= _SPOT_PER_SYMBOL * _area(box):
            return True
    return False


def _on_overlay(spot: Box, overlays: Iterable[Box]) -> bool:
    for box in overlays:
        shared = _overlap(spot, box)
        if shared > 0 and (
            shared >= _OVERLAY_SHARE * _area(spot) or shared >= _OVERLAY_SHARE * _area(box)
        ):
            return True
    return False


# ---- 3. a piece beside the unit -------------------------------------------------------------------


def _tags(text: object) -> set[str]:
    return {t.replace("-", "") for t in _TAG.findall(" ".join(str(text or "").upper().split()))}


def has_tag(
    phrases: Iterable[Mapping[str, Any]], box: Box, side: str, named: object = None
) -> str | None:
    """The millwork tag printed on or right next to the piece's box on that side's drawing (the
    one the look named when it named one), else `None`."""
    want = _tags(named)
    s = _TAG_SLACK_PT
    grown = (box[0] - s, box[1] - s, box[2] + s, box[3] + s)
    for phrase in phrases:
        if str(phrase.get("side") or "unknown") not in (side, "unknown"):
            continue
        where = _box(phrase.get("box"))
        if where is None or _overlap(grown, where) <= 0:
            continue
        found = _tags(phrase.get("text"))
        if found and (not want or found & want):
            return str(phrase.get("text") or "").strip()
    return None


def _piece(mark: Mapping[str, Any], main_file: str | None) -> dict[str, Any] | None:
    """The piece a missing or extra piece claim is about, as the look gave it (`{}` when it gave
    none code can use); `None` for a look made before it gave one."""
    piece = mark.get(PIECE)
    if not isinstance(piece, Mapping):
        return None
    box = _box(piece.get("box"))
    page = piece.get("page")
    side = str(piece.get("side") or "")
    if box is None or isinstance(page, bool) or not isinstance(page, int):
        return {}
    if side not in ("architect", "vendor"):
        return {}
    return {
        "side": side,
        "file": str(piece.get("file") or main_file or ""),
        "page": page,
        "box": box,
        "tag": piece.get("tag"),
        "place": piece.get("place"),
    }


def _place(piece: Mapping[str, Any], phrases: ByPage) -> str | None:
    """Why the piece is not shown to be part of the unit, else `None` (a tag code reads on it, or
    the look places it inside the unit's outline)."""
    if not piece:
        return (
            "the look did not say where the piece is on the drawing that has it, so it is asked, "
            "not claimed"
        )
    side, file, page, box = piece["side"], piece["file"], piece["page"], piece["box"]
    if has_tag(phrases.get((file, page), []), box, side, piece.get("tag")) is not None:
        return None
    if piece.get("place") == "inside":
        return None
    where = "the architect's drawing" if side == "architect" else "the vendor's drawing"
    return (
        f"the piece is drawn beside the unit on {where} and code reads no millwork tag on it, "
        "so it is asked, not claimed: it may be a wall item or the next unit"
    )


# ---- the step -------------------------------------------------------------------------------------


def changes(
    findings: Iterable[Mapping[str, Any]],
    *,
    main_file: str | None = None,
    stickers: ByPage | None = None,
    overlays: ByPage | None = None,
    phrases: ByPage | None = None,
) -> list[tuple[str, Any]]:
    """(finding id, `claim_rules.Change`) for each open words-only look claim a rule changes."""
    from app.review.assistant import claim_rules

    out: list[tuple[str, Any]] = []
    for finding in findings:
        if not likely_problems.likely_only(finding) or not claim_rules.open_claim(finding):
            continue
        mark = finding.get(likely_problems.FIELD)
        if not isinstance(mark, Mapping):
            continue
        kind = str(mark.get("kind") or "")
        fid = str(finding["id"])
        where = _where(finding, main_file)
        if where is not None:
            file, page, spot = where
            on_page = (stickers or {}).get((file, page), [])
            if kind in ("extra_piece", "count") and _symbol_in(spot, on_page):
                out.append(
                    (
                        fid,
                        (
                            claim_rules.Change("withdrawn", "gv_markup", _SYMBOL_WHY)
                            if kind == "extra_piece"
                            else claim_rules.Change("not_compared", "gv_markup", _COUNT_WHY)
                        ),
                    )
                )
                continue
            if _on_overlay(spot, _on(overlays or {}, file, page)):
                out.append(
                    (
                        fid,
                        claim_rules.Change(
                            "not_compared", "unsure_overlay", _OVERLAY_WHY, _OVERLAY_NEEDS
                        ),
                    )
                )
                continue
        ask = " ".join(str(mark.get(ASK) or "").split())
        if ask:
            out.append((fid, claim_rules.Change("not_compared", "look_question", ask, _ASK_NEEDS)))
            continue
        if kind in ("missing_piece", "extra_piece"):
            piece = _piece(mark, main_file)
            placed = _place(piece, phrases or {}) if piece is not None else None
            if placed is not None:
                out.append(
                    (fid, claim_rules.Change("not_compared", "piece_place", placed, _PLACE_NEEDS))
                )
    return out
