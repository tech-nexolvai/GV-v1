"""Turn the candidates a person confirmed into a `pariksha-key/1` key (#1232).

**Nothing joins a key without a person.** The person writes a confirmations file naming each
candidate they checked against the drawing; only those become cases. A candidate that cannot be
converted, belongs to another agent, or is named twice is refused, never skipped. Where the
decision does not state the key's answer (a label's printed text: the reviewer corrected or typed a
value), the person states it, and a candidate confirmed without it is refused. Where the decision
does state it (a confirmed no-countertop page), the person may not replace it.

**Only a real reviewer's decision (#1267).** A candidate whose `actor_status` is not `reviewer` (a
name not on the local list, or a file made before the list existed) is refused. Walls, piece-kind,
batch and "there is a countertop" candidates are refused on purpose until P6-2 (#1271) builds
their confirmation.

The confirmations file (`smriti-confirmations/1`):

    {"format": "smriti-confirmations/1", "confirmed_by": "<who checked>",
     "key": {"agent": "akshara" | "rekha", "name": "...", "version": "..."},
     "confirm": [{"candidate": "<candidate id>"},
                 {"candidate": "<label candidate id>", "texts": ["<printed text>"]},
                 {"candidate": "<label candidate id>", "no_label": true}]}

The result is checked by `eval.pariksha.keys.load_key`, the reader Pariksha scores with, so a key
Smriti writes is a key Pariksha reads. A base key may be given: its cases are kept, the same
question confirmed again with the same answer is kept once, and a different answer is refused.

Source: issue #1232 · Verification: `tests/eval/test_smriti_confirm.py`
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from eval.pariksha.keys import (
    FAMILIES,
    KEY_FORMAT,
    AnswerKey,
    KeyFormatError,
    dump_key,
    load_key,
)
from eval.smriti.actors import REVIEWER
from eval.smriti.candidates import NEEDS_CONFIRMATION, Candidate, CandidateExport

__all__ = ["CONFIRMATIONS_FORMAT", "ConfirmationRefused", "confirm_candidates"]

CONFIRMATIONS_FORMAT: Final = "smriti-confirmations/1"
_HEADER: Final = frozenset({"format", "confirmed_by", "key", "confirm"})
_KEY: Final = frozenset({"agent", "name", "version"})
_ENTRY: Final = frozenset({"candidate", "texts", "no_label"})


class ConfirmationRefused(ValueError):
    """The confirmations cannot make a key; the message says which one and why."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfirmationRefused(f"{field} must be a non-empty string, not {value!r}")
    return value


def _unknown(data: Mapping[str, object], known: frozenset[str], where: str) -> None:
    extra = sorted(set(data) - known)
    if extra:
        raise ConfirmationRefused(f"unknown field(s) in {where}: {', '.join(extra)}")


def _stated_answer(entry: Mapping[str, object], where: str) -> dict[str, object] | None:
    if "texts" in entry and "no_label" in entry:
        raise ConfirmationRefused(f"{where}: give texts or no_label, not both")
    if "texts" in entry:
        return {"texts": entry["texts"]}
    if "no_label" in entry:
        if entry["no_label"] is not True:
            raise ConfirmationRefused(f"{where}: no_label is true or absent")
        return {"no_label": True}
    return None


#: Question families whose keys this confirm does not write yet (P6-2, #1271).
_LATER_FAMILIES: Final = {"slot-walls": "a walls case", "piece-kind": "a piece-kind case"}


def _built_later(candidate: Candidate) -> str | None:
    """What kind of case this is when P6-2 (#1271) builds its confirmation, else `None`.

    Refused on purpose, not by accident of shape: a walls case needs each end's drawn truth, a
    piece kind its row widths, a batch answer its own look at the page, and "there is a
    countertop" the box the person picks."""
    family = (candidate.question or {}).get("family")
    if family in _LATER_FAMILIES:
        return _LATER_FAMILIES[str(family)]
    if candidate.table == "no_countertop_answers":
        if candidate.recorded.get("batch") is True:
            return "an answer given in a batch click (each page needs its own look)"
        if candidate.recorded.get("answer") == "countertop":
            return "'there is a countertop' (the person picks the box)"
    return None


def _case(candidate: Candidate, stated: dict[str, object] | None, source: str) -> dict[str, Any]:
    assert candidate.question is not None
    answer = candidate.answer
    if answer is None:
        if stated is None:
            raise ConfirmationRefused(
                f"{candidate.candidate_id}: {candidate.answer_needed or 'the answer'}. Give the "
                "printed text as it appears on the drawing (texts) or say the span has no label "
                "(no_label); it is never made up from the value"
            )
        answer = stated
    elif stated is not None:
        raise ConfirmationRefused(
            f"{candidate.candidate_id}: the reviewer's decision recorded the answer "
            f"({dict(answer)}); a confirmation cannot replace it"
        )
    return {**dict(candidate.question), "answer": dict(answer), "source": source}


