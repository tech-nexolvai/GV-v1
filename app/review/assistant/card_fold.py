"""Fold and order the cards (final plan change #8, 2026-10-26): one card per distinct question.

A full check reports one question many times: on the regular and the reversed sheet of one unit
(two vendor pages paired with the same reference page), on the plan and the elevation of one page,
and from a page helper and the whole-drawing look. `one_card` already folds copies of one item on
one page; this step, worked out on read (nothing stored), joins the copies of one question across
the sibling pages of a unit, so the reviewer reads it once.

Two items are one card only by **code facts**, all of them:

- both compare with the architect (`one_card.compares_architect`; GV's rules and the vendor's own
  checks ask another question) and are from the same round;
- the same outcome (`proof.proof_group`, after the reviewer's decisions): a red never joins a
  non-red, a Right never joins a Not compared;
- the same reason: the same Not-compared reason (a missing job value: the same values missing),
  the same kind of please-check question (`question_key`);
- the same reference page, and the same vendor value (code's inches; else "not shown"; else the
  same words);
- the same reference dimension: both name the same printed reference label. Only with no label on
  either side may two items join on the same reference value or words, and then only from
  different vendor pages (two items on one page with no label to tell them apart stay two).
  A Wrong or a Likely problem joins only on the same reference label, so one Agree may apply to
  every copy of the same proven difference (`agree_all`).

Nothing is dropped: the card's first item (the lead) carries its copies (`card.copies`), and every
copy keeps its own crop, page and decision. The same two sizes with another outcome elsewhere (a red
on one sheet, its reversed sheet's copy Not compared) are never joined, but the lead names them
(`card.related`) so the reviewer sees them.

A Right whose ends code did not prove alike (`proof.matches_unproved`, #1953) is a "Matches (ends
not proved)": in a card with a Right whose ends code proved, that one leads and it is shown under
it; alone it stays a Right with that note (`card.ends`).

Each card also names its question (`card.question`: a key and plain words), so the screen lists
please-check questions grouped by an identical reason ("the AI pairs these but code could not
confirm the same ends", "job value missing: front overhang"), and says whether the card leaves the
work list for the collapsed "Nothing to compare here" list (`card.aside`: the reference gives no
size, an honest different extent, a field-verify note). "Could not read" stays a question.

Pure code: no I/O, no AI call. Never changes a group, a value or a decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.review.assistant import (
    check_record,
    classification,
    dim_ends,
    job_value_rules,
    likely_problems,
    one_card,
    proof,
)

__all__ = [
    "ASIDE_REASONS",
    "CARD",
    "Card",
    "blocks_with_cards",
    "card_counts",
    "cards",
    "question_key",
]

#: The finding's field the screen reads (worked out on read, never stored).
CARD: Final = "card"

#: Not-compared reasons that leave the work list for the collapsed "Nothing to compare here" list:
#: the reference gives no size (or only parts), an honest different extent, a field-verify note,
#: a reference that is not the architect's, and an AI reading code never found at its spot (GV's
#: reviewer only). "Could not read" and the rest stay questions.
ASIDE_REASONS: Final = frozenset(
    {
        "nothing_to_compare",
        "no_overall",
        "different_reference",
        "field_verify",
        "finish_not_comparable",
        "prototype",
        "prototype_only",
        "round_only",
        "internal",
    }
)

#: The please-check questions of a Likely problem, by kind, in plain words.
_LOOK_WORDS: Final[Mapping[str, str]] = {
    "missing_piece": "The reference shows a piece the vendor's drawing does not",
    "extra_piece": "The vendor's drawing shows a piece the reference does not",
    "count": "The counts differ",
    "size": "A size differs, found by the whole-drawing look",
    "layout": "The layout differs",
    # The page reviewer's please-checks (`page_reviewer`): GV's reviewer's kinds B to F.
    **likely_problems.KIND_WORDS,
}
_ENDS_WORDS: Final = (
    "The AI pairs these sizes, but code could not confirm both measure the same thing"
)
_PICK_WORDS: Final = (
    "Code's drawing shows these sizes end at the same places, but a second AI look did not pair "
    "them"
)
_HELD_WORDS: Final = f"Code found a difference; {check_record.HELD_WORDS}"
_UNSIZED_WORDS: Final = "The vendor's overall runs past its sized pieces at both ends: unsized ends"


@dataclass(slots=True)
class Card:
    """One card: its items, the lead first, and what the screen says about it."""

    members: list[Mapping[str, Any]] = field(default_factory=list)
    how: str | None = None
    agree_all: bool = False
    question: tuple[str, str] | None = None
    aside: bool = False
    #: The same two sizes elsewhere with another outcome or reason: never joined, listed so the
    #: reviewer sees them (a red on one sheet, the reversed sheet's copy Not compared).
    related: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def lead(self) -> Mapping[str, Any]:
        return self.members[0]

    @property
    def pages(self) -> list[int]:
        return sorted({int(f["page"]) for f in self.members if isinstance(f.get("page"), int)})


# ---- the facts --------------------------------------------------------------------------------


def _group(finding: Mapping[str, Any]) -> str:
    given = finding.get("proof")
    if isinstance(given, Mapping) and isinstance(given.get("group"), str):
        return str(given["group"])
    return str(proof.proof_group(finding))


def _words(text: object) -> str:
    return " ".join(str(text or "").replace("″", '"').replace("”", '"').lower().split())


def _vendor_value(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Code's inches; else "not shown"; else the vendor's words; `None` when it says nothing."""
    inches = one_card._inches(finding, "vendor")
    if inches is not None:
        return ("inches", inches.normalize())
    text = finding.get("vendor")
    if classification.vendor_absent(text):
        return ("absent",)
    words = _words(text)
    return ("words", words) if words else None


