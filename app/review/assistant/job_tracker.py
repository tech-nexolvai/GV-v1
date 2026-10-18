"""The job tracker (#1578): every item found across a project's drawing sets, rounds and checks.

A chat forgets between conversations; GV Review keeps every answer's findings and every reviewer
decision append-only. This module turns them into what a project manager asks for, without opening a
PDF: per drawing set, each item's history (first seen, still wrong, fixed in which round, not a
problem and who said so) and the open count per round; per vendor, the kinds of mistakes it makes
most, how often it fixes them on the next round, and its average open items per round.

Pure code: no I/O, no AI call. `app/api/job_tracker.py` loads the stored answers (each finding at its
newest correction, with its latest decision) and hands them here.

**What one item is.** A finding that was Wrong or Your decision in at least one check. Its sightings
(one per finding, plus the pages the resolver folded into it) are linked by what the app already
knows, never by new matching rules of its own:

- inside one answer: the resolver's same-item fold (`also_on`, #1554), and the round link by the
  item's words (`chat_trust.round_item_key`, the key `round_statuses` uses). Two rounds with several
  findings of the same words are paired in page order, so two counters with the same words stay two
  items;
- across answers (several checks of one set): GV Review's recorded difference (`record_id`), then the
  numbered dimension label the vendor side was built from (#1558), then the same page and spot (the
  vendor boxes within `NEAR_PT`, `revision_diff`'s "at the zone" distance), then the same page and
  the same item words.

**Rounds.** A finding's `round` (the sheet's "first / second set of comments") says which round; a
finding with none takes the round other findings on its page were given, else round 1. A finding's
stored `round_status` wins over anything worked out here: today `compare_rounds` writes it, and the
automatic round-2 check (Fixed / Still wrong / New) writes it when it lands; both spellings are
read (`ROUND_STATUS_WORDS`), nothing is recomputed.

**Where an item stands now.** The latest round decides; inside one round the newest check wins (a
`same` item that was not compared is no proof and is skipped). Then:

- `not_a_problem`: the latest decision on any of its findings is Not a problem (the person's name);
- `fixed`: Wrong or Your decision in an earlier round, right in a later one (or the stored round
  status says the later round fixed it): `fixed_round`;
- `right_later`: right on a later check of the same round (the AI changed its reading, the vendor
  did not change the drawing);
- `open`: otherwise; `open_kind` is `wrong` or `decision`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any, Final, Literal
from uuid import UUID

from app.review.assistant.chat_trust import round_item_key
from app.review.assistant.impact import by_impact, impact_of
from vocabulary.product_registry import registry

__all__ = [
    "CHECK_WORDS",
    "KIND_WORDS",
    "NEAR_PT",
    "ROUND_STATUS_WORDS",
    "TrackerAnswer",
    "TrackerSet",
    "build_tracker",
    "comment_label",
    "comment_numbers",
    "item_kind",
    "item_words",
    "vendor_key",
]

#: Points within which two vendor boxes on one page are the same spot (`revision_diff._NEAR_PT`).
NEAR_PT: Final = 18.0

type State = Literal["open", "fixed", "not_a_problem", "right_later"]
type RoundStatus = Literal["fixed", "still_wrong", "new", "not_in_later_round", "single_round"]

#: A finding's stored `round_status`, in today's words and the automatic round-2 check's.
ROUND_STATUS_WORDS: Final[Mapping[str, RoundStatus]] = {
    "fixed_in_later_round": "fixed",
    "fixed": "fixed",
    "still_wrong": "still_wrong",
    "new_in_later_round": "new",
    "new": "new",
    "not_in_later_round": "not_in_later_round",
    "single_round": "single_round",
}

#: What a finding was checked against (its `source`), in the reviewer's words.
CHECK_WORDS: Final[Mapping[str, str]] = {
    "architect": "Against the architect",
    "brand_prototype": "Against the brand prototype",
    "rules": "GV rule",
    "vendor_arithmetic": "Vendor's own sum",
    "gv_review": "GV Review's recorded check",
    "cross_view": "Vendor's views disagree",
    "completeness": "Missing from the vendor's set",
}

#: What an item touches, from `impact.impact_of`'s first words, shortened for a table.
KIND_WORDS: Final[Mapping[str, str]] = {
    "changes fabrication (a missing or extra part)": "Missing or extra part",
    "changes fabrication (a cut-out or appliance opening)": "Cut-out or appliance opening",
    "changes fabrication (overall size)": "Overall size",
    "changes fabrication (a height)": "Height",
    "changes fabrication (parts or their arrangement)": "Parts or their arrangement",
    "changes fabrication (an outlet or power unit)": "Outlet or power",
    "changes a size": "Width, depth or length",
    "changes hardware": "Hardware",
    "cosmetic (a tag, label or note)": "Tag, label or note",
    "a dimension": "Other dimension",
}

_OPEN_STATUSES: Final = frozenset({"different", "check"})
#: How many entries each scorecard list keeps.
_TOP: Final = 5


@dataclass(frozen=True, slots=True)
class TrackerSet:
    """One drawing set (a package) of the project."""

    package_id: UUID
    name: str
    vendor: str | None
    product: str | None


@dataclass(frozen=True, slots=True)
class TrackerAnswer:
    """One stored answer with findings: each at its newest correction, with its latest decision."""

    package_id: UUID
    chat_id: UUID
    message_id: UUID
    at: datetime
    kind: Literal["full_check", "question"]
    title: str
    stopped: str | None
    findings: Sequence[Mapping[str, Any]]


def vendor_key(vendor: str | None) -> str:
    """The vendor a scorecard counts under: its name, case and spacing aside ('' = unnamed)."""
    return " ".join((vendor or "").split()).lower()


def item_kind(finding: Mapping[str, Any]) -> str:
    """What the item touches, in a few words ("Overall size"), from the impact score's reason."""
    found = impact_of({**finding, "status": "different"})
    words = str((found or {}).get("why") or "a dimension").split("; ")[0]
    return KIND_WORDS.get(words, words[:1].upper() + words[1:])


