"""One item, one card: the full check's last step folds the copies of one item (2026-10-23).

Two readers can report the same item: a page helper and a look-again closer, or a helper and the
whole-drawing look. Each copy was placed in a card on its own, so the result listed one item twice
(in Right twice, or in Not compared and in Please check). This step runs after every other step and
before the counts and the files. It folds copies by **code facts only**:

- the same printed vendor label (code's label id), or the same vendor value at the same spot on the
  same vendor page (the two boxes overlap by at least half);
- the same architect label, with one copy's vendor spot on top of the other's vendor label (a
  reader that named no vendor label, its spot holding the label the other copy names), and never
  two different vendor numbers;
- the same finish tag codes on the same page pair (finish items with no size on either side); when
  neither vendor side prints a tag, every word of the shorter vendor material is in the other.

Wording alone never merges two items, and neither does a big AI region alone: two different items
read at one big spot stay two. Only comparisons with the architect are folded: GV's rules, GV's
limits, a GV standard and the vendor's own checks ask another question and always stay.

Which card stays: a code-proved Wrong, then a Likely problem, then Ask the architect, then Not
compared, then Right (the most severe, decided by code). The card kept is one of the copies,
unchanged, so it is never redder or greener than a copy that is proved; the other copies are kept
inside it as evidence (`same_item.copies`). When the copies pair one vendor number with different
architect numbers, nobody knows which pair is right: when none of them is more than Not compared,
the card is Not compared with every value listed (`same_item.conflict`), never Right. A Wrong, a
Likely problem or an Ask the architect is never folded into such a card (folding never hides a
Wrong or changes a card's colour): every copy then stays its own card, as before. Copies with two
different vendor numbers are never folded either (two vendor dimensions, two items), nor a check
against the vendor's own earlier round (it never compares with the architect).

An item whose only fact is "no architect drawing paired" on a page already in Drawing needed says
what that card says: it is folded under the page's card (`drawing_needed`). The vendor's own checks
on that page (its sums, GV's rules) ask another question and stay.

Two more folds (2026-10-25, the client-meeting job), by code facts only, never hiding a Wrong:

- **pairing unsure, one page**: two Not-compared items on one vendor page with the same record of
  no architect page paired (`reference_page`), one's vendor spot holding the other's (a helper's
  whole-page "drawn items" beside its own item there): the bigger one says nothing more and folds
  into the other;
- **one material difference per set**: the same material claim on several pages (the item names
  the material or finish, no size on either side, the reference names the same material class or
  finish tag and the vendor's side names neither) is one Likely problem for the whole set, its
  pages listed, the other pages' copies kept on it as evidence. Only Likely problems and Not
  compared copies fold; a red stays its own card, and nothing folds without a Likely problem to
  carry it. A vendor side that says it lacks the reference's tag ("no SS 01 tag") does not name
  it (2026-10-26: such a copy had stayed a second card). The card lists every other page in
  `also_on`, so the drawing marks each page's spot and the card names the pages.

One more fold (2026-10-30, a real full check listed 7 vendor strings twice): **one vendor string
that does not add up, one card**. A page reviewer (or the consistency check) and the reading ledger
can both report the same vendor dimension string whose parts miss its printed overall; the
reviewer's copy is a Likely problem, the ledger's a Not compared ("vendor's string not verified on
the drawing"), so the list said it twice. They fold only when code knows it is one string, all of:

- the ledger's copy (`created_by: "ledger"`, the vendor's own sum) has parts and a printed overall,
  and code adds the parts up to something else;
- the other copy is on the same vendor page, in the same round, and its spot and the ledger's
  overlap by at least half of the smaller one (two strings of one page sit apart);
- and either it records the same printed overall with parts that do not add up either, one copy's
  parts all among the other's (one reader missed a part), or it records no parts and no architect
  side, and its own words name the ledger's overall and every one of the ledger's parts.

The card kept is the more severe (a Wrong, then a Likely problem, then Not compared), then code's
own (the ledger's or the consistency check's) over the AI's; the other is kept inside it as
evidence. A Wrong is never folded away (two Wrongs both stay), nothing is made redder, and two
strings with different overalls, or parts that are not one within the other, are never joined.

Pure code: no I/O, no AI call.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.review.assistant import likely_problems, proof
from app.review.assistant.chat_trust import inch_words
from app.review.assistant.classification import SAME_ITEM, internal
from app.review.assistant.decision_resolver import MILLWORK_FAMILIES
from app.review.assistant.finding_checks import to_inches
from app.review.assistant.round_reference import round_only

__all__ = ["RANK", "Folded", "OneCard", "compares_architect", "fold", "same_item"]

#: The card that stays, most severe first (decided by code).
RANK: Final[Mapping[str, int]] = {
    "clouded": 0,
    "likely_problem": 1,
    "rule_note": 2,
    "not_compared": 3,
    "right": 4,
    "dismissed": 5,
}
_TRUST: Final = {"confirmed": 0, "likely": 1, "to_check": 2}
#: Two boxes are one spot when they overlap by at least this share of their union.
_SAME_SPOT: Final = 0.5
_TAG: Final = re.compile(r"\b([A-Z]{1,4})[ -]?(\d{1,4})\b")
#: How code knows two copies are one item, in plain words.
_HOW: Final[Mapping[str, str]] = {
    "vendor_label": "the same printed vendor dimension",
    "vendor_spot": "the same vendor value at the same spot",
    "architect_label": "the same architect dimension, at the same vendor spot",
    "tags": "the same finish tags on the same two pages",
}
_BY: Final = (
    ("whole_look", "the whole-drawing look"),
    ("helper", "a page helper"),
    ("lead", "the lead reviewer"),
)


@dataclass(frozen=True, slots=True)
class Folded:
    """One copy folded away: `drop` into `keep` (a finding id, or `page:<n>` for a page's
    Drawing-needed card)."""

    keep: str
    drop: str
    reason: str


@dataclass(slots=True)
class OneCard:
    """What the step changes: the cards kept (with their copies), the copies folded away, and the
    items folded under a page's Drawing-needed card (`{page: [copy, ...]}`)."""

    kept: list[dict[str, Any]] = field(default_factory=list)
    folded: list[Folded] = field(default_factory=list)
    drawing_needed: dict[int, list[dict[str, Any]]] = field(default_factory=dict)