def _reference_value(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    inches = one_card._inches(finding, "architect")
    if inches is not None:
        return ("inches", inches.normalize())
    words = _words(finding.get("architect"))
    return ("words", words) if words else None


def _reference_label(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    dims = finding.get("dims")
    label = dims.get("architect") if isinstance(dims, Mapping) else None
    if not isinstance(label, Mapping) or not label.get("id"):
        return None
    return (label.get("file") or None, label.get("page"), str(label["id"]))


def _reference_page(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The reference page the item was compared with: its label's, else its pair's."""
    label = _reference_label(finding)
    if label is not None and label[1] is not None:
        return (label[0], label[1])
    page = finding.get("vs_page")
    if isinstance(page, int) and not isinstance(page, bool):
        return (None, page)
    return None


def _missing_values(finding: Mapping[str, Any]) -> list[str]:
    record = finding.get(job_value_rules.FIELD)
    if not isinstance(record, Mapping):
        return []
    return [str(m) for m in record.get("missing") or () if m]


def question_key(finding: Mapping[str, Any]) -> tuple[str, str]:
    """(key, plain words) of the question the item asks, for grouping identical reasons."""
    group = _group(finding)
    if group == "likely_problem":
        if likely_problems.unsized_ends(finding) is not None:
            return ("unsized_ends", _UNSIZED_WORDS)
        if proof.ends_unconfirmed(finding):
            return ("ends", _ENDS_WORDS)
        if proof.pick_disagrees(finding):
            return ("pick", _PICK_WORDS)
        if proof.held_back(finding) is not None:
            kind = check_record.kind_of(finding)
            return (f"held:{kind}", f"{_HELD_WORDS} ({check_record.KINDS.get(kind, kind)})")
        payload = likely_problems.payload(finding)
        if payload is not None:
            kind = str(payload.get("kind") or "size")
            return (f"look:{kind}", _LOOK_WORDS.get(kind, _LOOK_WORDS["size"]))
        return ("likely", "Found, not proved: please check")
    if group == "not_compared":
        reason = str(proof.classified(finding).reason or "other")
        missing = _missing_values(finding) if reason == "missing_setting" else []
        if missing:
            values = " and ".join(missing) if len(missing) <= 2 else ", ".join(missing)
            return (f"missing_setting:{'|'.join(missing)}", f"Job value missing: {values}")
        words = classification.REASON_WORDS.get(reason, "Not compared")
        return (reason, words[:1].upper() + words[1:])
    return (group, group)


def _ends(finding: Mapping[str, Any]) -> str | None:
    """ "not_proved" for a Right whose ends code did not prove alike, "proved" when it did."""
    if proof.matches_unproved(finding):
        return "not_proved"
    held = finding.get(dim_ends.LIKE_FOR_LIKE)
    if finding.get("status") == "same" and isinstance(held, Mapping) and held.get("proved"):
        return "proved"
    return None


def _key(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """What two copies of one question share; `None` for an item that never joins another."""
    if not finding.get("id") or classification.internal(finding):
        return None
    if not one_card.compares_architect(finding):
        return None
    group = _group(finding)
    if group == "dismissed":
        return None
    page = _reference_page(finding)
    vendor = _vendor_value(finding)
    if page is None or vendor is None:
        return None
    label = _reference_label(finding)
    if label is not None:
        reference: tuple[Any, ...] = ("label", label)
    elif group in ("clouded", "likely_problem"):
        return None  # a proven difference joins another only on the same reference label
    else:
        value = _reference_value(finding)
        if value is None:
            return None
        reference = ("value", value)
    return (group, question_key(finding)[0], finding.get("round"), page, vendor, reference)


def _order(finding: Mapping[str, Any]) -> tuple[Any, ...]:
    """The lead first: ends proved before not, then `one_card`'s order (the better proved)."""
    return (0 if _ends(finding) != "not_proved" else 1, *one_card._order(finding))


def _how(members: Sequence[Mapping[str, Any]]) -> str:
    pages = {f.get("page") for f in members}
    if _reference_label(members[0]) is None:
        return "same_values"
    return "same_reference_dimension" if len(pages) > 1 else "same_page"


def cards(findings: Iterable[Mapping[str, Any]]) -> list[Card]:
    """The answer's findings as cards, in the findings' order of their leads (see the module)."""
    buckets: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    order: list[tuple[Any, ...] | int] = []
    singles: dict[int, Mapping[str, Any]] = {}
    for at, finding in enumerate(findings):
        key = _key(finding)
        if key is None:
            singles[at] = finding
            order.append(at)
            continue
        if key not in buckets:
            order.append(key)
        buckets.setdefault(key, []).append(finding)
    out: list[Card] = []
    for entry in order:
        if isinstance(entry, int):
            out.append(_card([singles[entry]]))
            continue
        members = buckets[entry]
        reference = entry[-1]
        pages = [f.get("page") for f in members]
        if reference[0] == "value" and len(set(pages)) < len(pages):
            # No label tells two items on one page apart: every copy stays its own card.
            out.extend(_card([f]) for f in members)
            continue
        out.append(_card(sorted(members, key=_order)))
    _relate(out)
    return out


def _sizes(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The same vendor value at the same printed reference dimension, whatever the outcome."""
    if not finding.get("id") or not one_card.compares_architect(finding):
        return None
    label, vendor = _reference_label(finding), _vendor_value(finding)
    if label is None or vendor is None or vendor[0] != "inches":
        return None
    return (finding.get("round"), label, vendor)


def _relate(found: Sequence[Card]) -> None:
    """Each card's `related`: the other cards' items with the same two sizes (see `Card`)."""
    by_sizes: dict[tuple[Any, ...], list[tuple[int, Mapping[str, Any]]]] = {}
    for at, card in enumerate(found):
        for member in card.members:
            sizes = _sizes(member)
            if sizes is not None:
                by_sizes.setdefault(sizes, []).append((at, member))
    for at, card in enumerate(found):
        seen: set[str] = set()
        for member in card.members:
            sizes = _sizes(member)
            for other_at, other in by_sizes.get(sizes, []) if sizes is not None else []:
                fid = str(other["id"])
                if other_at != at and fid not in seen:
                    seen.add(fid)
                    card.related.append(other)


def _card(members: list[Mapping[str, Any]]) -> Card:
    lead = members[0]
    group = _group(lead)
    question = question_key(lead) if group in ("likely_problem", "not_compared") else None
    aside = group == "not_compared" and question is not None and question[0] in ASIDE_REASONS
    return Card(
        members=members,
        how=_how(members) if len(members) > 1 else None,
        agree_all=len(members) > 1 and group in ("clouded", "likely_problem"),
        question=question,
        aside=aside,
    )


def _payload(card: Card, finding: Mapping[str, Any]) -> dict[str, Any]:
    lead = card.lead
    is_lead = finding is lead
    return {
        "id": str(lead["id"]),
        "lead": is_lead,
        "pages": card.pages,
        "copies": (
            [
                {"finding_id": str(f["id"]), "page": f.get("page"), "ends": _ends(f)}
                for f in card.members[1:]
            ]
            if is_lead
            else []
        ),
        "how": card.how,
        "agree_all": card.agree_all,
        "question": (
            {"key": card.question[0], "words": card.question[1]} if card.question else None
        ),
        "aside": card.aside,
        "ends": _ends(finding),
        "related": (
            [
                {"finding_id": str(f["id"]), "page": f.get("page"), "group": _group(f)}
                for f in card.related
            ]
            if is_lead
            else []
        ),
    }


def blocks_with_cards(blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """An answer's blocks with each finding's `card` (worked out on read; nothing stored). The
    findings should already carry their `proof` (after the reviewer's decisions)."""
    findings = [
        block["finding"]
        for block in blocks
        if block.get("type") == "finding" and isinstance(block.get("finding"), Mapping)
    ]
    if not findings:
        return [dict(block) for block in blocks]
    by_id: dict[int, dict[str, Any]] = {}
    for card in cards(findings):
        for member in card.members:
            by_id[id(member)] = _payload(card, member)
    out: list[dict[str, Any]] = []
    for block in blocks:
        finding = block.get("finding")
        if block.get("type") == "finding" and isinstance(finding, Mapping) and id(finding) in by_id:
            block = {**block, "finding": {**finding, CARD: by_id[id(finding)]}}
        out.append(dict(block))
    return out


def card_counts(findings: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many cards each group has, as the result screen's tiles count them: a card once,
    whatever its copies; the "Nothing to compare here" cards apart (`aside`), never in Not
    compared."""
    counts: dict[str, int] = dict.fromkeys(proof.GROUPS, 0)
    counts["aside"] = 0
    for card in cards(findings):
        if card.aside:
            counts["aside"] += 1
        else:
            counts[_group(card.lead)] += 1
    return counts