def _page(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _box(value: object) -> tuple[str | None, int | None, tuple[float, float, float, float]] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        corners = (
            float(value["x0"]),
            float(value["y0"]),
            float(value["x1"]),
            float(value["y1"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    file = value.get("file")
    return (None if file is None else str(file)), _page(value.get("page")), corners


def _gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return float((dx * dx + dy * dy) ** 0.5)


@dataclass(slots=True)
class _Sighting:
    index: int
    answer: TrackerAnswer
    answer_index: int
    finding: Mapping[str, Any]
    """The finding as shown (for a folded page: its `also_on` entry over the carrier)."""
    carrier_id: str
    """The live finding that carries it: the one a click opens."""
    folded: bool
    page: int | None
    explicit_round: int | None
    status: str
    round_status: RoundStatus | None
    decision: Mapping[str, Any] | None
    key: str
    round: int = 1

    @property
    def counts(self) -> bool:
        """Whether this sighting says something about the item: a `same` that was not compared
        does not."""
        return not (self.status == "same" and self.finding.get("compared") is False)


#: An item named only by a record id (GV Review's recorded differences, `air:<uuid>`).
_ID_ONLY: Final = re.compile(r"^[a-z][a-z_-]*:[0-9a-f-]{8,}$", re.IGNORECASE)
_SENTENCE: Final = re.compile(r"^(.+?[.!?])(?:\s|$)")
_ITEM_CHARS: Final = 90


def item_words(finding: Mapping[str, Any]) -> str:
    """The item's words for a person. A finding named only by a record id is named by the first
    sentence of its reason instead ("Vendor overall width is 30" vs architect 32".")."""
    item = " ".join(str(finding.get("item") or "").split())
    if item and not _ID_ONLY.match(item):
        return item
    why = " ".join(str(finding.get("why") or "").split())
    # The evaluator's second opinion is appended to the reason; the first part names the item.
    why = re.split(r"\s*\bChecked again by\b", why, maxsplit=1)[0]
    found = _SENTENCE.match(why)
    words = (found.group(1) if found else why).rstrip(".")
    words = words[:1].upper() + words[1:]
    if not words:
        return "GV Review's recorded difference"
    return words if len(words) <= _ITEM_CHARS else words[: _ITEM_CHARS - 1].rstrip() + "…"


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sightings(answers: Sequence[TrackerAnswer]) -> list[_Sighting]:
    out: list[_Sighting] = []
    for answer_index, answer in enumerate(answers):
        for finding in answer.findings:
            finding_id = str(finding.get("id") or "")
            if not finding_id:
                continue
            raw_status = str(finding.get("round_status") or "")
            decision = finding.get("decision")
            out.append(
                _Sighting(
                    index=len(out),
                    answer=answer,
                    answer_index=answer_index,
                    finding=finding,
                    carrier_id=finding_id,
                    folded=False,
                    page=_page(finding.get("page")),
                    explicit_round=_int_or_none(finding.get("round")),
                    status=str(finding.get("status") or ""),
                    round_status=ROUND_STATUS_WORDS.get(raw_status),
                    decision=decision if isinstance(decision, Mapping) else None,
                    key=round_item_key(finding),
                )
            )
            for entry in finding.get("also_on") or []:
                if not isinstance(entry, Mapping):
                    continue
                shown = {
                    **finding,
                    "id": entry.get("finding_id") or finding_id,
                    "page": entry.get("page"),
                    "vs_page": entry.get("vs_page"),
                    "item": entry.get("item") or finding.get("item"),
                    "vendor": entry.get("vendor") or finding.get("vendor"),
                    "architect": entry.get("architect") or finding.get("architect"),
                    "vendor_box": entry.get("vendor_box"),
                    "round": entry.get("round"),
                    # Only "Your decision" items are folded into another (#1554).
                    "status": "check",
                    "round_status": None,
                    "decision": None,
                    "dims": None,
                    "record_id": None,
                }
                out.append(
                    _Sighting(
                        index=len(out),
                        answer=answer,
                        answer_index=answer_index,
                        finding=shown,
                        carrier_id=finding_id,
                        folded=True,
                        page=_page(entry.get("page")),
                        explicit_round=_int_or_none(entry.get("round")),
                        status="check",
                        round_status=None,
                        decision=None,
                        key=round_item_key(shown),
                    )
                )
    return out


def _fill_rounds(sightings: Sequence[_Sighting]) -> None:
    """A sighting with no round takes the round its page was given elsewhere, else round 1."""
    by_page: dict[int, dict[int, int]] = {}
    for sighting in sightings:
        if sighting.page is not None and sighting.explicit_round is not None:
            counts = by_page.setdefault(sighting.page, {})
            counts[sighting.explicit_round] = counts.get(sighting.explicit_round, 0) + 1
    for sighting in sightings:
        if sighting.explicit_round is not None:
            sighting.round = max(1, sighting.explicit_round)
            continue
        given = by_page.get(sighting.page or 0)
        sighting.round = (
            max(1, min(given.items(), key=lambda kv: (-kv[1], kv[0]))[0]) if given else 1
        )


class _Groups:
    """Union-find over sightings."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, at: int) -> int:
        while self.parent[at] != at:
            self.parent[at] = self.parent[self.parent[at]]
            at = self.parent[at]
        return at

    def join(self, a: int, b: int) -> None:
        first, second = self.find(a), self.find(b)
        if first != second:
            self.parent[max(first, second)] = min(first, second)


def _link_inside_answers(sightings: Sequence[_Sighting], groups: _Groups) -> None:
    by_answer: dict[int, list[_Sighting]] = {}
    for sighting in sightings:
        by_answer.setdefault(sighting.answer_index, []).append(sighting)
    for members in by_answer.values():
        # The resolver's fold: a folded page is the item that carries it.
        mains = {s.carrier_id: s for s in members if not s.folded}
        for sighting in members:
            if sighting.folded and sighting.carrier_id in mains:
                groups.join(sighting.index, mains[sighting.carrier_id].index)
        # The round link by item words, round N's k-th (by page) with round N+1's k-th.
        by_key: dict[str, dict[int, list[_Sighting]]] = {}
        for sighting in members:
            if sighting.key:
                by_key.setdefault(sighting.key, {}).setdefault(sighting.round, []).append(sighting)
        for rounds in by_key.values():
            ordered = sorted(rounds)
            for earlier, later in pairwise(ordered):
                first = sorted(rounds[earlier], key=lambda s: (s.page or 0, s.index))
                second = sorted(rounds[later], key=lambda s: (s.page or 0, s.index))
                for a, b in zip(first, second, strict=False):
                    groups.join(a.index, b.index)


def _vendor_dim(sighting: _Sighting) -> tuple[str | None, int | None, str] | None:
    dims = sighting.finding.get("dims")
    vendor = dims.get("vendor") if isinstance(dims, Mapping) else None
    if not isinstance(vendor, Mapping) or not vendor.get("id"):
        return None
    file = vendor.get("file")
    return (None if file is None else str(file)), _page(vendor.get("page")), str(vendor["id"])


def _match(new: _Sighting, old: _Sighting) -> int | None:
    """How strongly two sightings of different answers are the same item (lower is stronger), or
    `None` when they are not."""
    record = new.finding.get("record_id")
    if record and record == old.finding.get("record_id"):
        return 0
    dim = _vendor_dim(new)
    if dim is not None and dim == _vendor_dim(old):
        return 1
    if new.page is None or new.page != old.page:
        return None
    new_box, old_box = _box(new.finding.get("vendor_box")), _box(old.finding.get("vendor_box"))
    if new_box is not None and old_box is not None:
        same_file = new_box[0] is None or old_box[0] is None or new_box[0] == old_box[0]
        if same_file and _gap(new_box[2], old_box[2]) <= NEAR_PT:
            return 2
    if new.key and new.key == old.key:
        return 3
    return None


def _link_across_answers(sightings: Sequence[_Sighting], groups: _Groups) -> None:
    """Each sighting joins its best match in every earlier answer of the same set."""
    earlier: dict[UUID, list[_Sighting]] = {}
    by_answer: dict[int, list[_Sighting]] = {}
    for sighting in sightings:
        by_answer.setdefault(sighting.answer_index, []).append(sighting)
    for answer_index in sorted(by_answer):
        members = by_answer[answer_index]
        package = members[0].answer.package_id
        seen = earlier.setdefault(package, [])
        for sighting in members:
            best: dict[int, tuple[int, int]] = {}
            for old in seen:
                strength = _match(sighting, old)
                if strength is None:
                    continue
                current = best.get(old.answer_index)
                if current is None or strength < current[0]:
                    best[old.answer_index] = (strength, old.index)
            for _strength, old_index in best.values():
                groups.join(sighting.index, old_index)
        seen.extend(members)


def _stamp(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def _decided(sightings: Iterable[_Sighting]) -> Mapping[str, Any] | None:
    """The latest decision on any of the item's findings."""
    latest: Mapping[str, Any] | None = None
    for sighting in sightings:
        decision = sighting.decision
        if decision is None:
            continue
        if latest is None or _stamp(decision.get("at")) >= _stamp(latest.get("at")):
            latest = decision
    return latest


def _key_of(sighting: _Sighting) -> str:
    return hashlib.sha256(
        f"{sighting.answer.message_id}:{sighting.carrier_id}:{sighting.finding.get('id')}".encode()
    ).hexdigest()[:12]


def _when(value: datetime) -> str:
    return value.isoformat()


def _sighting_out(sighting: _Sighting) -> dict[str, Any]:
    decision = sighting.decision
    return {
        "at": _when(sighting.answer.at),
        "check": sighting.answer.kind,
        "check_title": sighting.answer.title,
        "chat_id": str(sighting.answer.chat_id),
        "message_id": str(sighting.answer.message_id),
        "finding_id": sighting.carrier_id,
        "round": sighting.round,
        "page": sighting.page,
        "status": sighting.status,
        "compared": sighting.counts,
        "round_status": sighting.round_status,
        "folded": sighting.folded,
        "vendor": str(sighting.finding.get("vendor") or ""),
        "architect": str(sighting.finding.get("architect") or ""),
        "decision": None if decision is None else str(decision.get("decision") or ""),
        "decided_by": None if decision is None else str(decision.get("by") or ""),
    }


@dataclass(slots=True)
class _Item:
    key: str
    package_id: UUID
    sightings: list[_Sighting]
    per_round: dict[int, _Sighting] = field(default_factory=dict)
    standing: State = "open"
    open_kind: Literal["wrong", "decision"] | None = None
    fixed_round: int | None = None
    tags: list[str] = field(default_factory=list)
    out: dict[str, Any] = field(default_factory=dict)


def _newest(sightings: Iterable[_Sighting]) -> _Sighting:
    """The newest check's sighting; inside one check, the latest round, then its own finding over
    a folded page."""
    return max(sightings, key=lambda s: (s.answer.at, s.round, not s.folded, s.index))


def _judge(item: _Item, first_round_of_set: int) -> None:
    by_round: dict[int, list[_Sighting]] = {}
    for sighting in item.sightings:
        if sighting.counts:
            by_round.setdefault(sighting.round, []).append(sighting)
    item.per_round = {r: _newest(members) for r, members in sorted(by_round.items())}
    rounds = sorted(item.per_round)
    current = item.per_round[rounds[-1]]
    wrong_rounds = [r for r in rounds if item.per_round[r].status in _OPEN_STATUSES]
    decision = _decided(item.sightings)
    stored = current.round_status

    fixed_round: int | None = None
    if current.status == "same" and wrong_rounds and wrong_rounds[-1] < current.round:
        fixed_round = min(r for r in rounds if r > wrong_rounds[-1])
    elif current.status in _OPEN_STATUSES and stored == "fixed":
        fixed_round = current.round + 1

    if decision is not None and decision.get("decision") == "not_a_problem":
        item.standing = "not_a_problem"
    elif fixed_round is not None:
        item.standing = "fixed"
    elif current.status == "same":
        item.standing = "right_later"
    else:
        item.standing = "open"
        item.open_kind = "wrong" if current.status == "different" else "decision"
    item.fixed_round = fixed_round if item.standing == "fixed" else None

    first_round = min(s.round for s in item.sightings)
    if first_round > first_round_of_set or any(s.round_status == "new" for s in item.sightings):
        item.tags.append(f"New in round {first_round}")
    if item.standing == "open" and (
        len([r for r in wrong_rounds if item.per_round[r].status == "different"]) >= 2
        or stored == "still_wrong"
    ):
        item.tags.append("Still wrong")


def _item_out(item: _Item, sets: Mapping[UUID, TrackerSet]) -> dict[str, Any]:
    current = item.per_round[max(item.per_round)]
    mains = [s for s in item.sightings if not s.folded]
    face = current if not current.folded or not mains else _newest(mains)
    finding = current.finding if not current.folded else face.finding
    first = min(item.sightings, key=lambda s: (s.answer.at, s.round, s.index))
    decision = _decided(item.sightings)
    ever_wrong = any(s.status == "different" for s in item.sightings)
    found_impact = impact_of(finding) if item.standing == "open" else None
    the_set = sets.get(item.package_id)
    category = str(face.finding.get("category") or "other")
    source = str(face.finding.get("source") or "")
    history = sorted(item.sightings, key=lambda s: (s.answer.at, s.round, s.index))
    return {
        "key": item.key,
        "package_id": str(item.package_id),
        "set_name": the_set.name if the_set else "",
        "vendor_name": (the_set.vendor or "").strip() if the_set else "",
        "vendor_key": vendor_key(the_set.vendor if the_set else None),
        "item": item_words(current.finding) or item_words(face.finding),
        "page": current.page,
        "vendor_value": str(current.finding.get("vendor") or ""),
        "architect_value": str(current.finding.get("architect") or ""),
        "category": category,
        "category_label": registry().category_label(category),
        "kind": item_kind(face.finding),
        "check": source,
        "check_label": CHECK_WORDS.get(source, "Other check"),
        "state": item.standing,
        "open_kind": item.open_kind,
        "status_now": current.status,
        "ever_wrong": ever_wrong,
        "fixed_round": item.fixed_round,
        "tags": item.tags,
        "first_round": min(s.round for s in item.sightings),
        "round_now": current.round,
        "rounds": sorted({s.round for s in item.sightings}),
        "first_seen_at": _when(first.answer.at),
        "last_seen_at": _when(max(s.answer.at for s in item.sightings)),
        "checks_seen": len({s.answer.message_id for s in item.sightings}),
        "impact": None if found_impact is None else int(found_impact["score"]),
        "decision": None if decision is None else str(decision.get("decision") or ""),
        "decided_by": None if decision is None else str(decision.get("by") or ""),
        "decided_at": None if decision is None else _stamp(decision.get("at")),
        "link": {
            "package_id": str(item.package_id),
            "chat_id": str(face.answer.chat_id),
            "message_id": str(face.answer.message_id),
            "finding_id": face.carrier_id,
        },
        "history": [_sighting_out(s) for s in history],
    }


def _share(part: int, whole: int) -> str | None:
    if whole <= 0:
        return None
    return str((Decimal(100 * part) / Decimal(whole)).quantize(Decimal(1)))


def _ranked(values: Iterable[str], total: int) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP]
    return [
        {"label": label, "count": count, "share": _share(count, total)} for label, count in ranked
    ]


def _average(values: Sequence[int]) -> str | None:
    if not values:
        return None
    return str((Decimal(sum(values)) / Decimal(len(values))).quantize(Decimal("0.1")))


def _open_in(sighting: _Sighting, item: _Item) -> bool:
    return sighting.status in _OPEN_STATUSES and item.standing != "not_a_problem"


def build_tracker(sets: Sequence[TrackerSet], answers: Sequence[TrackerAnswer]) -> dict[str, Any]:
    """The tracker of a project: totals, its drawing sets (rounds, checks, open counts), every item
    with its history, and the vendor scorecard. `answers` in any order."""
    ordered = sorted(answers, key=lambda a: (a.at, str(a.message_id)))
    sightings = _sightings(ordered)
    by_package: dict[UUID, list[_Sighting]] = {}
    for sighting in sightings:
        by_package.setdefault(sighting.answer.package_id, []).append(sighting)
    for members in by_package.values():
        _fill_rounds(members)
    groups = _Groups(len(sightings))
    _link_inside_answers(sightings, groups)
    _link_across_answers(sightings, groups)

    grouped: dict[int, list[_Sighting]] = {}
    for sighting in sightings:
        grouped.setdefault(groups.find(sighting.index), []).append(sighting)
    set_rounds: dict[UUID, set[int]] = {}
    for sighting in sightings:
        set_rounds.setdefault(sighting.answer.package_id, set()).add(sighting.round)

    known = {s.package_id: s for s in sets}
    items: list[_Item] = []
    for members in grouped.values():
        if not any(s.status in _OPEN_STATUSES for s in members):
            continue  # right everywhere: never an item to track
        counted = [s for s in members if s.counts]
        if not counted:
            continue
        first = min(members, key=lambda s: (s.answer.at, s.index))
        item = _Item(key=_key_of(first), package_id=first.answer.package_id, sightings=members)
        _judge(item, min(set_rounds.get(item.package_id) or {1}))
        item.out = _item_out(item, known)
        items.append(item)

    def order(item: _Item) -> tuple[int, int, str, str]:
        # Wrong before Your decision (the reviewer's path), then fixed, then the rest.
        rank = (
            (0 if item.open_kind == "wrong" else 1)
            if item.standing == "open"
            else {"fixed": 2, "right_later": 3, "not_a_problem": 4}[item.standing]
        )
        return (rank, -(item.out["impact"] or 0), item.out["set_name"], item.out["item"])

    items.sort(key=order)

    sets_out = _sets_out(sets, ordered, items, set_rounds)
    return {
        "totals": {
            "sets": len(sets_out),
            "checks": len(ordered),
            "items": len(items),
            "open": sum(1 for i in items if i.standing == "open"),
            "open_wrong": sum(1 for i in items if i.open_kind == "wrong"),
            "open_decision": sum(1 for i in items if i.open_kind == "decision"),
            "fixed": sum(1 for i in items if i.standing == "fixed"),
            "not_a_problem": sum(1 for i in items if i.standing == "not_a_problem"),
            "right_later": sum(1 for i in items if i.standing == "right_later"),
        },
        "sets": sets_out,
        "items": [item.out for item in items],
        "vendors": _vendors_out(sets, items, set_rounds),
    }


def _sets_out(
    sets: Sequence[TrackerSet],
    answers: Sequence[TrackerAnswer],
    items: Sequence[_Item],
    set_rounds: Mapping[UUID, set[int]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for the_set in sets:
        checks = [a for a in answers if a.package_id == the_set.package_id]
        if not checks:
            continue
        mine = [i for i in items if i.package_id == the_set.package_id]
        rounds = []
        for number in sorted(set_rounds.get(the_set.package_id) or {1}):
            here = [(i, i.per_round[number]) for i in mine if number in i.per_round]
            rounds.append(
                {
                    "round": number,
                    "items": len(here),
                    "open": sum(1 for i, s in here if _open_in(s, i)),
                    "wrong": sum(1 for i, s in here if s.status == "different" and _open_in(s, i)),
                    "decision": sum(1 for i, s in here if s.status == "check" and _open_in(s, i)),
                    "fixed": sum(1 for i in mine if i.fixed_round == number),
                }
            )
        out.append(
            {
                "package_id": str(the_set.package_id),
                "name": the_set.name,
                "vendor_name": (the_set.vendor or "").strip(),
                "vendor_key": vendor_key(the_set.vendor),
                "product": the_set.product,
                "items": len(mine),
                "open": sum(1 for i in mine if i.standing == "open"),
                "fixed": sum(1 for i in mine if i.standing == "fixed"),
                "not_a_problem": sum(1 for i in mine if i.standing == "not_a_problem"),
                "rounds": rounds,
                "last_checked_at": _when(max(a.at for a in checks)),
                "checks": [_check_out(a) for a in sorted(checks, key=lambda a: a.at, reverse=True)],
            }
        )
    # Most open first; among equals, the most recently checked.
    out.sort(key=lambda s: str(s["last_checked_at"]), reverse=True)
    out.sort(key=lambda s: -int(s["open"]))
    return out


def _check_out(answer: TrackerAnswer) -> dict[str, Any]:
    statuses = [str(f.get("status") or "") for f in answer.findings]
    rounds = sorted(
        {r for f in answer.findings if isinstance(r := f.get("round"), int) and r >= 1} or {1}
    )
    return {
        "chat_id": str(answer.chat_id),
        "message_id": str(answer.message_id),
        "at": _when(answer.at),
        "kind": answer.kind,
        "title": answer.title,
        "stopped": answer.stopped,
        "rounds": rounds,
        "wrong": statuses.count("different"),
        "decision": statuses.count("check"),
        "right": statuses.count("same"),
    }


def _vendors_out(
    sets: Sequence[TrackerSet], items: Sequence[_Item], set_rounds: Mapping[UUID, set[int]]
) -> list[dict[str, Any]]:
    names: dict[str, str] = {}
    packages: dict[str, list[UUID]] = {}
    for the_set in sets:
        key = vendor_key(the_set.vendor)
        names.setdefault(key, (the_set.vendor or "").strip() or "Unnamed vendor")
        packages.setdefault(key, []).append(the_set.package_id)
    out: list[dict[str, Any]] = []
    for key, package_ids in packages.items():
        mine = [i for i in items if i.package_id in package_ids]
        reviewed = [p for p in package_ids if p in set_rounds]
        if not reviewed:
            continue
        # A mistake: Wrong at least once, and neither a reviewer's "Not a problem" nor the AI's
        # own changed reading of the same round.
        mistakes = [
            i
            for i in mine
            if i.out["ever_wrong"] and i.standing not in ("not_a_problem", "right_later")
        ]
        # Fixed on the next round: Wrong in round N and seen again in round N+1 (or the stored
        # round status says what round N+1 did). An item round N+1 never showed is not counted
        # either way: nobody knows yet.
        fixed_next = of_next = 0
        for item in mistakes:
            for number, sighting in item.per_round.items():
                if sighting.status != "different":
                    continue
                told = sighting.round_status in ("fixed", "still_wrong")
                if number + 1 not in item.per_round and not told:
                    continue
                of_next += 1
                if item.fixed_round == number + 1:
                    fixed_next += 1
                break
        per_round = [
            sum(
                1
                for i in mine
                if i.package_id == package_id
                and number in i.per_round
                and _open_in(i.per_round[number], i)
            )
            for package_id in reviewed
            for number in sorted(set_rounds[package_id])
        ]
        out.append(
            {
                "vendor_key": key,
                "vendor_name": names[key],
                "sets": len(reviewed),
                "rounds": sum(len(set_rounds[p]) for p in reviewed),
                "items": len(mine),
                "mistakes": len(mistakes),
                "open": sum(1 for i in mine if i.standing == "open"),
                "fixed_next_round": fixed_next,
                "fixed_next_of": of_next,
                "fixed_next_share": _share(fixed_next, of_next),
                "average_open_per_round": _average(per_round),
                "by_product": _ranked((i.out["category_label"] for i in mistakes), len(mistakes)),
                "by_kind": _ranked((i.out["kind"] for i in mistakes), len(mistakes)),
                "by_check": _ranked((i.out["check_label"] for i in mistakes), len(mistakes)),
            }
        )
    out.sort(key=lambda v: (-v["mistakes"], v["vendor_name"].lower()))
    return out


# ---- stable comment numbers (#1622) -------------------------------------------------------------


def comment_label(number: int) -> str:
    """ "C-01", "C-12", "C-104"."""
    return f"C-{number:02d}"


def comment_numbers(answers: Sequence[TrackerAnswer]) -> dict[tuple[str, str], str]:
    """Stable comment numbers for one drawing set's checks (#1622): "C-01", "C-02", … one per
    tracked item that was ever Wrong, the same item linked across checks and rounds exactly as the
    tracker links it (`build_tracker`). Numbers go in the order each item was FIRST Wrong: the
    oldest check first and, inside one check, biggest first (`impact.by_impact`); so a later check
    never changes an earlier item's number, and a new item gets the next free one. The impact order
    stays the order things are listed in, not the number. Keyed by (message id, finding id) of every
    finding of the item, so any check's finding finds its number. A Wrong status is the finding's
    own (never a reviewer's later click), so a decision never renumbers anything."""
    ordered = sorted(answers, key=lambda a: (a.at, str(a.message_id)))
    sightings = _sightings(ordered)
    by_package: dict[UUID, list[_Sighting]] = {}
    for sighting in sightings:
        by_package.setdefault(sighting.answer.package_id, []).append(sighting)
    for members in by_package.values():
        _fill_rounds(members)
    groups = _Groups(len(sightings))
    _link_inside_answers(sightings, groups)
    _link_across_answers(sightings, groups)
    rank: dict[tuple[int, str], int] = {}
    for answer_index, answer in enumerate(ordered):
        for at, finding in enumerate(by_impact(answer.findings)):
            rank.setdefault((answer_index, str(finding.get("id") or "")), at)
    grouped: dict[int, list[_Sighting]] = {}
    for sighting in sightings:
        grouped.setdefault(groups.find(sighting.index), []).append(sighting)
    firsts: list[tuple[tuple[int, int, int], list[_Sighting]]] = []
    for members in grouped.values():
        wrong = [s for s in members if s.status == "different" and not s.folded]
        if not wrong:
            continue
        first = min(
            (s.answer_index, rank.get((s.answer_index, s.carrier_id), 0), s.index) for s in wrong
        )
        firsts.append((first, members))
    out: dict[tuple[str, str], str] = {}
    for number, (_first, members) in enumerate(sorted(firsts, key=lambda pair: pair[0]), start=1):
        for sighting in members:
            if not sighting.folded:
                out.setdefault(
                    (str(sighting.answer.message_id), sighting.carrier_id), comment_label(number)
                )
    return out