def confirm_candidates(
    export: CandidateExport,
    confirmations: Mapping[str, Any],
    *,
    base: AnswerKey | None = None,
) -> AnswerKey:
    """The key made of exactly the candidates the person confirmed (plus the base key's cases)."""
    if not isinstance(confirmations, Mapping):
        raise ConfirmationRefused("the confirmations are one JSON object")
    if confirmations.get("format") != CONFIRMATIONS_FORMAT:
        raise ConfirmationRefused(
            f"format must be {CONFIRMATIONS_FORMAT!r}, not {confirmations.get('format')!r}"
        )
    _unknown(confirmations, _HEADER, "the confirmations")
    confirmed_by = _text(confirmations.get("confirmed_by"), "confirmed_by")
    header = confirmations.get("key")
    if not isinstance(header, Mapping):
        raise ConfirmationRefused("key must be an object: agent, name, version")
    _unknown(header, _KEY, "key")
    agent = header.get("agent")
    if agent not in FAMILIES:
        raise ConfirmationRefused(f"key agent must be one of {tuple(FAMILIES)}, not {agent!r}")
    name = _text(header.get("name"), "key name")
    version = _text(header.get("version"), "key version")
    entries = confirmations.get("confirm")
    if not isinstance(entries, list) or not entries:
        raise ConfirmationRefused(
            "nothing is confirmed: name each candidate a person checked under confirm; nothing "
            "joins a key without that"
        )
    if base is not None and base.agent != agent:
        raise ConfirmationRefused(f"the base key is {base.agent}'s, not {agent}'s")

    by_id = {candidate.candidate_id: candidate for candidate in export.candidates}
    when = datetime.now(UTC).date().isoformat()
    seen: set[str] = set()
    cases: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        where = f"confirm entry {index + 1}"
        if not isinstance(entry, Mapping):
            raise ConfirmationRefused(f"{where} is not an object")
        _unknown(entry, _ENTRY, where)
        candidate_id = _text(entry.get("candidate"), f"{where} candidate")
        if candidate_id in seen:
            raise ConfirmationRefused(f"{candidate_id} is confirmed twice")
        seen.add(candidate_id)
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise ConfirmationRefused(
                f"{candidate_id} is not in the candidates of {export.database}"
            )
        if candidate.status != NEEDS_CONFIRMATION:
            raise ConfirmationRefused(f"{candidate_id} has status {candidate.status!r}")
        if not candidate.convertible or candidate.question is None:
            raise ConfirmationRefused(
                f"{candidate_id} is not convertible: {candidate.not_convertible}"
            )
        if candidate.actor_status != REVIEWER:
            raise ConfirmationRefused(
                f"{candidate_id} was decided by {candidate.decided_by!r} ({candidate.actor_status}); "
                "only a decision by a name on the local real-reviewer list becomes a key case "
                "(#1267)"
            )
        later = _built_later(candidate)
        if later is not None:
            raise ConfirmationRefused(
                f"{candidate_id}: {later} is confirmed in P6-2 (#1271), not by this confirm yet"
            )
        if candidate.agent != agent:
            raise ConfirmationRefused(
                f"{candidate_id} is a case for {candidate.agent}, not for {agent}'s key"
            )
        source = (
            f"smriti {candidate_id} ({candidate.corrects}; {candidate.kind} by a reviewer on "
            f"{candidate.decided_at[:10]} in {export.database}); confirmed by {confirmed_by} on "
            f"{when}"
        )
        cases.append(_case(candidate, _stated_answer(entry, where), source))

    merged: list[dict[str, Any]] = []
    known: dict[tuple[str, str, str], dict[str, Any]] = {}
    if base is not None:
        base_cases = dump_key(base)["cases"]
        assert isinstance(base_cases, list)
        for case in base_cases:
            assert isinstance(case, dict)
            known[(case["family"], case["page_sha256"], case["question_id"])] = case
            merged.append(case)
    for case in cases:
        identity = (case["family"], case["page_sha256"], case["question_id"])
        existing = known.get(identity)
        if existing is None:
            known[identity] = case
            merged.append(case)
            continue
        if _same_case(existing, case, str(agent)):
            continue
        raise ConfirmationRefused(
            f"question {identity[2]} on page {identity[1][:12]} is already in the key with another "
            f"answer or guard ({existing.get('answer')}); a key holds one answer per question"
        )
    try:
        key = load_key(
            {
                "format": KEY_FORMAT,
                "agent": agent,
                "name": name,
                "version": version,
                "cases": merged,
            }
        )
    except KeyFormatError as error:
        raise ConfirmationRefused(
            f"the confirmed cases do not make a valid key: {error}"
        ) from error
    assert isinstance(key, AnswerKey)
    return key


def _same_case(first: Mapping[str, Any], second: Mapping[str, Any], agent: str) -> bool:
    """The same question with the same answer and guards (texts compared as Pariksha folds them)."""

    def folded(case: Mapping[str, Any]) -> tuple[object, ...]:
        key = load_key(
            {
                "format": KEY_FORMAT,
                "agent": agent,
                "name": "-",
                "version": "-",
                "cases": [{**case, "source": ""}],
            }
        )
        assert isinstance(key, AnswerKey)
        only = key.cases[0]
        return (only.truth, only.slot_box, only.picture_sha256)

    try:
        return folded(first) == folded(second)
    except KeyFormatError:
        return False