# ---- the code facts -------------------------------------------------------------------------------


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        found = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return found if found.is_finite() else None


def _inches(finding: Mapping[str, Any], side: str) -> Decimal | None:
    """The side's value in inches as the finding's checks read it; for the vendor, else as its
    label prints it (the architect's is never read back: the resolver clears it on purpose when
    several architect values apply)."""
    inches = finding.get("inches")
    found = _dec(inches.get(side)) if isinstance(inches, Mapping) else None
    if found is not None or side != "vendor":
        return found
    label = _label(finding, side)
    return _dec(label.get("inches")) if label is not None else None


def _label(finding: Mapping[str, Any], side: str) -> Mapping[str, Any] | None:
    dims = finding.get("dims")
    label = dims.get(side) if isinstance(dims, Mapping) else None
    return label if isinstance(label, Mapping) and label.get("id") else None


def _label_key(finding: Mapping[str, Any], side: str) -> tuple[Any, ...] | None:
    """The label's (page, id, file); the file is compared only when both name one."""
    label = _label(finding, side)
    if label is None:
        return None
    return (label.get("page"), str(label["id"]), label.get("file") or None)


def _same_label(a: tuple[Any, ...] | None, b: tuple[Any, ...] | None) -> bool:
    if a is None or b is None or a[:2] != b[:2]:
        return False
    return not (a[2] and b[2] and a[2] != b[2])


type _Box = tuple[Any, Any, float, float, float, float]


def _box(raw: object, page: object = None, file: object = None) -> _Box | None:
    """(page, file, x0, y0, x1, y1) of a box (a mapping, or a label's [x0, y0, x1, y1])."""
    try:
        if isinstance(raw, Mapping):
            x0, y0, x1, y1 = (float(raw[k]) for k in ("x0", "y0", "x1", "y1"))
            page, file = raw.get("page", page), raw.get("file") or file
        elif isinstance(raw, Sequence) and not isinstance(raw, str) and len(raw) == 4:
            x0, y0, x1, y1 = (float(v) for v in raw)
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    if x1 < x0 or y1 < y0:
        return None
    return (page, file or None, x0, y0, x1, y1)


def _vendor_box(finding: Mapping[str, Any]) -> _Box | None:
    return _box(finding.get("vendor_box"), finding.get("page"))


def _vendor_spot(finding: Mapping[str, Any]) -> _Box | None:
    """The vendor label's own box, else the finding's vendor box."""
    label = _label(finding, "vendor")
    if label is not None:
        found = _box(label.get("box"), label.get("page"), label.get("file"))
        if found is not None:
            return found
    return _vendor_box(finding)


def _same_sheet(a: _Box, b: _Box) -> bool:
    if a[0] is not None and b[0] is not None and a[0] != b[0]:
        return False
    return not (a[1] and b[1] and a[1] != b[1])


def _overlap(a: _Box, b: _Box) -> float:
    """How much two boxes overlap: the shared area over the union (0 to 1)."""
    if not _same_sheet(a, b):
        return 0.0
    w = min(a[4], b[4]) - max(a[2], b[2])
    h = min(a[5], b[5]) - max(a[3], b[3])
    if w < 0 or h < 0:
        return 0.0
    shared = w * h
    union = (a[4] - a[2]) * (a[5] - a[3]) + (b[4] - b[2]) * (b[5] - b[3]) - shared
    return shared / union if union > 0 else 1.0


def _inside(spot: _Box, region: _Box) -> bool:
    """The centre of `spot` lies in `region` (same page and file)."""
    if not _same_sheet(spot, region):
        return False
    cx, cy = (spot[2] + spot[4]) / 2, (spot[3] + spot[5]) / 2
    return region[2] <= cx <= region[4] and region[3] <= cy <= region[5]


def _codes(text: object) -> frozenset[str]:
    """The millwork finish tags written in `text`, normalised ("SS 01", "SS-01" -> "SS-1")."""
    return frozenset(
        f"{m.group(1)}-{int(m.group(2))}"
        for m in _TAG.finditer(str(text or ""))
        if m.group(1) in MILLWORK_FAMILIES
    )


def _same_round(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return a.get("round") is None or b.get("round") is None or a.get("round") == b.get("round")


def same_item(a: Mapping[str, Any], b: Mapping[str, Any]) -> str | None:
    """How code knows `a` and `b` are one item ("vendor_label", "vendor_spot", "architect_label",
    "tags"), else `None`. Only code facts count; the words never do."""
    if a.get("page") != b.get("page") or not _same_round(a, b):
        return None
    v_a, v_b = _label_key(a, "vendor"), _label_key(b, "vendor")
    if v_a is not None and v_b is not None:
        # Two printed vendor labels: one item only when it is the same label.
        return "vendor_label" if _same_label(v_a, v_b) else None
    spot_a, spot_b = _vendor_spot(a), _vendor_spot(b)
    in_a, in_b = _inches(a, "vendor"), _inches(b, "vendor")
    if (
        in_a is not None
        and in_a == in_b
        and spot_a is not None
        and spot_b is not None
        and _overlap(spot_a, spot_b) >= _SAME_SPOT
    ):
        return "vendor_spot"
    a_a, a_b = _label_key(a, "architect"), _label_key(b, "architect")
    if in_a is not None and in_b is not None and in_a != in_b:
        # Two vendor numbers against one architect dimension: two vendor dimensions, two items.
        a_a = None
    if _same_label(a_a, a_b) and spot_a is not None and spot_b is not None:
        # One reader named the vendor label, the other only a spot: that spot holds the label.
        if v_a is not None and v_b is None and _inside(spot_a, spot_b):
            return "architect_label"
        if v_b is not None and v_a is None and _inside(spot_b, spot_a):
            return "architect_label"
        if v_a is None and v_b is None and _overlap(spot_a, spot_b) >= _SAME_SPOT:
            return "architect_label"
    if _tags_match(a, b):
        return "tags"
    return None


def _tags_match(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Two finish items (no size on either side) on the same page pair with the same tag codes:
    the architect's the same and not none, and the vendor's the same."""
    if a.get("vs_page") is None or a.get("vs_page") != b.get("vs_page"):
        return False
    if any(_inches(f, side) is not None for f in (a, b) for side in ("vendor", "architect")):
        return False
    arch = _codes(a.get("architect"))
    if not arch or arch != _codes(b.get("architect")):
        return False
    vendor = _codes(a.get("vendor"))
    if vendor != _codes(b.get("vendor")):
        return False
    if vendor:
        return True
    # No tag on either vendor side: the same finish only when the vendor names the same material
    # (every word of the shorter one in the other), else two elements against one tag.
    words_a, words_b = _words(a.get("vendor")), _words(b.get("vendor"))
    shorter, longer = sorted((words_a, words_b), key=len)
    return bool(shorter) and shorter <= longer


_WORD: Final = re.compile(r"[a-z]{4,}")
_COMMON: Final = frozenset({"with", "drawn", "shows", "shown", "tag", "finish", "none", "side"})


def _words(text: object) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(str(text or "").lower()) if w not in _COMMON)


#: The sources of a comparison with the architect's drawings (the brand prototype standing in).
_ARCHITECT_SOURCES: Final = frozenset({"architect", "brand_prototype", "gv_review"})


def compares_architect(finding: Mapping[str, Any]) -> bool:
    """A comparison with the architect: never GV's rule, GV's limits, a GV standard or the
    vendor's own check (its sum, its other view), which ask another question."""
    if str(finding.get("source") or "architect") not in _ARCHITECT_SOURCES:
        return False
    if proof.vendor_own(finding) or finding.get("rule_check_id") or finding.get("gv_standard"):
        return False
    if round_only(finding) or finding.get("reference_side") == "vendor":
        return False  # compared with the vendor's own drawing (an earlier round): never folded
    return proof.gv_limits(finding) is None


# ---- the card that stays --------------------------------------------------------------------------


def _number(finding_id: str) -> tuple[int, str]:
    digits = "".join(ch for ch in finding_id if ch.isdigit())
    return (int(digits) if digits else 0, finding_id)


def _order(finding: Mapping[str, Any]) -> tuple[Any, ...]:
    """The most severe card first; among one card's copies, the better proved."""
    return (
        RANK.get(proof.proof_group(finding), len(RANK)),
        _TRUST.get(str(finding.get("trust_level") or ""), 3),
        0 if finding.get("paired_by") == "code" else 1,
        -len(proof.witnesses_of(finding)),
        0 if _label(finding, "vendor") is not None else 1,
        _number(str(finding.get("id") or "")),
    )


def _fixed(finding: Mapping[str, Any]) -> bool:
    """A copy never folded away: a recorded difference, one the reviewer decided, or one that
    carries the same item on other pages (its marks there belong to its own verdict; the pages
    this step's own set-wide material card lists do not count: a Continue folds it again)."""
    carried = [
        e for e in finding.get("also_on") or [] if not (isinstance(e, Mapping) and e.get("fold"))
    ]
    return bool(finding.get("record_id") or finding.get("decision") or carried)


def _conflict(group: Sequence[Mapping[str, Any]]) -> list[str] | None:
    """The values when the copies disagree: different architect numbers, or different vendor
    numbers code read at its labels. `None` when they agree."""
    architect = {v for f in group if (v := _inches(f, "architect")) is not None}
    vendor = {
        v
        for f in group
        if _label(f, "vendor") is not None and (v := _inches(f, "vendor")) is not None
    }
    if len(architect) < 2 and len(vendor) < 2:
        return None
    values = architect if len(architect) >= 2 else vendor
    return [inch_words(v) for v in sorted(values)]


def _by(finding: Mapping[str, Any]) -> str:
    made = str(finding.get("created_by") or "")
    for start, words in _BY:
        if made.startswith(start):
            return words
    return "code" if made else "the check"


def _copy(finding: Mapping[str, Any]) -> dict[str, Any]:
    """A folded copy as evidence on the card kept: what it said, its card, and why."""
    group = proof.proof_group(finding)
    found = proof.proof_of(finding)
    reason = found.get("reason") or (found.get("proof") if group == "clouded" else None)
    return {
        "finding_id": str(finding.get("id")),
        "item": finding.get("item"),
        "group": group,
        "vendor": finding.get("vendor"),
        "architect": finding.get("architect"),
        "reason": reason,
        "by": _by(finding),
        "vendor_box": finding.get("vendor_box"),
    }


def _groups(findings: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """The findings joined by `same_item` (each joined pair is a code fact)."""
    parent = list(range(len(findings)))

    def root(at: int) -> int:
        while parent[at] != at:
            parent[at] = parent[parent[at]]
            at = parent[at]
        return at

    for i, a in enumerate(findings):
        for j in range(i + 1, len(findings)):
            if root(i) != root(j) and same_item(a, findings[j]) is not None:
                parent[root(j)] = root(i)
    joined: dict[int, list[Mapping[str, Any]]] = {}
    for at, finding in enumerate(findings):
        joined.setdefault(root(at), []).append(finding)
    return [group for group in joined.values() if len(group) > 1]


def _no_drawing(finding: Mapping[str, Any]) -> bool:
    """A Not-compared item against the architect with no architect side at all (no page, spot or
    label): on a Drawing-needed page it only says what that card says."""
    if proof.proof_group(finding) != "not_compared" or _fixed(finding):
        return False
    if str(finding.get("source") or "architect") != "architect" or proof.vendor_own(finding):
        return False
    if finding.get("rule_check_id") or finding.get("created_by") in ("consistency", "ledger"):
        return False
    if _inches(finding, "architect") is not None or _codes(finding.get("architect")):
        return False  # an architect value or tag was read: it says more than the page's card
    return not (finding.get("vs_page") or finding.get("arch_box") or _label(finding, "architect"))


def fold(findings: Sequence[Mapping[str, Any]], drawing_needed: Collection[int] = ()) -> OneCard:
    """The step (see the module): `findings`, the answer's live findings; `drawing_needed`, the
    vendor pages the result lists under Drawing needed."""
    out = OneCard()
    live = [f for f in findings if f.get("id") and not internal(f)]
    gone: set[str] = set()
    needed = set(drawing_needed)
    for finding in live:
        page = finding.get("page")
        if isinstance(page, int) and page in needed and _no_drawing(finding):
            fid = str(finding["id"])
            gone.add(fid)
            out.drawing_needed.setdefault(page, []).append(_copy(finding))
            out.folded.append(
                Folded(
                    keep=f"page:{page}",
                    drop=fid,
                    reason=(
                        f"Listed under page {page}'s Drawing needed: no architect drawing of this "
                        "page is in the upload, so there is nothing to compare it with yet."
                    ),
                )
            )
    for group in _groups([f for f in live if str(f["id"]) not in gone and compares_architect(f)]):
        ordered = sorted(group, key=_order)
        keep = ordered[0]
        if any(_fixed(f) for f in ordered[1:]):
            continue  # a copy that must stay would be folded away: both stay
        labels = [key for f in ordered if (key := _label_key(f, "vendor")) is not None]
        if any(not _same_label(labels[0], other) for other in labels[1:]):
            continue  # two printed vendor labels (joined through a spot): two items, both stay
        if len({v for f in ordered if (v := _inches(f, "vendor")) is not None}) > 1:
            continue  # two vendor numbers (joined through a chain of spots): two items
        conflict = _conflict(ordered)
        # A card folded before (Continue, a re-report) keeps the values its copies disagreed on.
        carried = [
            str(value)
            for f in ordered
            if isinstance(f.get(SAME_ITEM), Mapping)
            for value in (f[SAME_ITEM].get("conflict") or [])
        ]
        if carried:
            conflict = sorted({*(conflict or []), *carried}, key=str)
        if conflict and RANK.get(proof.proof_group(keep), len(RANK)) < RANK["not_compared"]:
            # A Wrong, Likely problem or Ask the architect among copies that disagree on the
            # architect's size: folding would hide it or change its colour, so every copy stays.
            continue
        given = keep.get(SAME_ITEM)
        before: Mapping[str, Any] = given if isinstance(given, Mapping) else {}
        copies = list(before.get("copies") or [])
        for other in ordered[1:]:
            copies.append(_copy(other))
            earlier = other.get(SAME_ITEM)
            if isinstance(earlier, Mapping):
                copies.extend(earlier.get("copies") or [])
            out.folded.append(
                Folded(
                    keep=str(keep["id"]),
                    drop=str(other["id"]),
                    reason=(
                        f"The same item as {keep['id']} "
                        f"({_HOW.get(same_item(keep, other) or '', 'the same item')}): one card, "
                        "this copy kept on it as evidence."
                    ),
                )
            )
        merged: dict[str, Any] = {"copies": copies}
        if conflict:
            merged["conflict"] = conflict
        out.kept.append({**keep, SAME_ITEM: merged})
    gone.update(f.drop for f in out.folded)
    _fold_unpaired(live, gone, out)
    gone.update(f.drop for f in out.folded)
    _fold_material(live, gone, out)
    gone.update(f.drop for f in out.folded)
    _fold_sums(live, gone, out)
    return out


# ---- pairing unsure on one page -------------------------------------------------------------------


def _current(finding: Mapping[str, Any], out: OneCard) -> Mapping[str, Any]:
    """The finding as this step left it (a card kept with its copies), else as given."""
    for kept in out.kept:
        if str(kept.get("id")) == str(finding.get("id")):
            return kept
    return finding


def _keep_card(
    out: OneCard,
    keep: Mapping[str, Any],
    others: Sequence[Mapping[str, Any]],
    reason: str,
    **changes: Any,
) -> None:
    """`others` fold into `keep` (as this step left it), each kept on it as evidence."""
    card = dict(_current(keep, out))
    given = card.get(SAME_ITEM)
    before: Mapping[str, Any] = given if isinstance(given, Mapping) else {}
    copies = list(before.get("copies") or [])
    for other in others:
        other = _current(other, out)
        copies.append(_copy(other))
        earlier = other.get(SAME_ITEM)
        if isinstance(earlier, Mapping):
            copies.extend(earlier.get("copies") or [])
        out.folded.append(Folded(keep=str(keep["id"]), drop=str(other["id"]), reason=reason))
    card.update(changes)
    card[SAME_ITEM] = {**before, "copies": copies}
    gone = {str(o["id"]) for o in others}
    out.kept = [k for k in out.kept if str(k.get("id")) not in gone | {str(keep["id"])}]
    out.kept.append(card)


def _unpaired_record(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Code's record that no architect page is paired with the item's page, else None."""
    record = finding.get("reference_page")
    if not isinstance(record, Mapping) or record.get("page") is not None or record.get("pair"):
        return None
    if proof.classified(finding).reason != "no_sheet":
        return None
    return (str(record.get("why") or ""), record.get("file"))


_NO_BOX: Final[_Box] = (None, None, 0.0, 0.0, 0.0, 0.0)


def _area(box: _Box) -> float:
    return max(box[4] - box[2], 0.0) * max(box[5] - box[3], 0.0)


def _holds(outer: _Box, inner: _Box) -> bool:
    """`outer` holds all of `inner` (same sheet) and is at least twice its size."""
    return (
        _same_sheet(outer, inner)
        and outer[2] <= inner[2] + 0.5
        and outer[3] <= inner[3] + 0.5
        and inner[4] <= outer[4] + 0.5
        and inner[5] <= outer[5] + 0.5
        and _area(outer) >= 2 * _area(inner)
    )


def _fold_unpaired(live: Sequence[Mapping[str, Any]], gone: set[str], out: OneCard) -> None:
    """Pairing unsure, one page (see the module): the bigger spot folds into the item it holds."""
    open_items = [
        f
        for f in live
        if str(f["id"]) not in gone
        and compares_architect(f)
        and not _fixed(f)
        and proof.proof_group(f) == "not_compared"
        and _unpaired_record(f) is not None
        and _vendor_box(f) is not None
    ]
    # The smallest spots first, so an item folds only into one that stays.
    for outer in sorted(
        open_items, key=lambda f: (_area(_vendor_box(f) or _NO_BOX), _number(str(f["id"])))
    ):
        if str(outer["id"]) in gone:
            continue
        outer_box = _vendor_box(outer)
        held = [
            inner
            for inner in open_items
            if inner is not outer
            and str(inner["id"]) not in gone
            and inner.get("page") == outer.get("page")
            and _same_round(inner, outer)
            and _unpaired_record(inner) == _unpaired_record(outer)
            and outer_box is not None
            and (inner_box := _vendor_box(inner)) is not None
            and _holds(outer_box, inner_box)
        ]
        if not held:
            continue
        keep = min(held, key=lambda f: _number(str(f["id"])))
        _keep_card(
            out,
            keep,
            [outer],
            f"The same page with no architect page paired as {keep['id']}, whose item lies inside "
            "this one's spot: one card, this copy kept on it as evidence.",
        )
        gone.add(str(outer["id"]))


# ---- one material difference per set ---------------------------------------------------------------

_MATERIAL_ITEM: Final = re.compile(r"\b(?:material|finish)\b", re.IGNORECASE)
#: The material classes a reference names (the claim is the same when the classes are).
_CLASSES: Final = (
    "solid surface",
    "quartz",
    "granite",
    "marble",
    "laminate",
    "stainless",
    "porcelain",
    "concrete",
    "terrazzo",
    "veneer",
    "glass",
)


def _classes(text: object) -> frozenset[str]:
    flat = " ".join(str(text or "").lower().split())
    return frozenset(c for c in _CLASSES if re.search(rf"\b{c}\b", flat))


#: A material or tag the vendor's side says is NOT there ("no SS 01 tag", "without solid
#: surface"): up to the next comma, semicolon, full stop or bracket.
_NEGATED: Final = re.compile(
    r"\b(?:no|not|without|instead of|missing|lacks?|lacking)\b[^,;.()]*", re.IGNORECASE
)


def _named(text: object) -> str:
    """The words a side names as present: its "no SS 01 tag" and "without solid surface" parts
    taken out (a vendor side that says it lacks the reference's tag does not name it)."""
    return _NEGATED.sub(" ", str(text or ""))


def _material_key(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """(the reference's material classes, its finish tags, the round) when the item is a
    material claim the vendor's side differs on, else None."""
    if not _MATERIAL_ITEM.search(str(finding.get("item") or "")):
        return None
    if any(_inches(finding, side) is not None for side in ("vendor", "architect")):
        return None
    vendor = finding.get("vendor")
    if not _words(vendor):
        return None
    classes, tags = _classes(finding.get("architect")), _codes(finding.get("architect"))
    named = _named(vendor)
    if (classes & _classes(named)) or (tags & _codes(named)):
        return None  # the vendor names the reference's material: no difference claimed
    if not (classes or tags):
        return None
    return (classes, tags, finding.get("round"))


def _material_groups(
    keyed: Sequence[tuple[tuple[Any, ...], Mapping[str, Any]]],
) -> list[list[Mapping[str, Any]]]:
    """The copies of one material claim: the same classes (else the same tags) in the same round;
    copies naming different finish tags are different claims (laminate PL-1 is not PL-2), and a
    copy naming no tag joins only when its classes' copies name one tag set at most."""
    by_class: dict[tuple[Any, ...], list[tuple[frozenset[str], Mapping[str, Any]]]] = {}
    for (classes, tags, round_), finding in keyed:
        head = (classes, round_) if classes else (frozenset(), round_, tags)
        by_class.setdefault(head, []).append((tags, finding))
    out: list[list[Mapping[str, Any]]] = []
    for copies in by_class.values():
        tag_sets = {tags for tags, _ in copies if tags}
        if len(tag_sets) <= 1:
            out.append([f for _, f in copies])
        else:
            out.extend([f for tags, f in copies if tags == one] for one in tag_sets)
    return out


#: The words a set-wide card adds to its item and its "please check" line (taken off again when
#: the card is folded once more, so they are never repeated).
_WHERE: Final = re.compile(r"\s*\(the whole set, vendor pages? [^)]*\)\s*$")
_WHERE_WORDS: Final = re.compile(r"\s*The same on the whole set, vendor pages? [^.]*\.\s*$")


def _pages_words(pages: Sequence[int]) -> str:
    listed = [str(p) for p in sorted(set(pages))]
    if len(listed) == 1:
        return f"page {listed[0]}"
    return f"pages {', '.join(listed[:-1])} and {listed[-1]}"


def _fold_material(live: Sequence[Mapping[str, Any]], gone: set[str], out: OneCard) -> None:
    """One material difference per set (see the module)."""
    keyed: list[tuple[tuple[Any, ...], Mapping[str, Any]]] = []
    for finding in live:
        if str(finding["id"]) in gone or not compares_architect(finding) or _fixed(finding):
            continue
        current = _current(finding, out)
        if proof.proof_group(current) not in ("likely_problem", "not_compared"):
            continue
        key = _material_key(current)
        if key is not None:
            keyed.append((key, current))
    for copies in _material_groups(keyed):
        pages = {int(f["page"]) for f in copies if isinstance(f.get("page"), int)}
        likely = [f for f in copies if proof.proof_group(f) == "likely_problem"]
        if len(copies) < 2 or len(pages) < 2 or not likely:
            continue
        keep = min(likely, key=_order)
        others = [f for f in copies if f is not keep]
        # The pages of copies folded into any of them before (a Continue) are the set's too.
        for copy in (c for f in copies for c in _earlier_copies(f)):
            box = copy.get("vendor_box")
            if isinstance(box, Mapping) and isinstance(box.get("page"), int):
                pages.add(int(box["page"]))
        where = f"the whole set, vendor {_pages_words(sorted(pages))}"
        item = _WHERE.sub("", " ".join(str(keep.get("item") or "Material").split()))
        changes: dict[str, Any] = {"item": f"{item} ({where})", "also_on": _also_on(keep, others)}
        mark = keep.get("whole_look")
        if isinstance(mark, Mapping) and mark.get("words"):
            words = _WHERE_WORDS.sub("", str(mark["words"]))
            changes["whole_look"] = {**mark, "words": f"{words} The same on {where}."}
        _keep_card(
            out,
            keep,
            others,
            f"The same material difference as {keep['id']} (the reference names the same "
            "material, the vendor another): one card for the whole set, this page's copy kept on "
            "it as evidence.",
            **changes,
        )
        gone.update(str(o["id"]) for o in others)


#: An `also_on` entry this step's material fold wrote (never a reason to keep the card fixed).
_MATERIAL_FOLD: Final = "set_card"
#: What a page's entry on the set-wide card keeps (`also_on`, as the resolver's): the cloud
#: drawing marks that page's spot too, and the card lists the page.
_ALSO_KEYS: Final = (
    "page",
    "vs_page",
    "round",
    "item",
    "vendor",
    "architect",
    "vendor_box",
    "arch_box",
)


def _also_on(keep: Mapping[str, Any], others: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The set-wide card's other pages: each folded copy's page, spot and the pages it carried."""
    out = [dict(e) for e in keep.get("also_on") or [] if isinstance(e, Mapping)]
    entries: list[dict[str, Any]] = []
    for other in others:
        entries.append(
            {"finding_id": str(other.get("id")), **{k: other.get(k) for k in _ALSO_KEYS}}
        )
        entries += [dict(e) for e in other.get("also_on") or [] if isinstance(e, Mapping)]
    for copy in (c for f in (keep, *others) for c in _earlier_copies(f)):  # folded before
        box = copy.get("vendor_box")
        page = box.get("page") if isinstance(box, Mapping) else None
        if isinstance(page, int) and page != keep.get("page"):
            entries.append(
                {"finding_id": str(copy.get("finding_id")), "page": page}
                | {k: copy.get(k) for k in ("item", "vendor", "architect", "vendor_box")}
            )
    for entry in entries:
        if not any(e.get("finding_id") == entry.get("finding_id") for e in out):
            out.append({**entry, "fold": _MATERIAL_FOLD})
    return out


def _earlier_copies(finding: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    given = finding.get(SAME_ITEM)
    copies = given.get("copies") if isinstance(given, Mapping) else None
    return [c for c in copies or [] if isinstance(c, Mapping)]


# ---- one vendor string that does not add up, one card ----------------------------------------------

#: Who made the reading ledger's copy of a vendor string (its sum).
_LEDGER: Final = "ledger"
#: Code's own checks of a vendor string: kept over the AI's copy of the same string.
_CODE_SUMS: Final = frozenset({"ledger", "consistency"})
#: The source of a vendor's own sum (the ledger's, the consistency check's, a reviewer's).
_VENDOR_SUM: Final = "vendor_arithmetic"
#: The sources a copy of a vendor string may have: its sum, or a page reviewer's please-check
#: filed against no reference (GV's rules and limits ask another question).
_STRING_SOURCES: Final = frozenset({_VENDOR_SUM, "architect"})
#: Two spots are one string's when they share at least this part of the smaller one: a reviewer's
#: box is often a tight crop inside the ledger's whole-string box, or the other way round, so the
#: share of their union (`_SAME_SPOT`) is too strict here.
_SAME_STRING_SPOT: Final = 0.5
#: The cards a copy may be on: a Right or a dismissed one says nothing is wrong, and an Ask the
#: architect asks another question.
_OPEN_GROUPS: Final = frozenset({"clouded", "likely_problem", "not_compared"})
#: A size written in a finding's words: a whole number with a fraction, a fraction, a decimal or a
#: whole number ("48 1/8", "3/4", "50.75", "2").
_SIZE: Final = re.compile(r"(?<![\d./])(\d+)(?:\s+(\d+)/(\d+)|/(\d+)|(\.\d+))?(?![\d/])")


def _sizes_in(text: str) -> frozenset[Decimal]:
    """Every size the words write, in inches (`_SIZE`; a zero denominator is no size)."""
    out: set[Decimal] = set()
    for found in _SIZE.finditer(text):
        whole, top, bottom, under, decimal = found.groups()
        if under is not None:  # "3/4"
            if int(under):
                out.add(Decimal(int(whole)) / Decimal(int(under)))
        elif top is not None:  # "48 1/8"
            if int(bottom):
                out.add(Decimal(int(whole)) + Decimal(int(top)) / Decimal(int(bottom)))
        else:  # "50.75", "2"
            out.add(Decimal(whole + (decimal or "")))
    return frozenset(out)


def _string_sum(finding: Mapping[str, Any]) -> tuple[Decimal, tuple[Decimal, ...]] | None:
    """(the printed overall, the parts) in inches of a vendor string whose parts code adds up to
    something other than the overall; else None (no parts, a value code cannot read, or a string
    that adds up)."""
    parts, total = finding.get("parts"), finding.get("printed_total")
    if not isinstance(parts, list) or len(parts) < 2 or total is None:
        return None
    whole = to_inches(str(total))
    read = tuple(value for part in parts if (value := to_inches(str(part))) is not None)
    if whole is None or len(read) != len(parts):
        return None
    return None if sum(read) == whole else (whole, read)


def _shared_of_smaller(a: _Box, b: _Box) -> float:
    """The area two boxes share over the smaller one's (0 to 1; 0 on two sheets)."""
    if not _same_sheet(a, b):
        return 0.0
    w = min(a[4], b[4]) - max(a[2], b[2])
    h = min(a[5], b[5]) - max(a[3], b[3])
    smaller = min(_area(a), _area(b))
    if w <= 0 or h <= 0 or smaller <= 0:
        return 0.0
    return (w * h) / smaller


def _one_within(a: Sequence[Decimal], b: Sequence[Decimal]) -> bool:
    """Every part of one string is among the other's (counted): one reader missed a part."""
    first, second = Counter(a), Counter(b)
    return not (first - second) or not (second - first)


def _said(finding: Mapping[str, Any]) -> str:
    """The copy's own words: its item, its why, and its please-check mark's words."""
    mark = finding.get(likely_problems.FIELD)
    marked = [mark.get("why"), mark.get("words")] if isinstance(mark, Mapping) else []
    return " ".join(str(t) for t in (finding.get("item"), finding.get("why"), *marked) if t)


def _same_string(ledger: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """Code knows `other` reports the ledger's vendor string (see the module)."""
    found = _string_sum(ledger)
    if found is None or other.get("page") != ledger.get("page"):
        return False
    if not _same_round(ledger, other) or str(other.get("source") or "") not in _STRING_SOURCES:
        return False
    if other.get("rule_check_id") or other.get("gv_standard") or proof.gv_limits(other):
        return False  # GV's rule or limit at that spot asks another question
    if {proof.proof_group(ledger), proof.proof_group(other)} - _OPEN_GROUPS:
        return False
    spot, theirs = _vendor_box(ledger), _vendor_box(other)
    if spot is None or theirs is None or _shared_of_smaller(spot, theirs) < _SAME_STRING_SPOT:
        return False  # two strings of one page sit apart
    total, parts = found
    their_sum = _string_sum(other)
    if their_sum is not None:
        return their_sum[0] == total and _one_within(parts, their_sum[1])
    if other.get("parts") or other.get("printed_total") or other.get("vs_page") is not None:
        return False  # its own string adds up, or it compares with a reference page
    if _inches(other, "architect") is not None:
        return False
    sizes = _sizes_in(_said(other))
    return total in sizes and set(parts) <= sizes


def _sum_order(finding: Mapping[str, Any]) -> tuple[Any, ...]:
    """The most severe copy first, then code's own check over the AI's, then `_order`."""
    return (
        RANK.get(proof.proof_group(finding), len(RANK)),
        0 if finding.get("created_by") in _CODE_SUMS else 1,
        *_order(finding),
    )


def _fold_sums(live: Sequence[Mapping[str, Any]], gone: set[str], out: OneCard) -> None:
    """One vendor string that does not add up, one card (see the module)."""
    ledgers = [
        f
        for f in live
        if str(f["id"]) not in gone
        and f.get("created_by") == _LEDGER
        and f.get("source") == _VENDOR_SUM
        and _string_sum(f) is not None
    ]
    for given in sorted(ledgers, key=lambda f: _number(str(f["id"]))):
        if str(given["id"]) in gone:
            continue
        ledger = _current(given, out)
        copies = [
            current
            for f in live
            if str(f["id"]) not in gone | {str(ledger["id"])}
            and f.get("created_by") != _LEDGER
            and _same_string(ledger, current := _current(f, out))
        ]
        if not copies:
            continue
        keep, *others = sorted([ledger, *copies], key=_sum_order)
        if any(_fixed(f) or proof.proof_group(f) == "clouded" for f in others):
            continue  # a Wrong, or a copy that must stay, is never folded away: all stay
        found = _string_sum(ledger)
        total = inch_words(found[0]) if found is not None else "its overall"
        _keep_card(
            out,
            keep,
            others,
            f"The same vendor dimension string as {keep['id']} (the same page and spot, the "
            f"printed overall {total}, the same parts not adding up to it): one card, this copy "
            "kept on it as evidence.",
        )
        gone.update(str(o["id"]) for o in others)
