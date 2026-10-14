"""Turn what a person confirmed, case by case, into Pariksha keys of every type (#1232, #1271).

**Nothing joins a key without a person.** A person looks at each ready candidate on the local review
page (`scripts/smriti_review_page.py`): the exact picture the AI saw, the proposed answer and where it
came from, and the reviewer's decision. They answer **Yes**, **No** or **Unsure**, and the page's
"Copy answers" gives this file. Only a Yes joins a key. No and Unsure are listed, never written.
Anant decided (2026-10-15) that he confirms each case by clicking, never by typing blind, and never
Claude: the AI under test is Claude, so a shared misread would grade itself right.

The confirmations file (`smriti-confirmations/2`):

    {"format": "smriti-confirmations/2", "confirmed_by": "<who looked>",
     "candidates_sha256": "<the candidates file's own hash>",
     "answers": [{"candidate": "<id>", "verdict": "yes" | "no" | "unsure", ...}]}

**What a Yes needs, by the question it answers** (anything else on an entry is refused):

* **A label** (`akshara`) whose answer is proposed (`answer_source` `pdf-text`, `ai-reading` or
  `decision`): nothing more. The proposal is never replaced: a wrong proposal is answered No.
* **A label the reviewer corrected or typed**: `texts` (the printed text, as the person reads it in
  the picture) or `no_label`, and `mistake`: `ai_misread`, `vendor_drew_it` or `unsure`. Only
  `ai_misread` joins the AI's key. **`vendor_drew_it` is kept out of the AI's key** and listed for
  the vendor report (the AI may have read the drawing right; the vendor drew it wrong). `unsure` is
  kept out.
* **"Confirm: no countertop"**: nothing more.
* **"There is a countertop"**: `box` (the numbered row on the stored picture, from 1) or
  `not_among_boxes: true`. Saying `no_countertop` contradicts the reviewer, so it is refused.
* **A page answered in a batch click** ("confirm these N pages"): its own look, stated on its own
  card: `no_countertop: true`, `box` or `not_among_boxes`. It must agree with what the batch said;
  a disagreement is refused, for a person to settle.
* **Walls** (`rekha-walls`): `left` and `right`, what the vendor drew at each end (`wall`, `open`,
  `not_shown`, `key_unsure`). The reviewer's layout is the site answer (`site_layout`, and each end's
  `site` where the layout settles it), never what was drawn.
* **A piece's kind** (`bala-kinds`): nothing more. Its `row_widths` come from the stored row
  (`stored_row_widths`), so Pariksha's kinds scorer can tell the run read the keyed row.

**Who.** Only a decision by a name on the local real-reviewer list (`actor_status` `reviewer`) is
ever written; any other is refused even when its file says it converts.

**Conflicts are refused, never merged.** Two answers giving one question different answers, an
answer that differs from a base key's, or (in `scripts/smriti_candidates.py`) from any key already
registered in the key library for that agent.

Every key is checked by its own Pariksha loader (`load_key`, `load_walls_key`, `load_kinds_key`), so
a key Smriti writes is a key Pariksha reads.

Source: issues #1232, #1271 · Verification: `tests/eval/test_smriti_confirm.py`,
`tests/scripts/test_smriti_candidates.py`
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from eval.pariksha.keys import (
    FAMILIES,
    KEY_FORMAT,
    AnswerKey,
    ArchitectAnswerKey,
    KeyFormatError,
    dump_key,
    load_key,
)
from eval.pariksha.kinds import (
    KINDS_AGENT,
    KINDS_FAMILY,
    KindsKey,
    dump_kinds_key,
    load_kinds_key,
)
from eval.pariksha.walls import (
    DRAWN,
    KEY_UNSURE,
    WALLS_AGENT,
    WALLS_FAMILY,
    WallsKey,
    dump_walls_key,
    load_walls_key,
)
from eval.smriti.actors import REVIEWER
from eval.smriti.candidates import (
    MISTAKE_ANSWERS,
    MISTAKE_UNANSWERED,
    NEEDS_CONFIRMATION,
    Candidate,
    CandidateExport,
)

__all__ = [
    "CONFIRMATIONS_FORMAT",
    "KEY_AGENTS",
    "VERDICTS",
    "ConfirmationRefused",
    "Confirmed",
    "QuestionType",
    "confirm_candidates",
    "not_ready",
    "question_type",
    "ready_candidates",
    "site_ends",
    "stored_row_widths",
]

CONFIRMATIONS_FORMAT: Final = "smriti-confirmations/2"
YES: Final = "yes"
NO: Final = "no"
UNSURE: Final = "unsure"
VERDICTS: Final = (YES, NO, UNSURE)
AI_MISREAD: Final = "ai_misread"
VENDOR_DREW_IT: Final = "vendor_drew_it"
#: The key each question family is written into, by the key's agent.
KEY_AGENTS: Final[Mapping[str, str]] = {
    FAMILIES["akshara"]: "akshara",
    FAMILIES["rekha"]: "rekha",
    WALLS_FAMILY: WALLS_AGENT,
    KINDS_FAMILY: KINDS_AGENT,
}
#: What each end of the row is on site, as the reviewer's layout settles it. `back_only` does not
#: settle the ends: the stone may stop at panels with walls behind them (#1138), so it is unsure.
_SITE_ENDS: Final[Mapping[str, tuple[str, str]]] = {
    "back_left_right": ("wall", "wall"),
    "back_and_left": ("wall", "open"),
    "back_and_right": ("open", "wall"),
    "island": ("open", "open"),
}

_HEADER: Final = frozenset({"format", "confirmed_by", "candidates_sha256", "answers"})
_COMMON: Final = frozenset({"candidate", "verdict", "note"})


class QuestionType:
    """What a ready candidate asks the person, which decides what a Yes must carry."""

    LABEL_PROPOSED: Final = "label-proposed"
    LABEL_STATED: Final = "label-stated"
    ROW_STATED: Final = "row-stated"
    ROW_BOX: Final = "row-box"
    ROW_BATCH: Final = "row-batch"
    WALLS: Final = "walls"
    PIECE_KIND: Final = "piece-kind"


#: The fields a Yes may carry, per question type (beyond candidate, verdict and note).
_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    QuestionType.LABEL_PROPOSED: frozenset(),
    QuestionType.LABEL_STATED: frozenset({"texts", "no_label", "mistake"}),
    QuestionType.ROW_STATED: frozenset(),
    QuestionType.ROW_BOX: frozenset({"box", "not_among_boxes", "no_countertop"}),
    QuestionType.ROW_BATCH: frozenset({"box", "not_among_boxes", "no_countertop"}),
    QuestionType.WALLS: frozenset({"left", "right"}),
    QuestionType.PIECE_KIND: frozenset(),
}
_ALL_FIELDS: Final = frozenset().union(*_FIELDS.values()) | _COMMON


class ConfirmationRefused(ValueError):
    """The confirmations cannot make keys; the message says which entry and why."""


Key = AnswerKey | WallsKey | KindsKey


@dataclass(frozen=True, slots=True)
class Confirmed:
    """What the person's answers made."""

    keys: Mapping[str, Key]
    """One key per agent that got at least one Yes, each read back by its Pariksha loader."""
    documents: Mapping[str, dict[str, object]]
    """Each key as its `pariksha-key/1` JSON."""
    joined: tuple[str, ...]
    """The candidates written into a key."""
    vendor: tuple[str, ...]
    """Corrections the person said the vendor drew wrong: kept out of the AI's key, for the vendor
    report."""
    kept_out: tuple[tuple[str, str], ...]
    """Answered, and not written: `(candidate, why)` for a No, an Unsure, or an unsure mistake."""


# --- which candidates a person can confirm --------------------------------------------------------


def _batch(candidate: Candidate) -> bool:
    return bool(candidate.recorded.get("batch"))


def question_type(candidate: Candidate) -> str | None:
    """What this candidate asks the person, or `None` when no key takes its question."""
    family = None if candidate.question is None else candidate.question.get("family")
    if family == FAMILIES["akshara"]:
        return (
            QuestionType.LABEL_PROPOSED
            if candidate.answer is not None
            else (QuestionType.LABEL_STATED)
        )
    if family == FAMILIES["rekha"]:
        if _batch(candidate):
            return QuestionType.ROW_BATCH
        return QuestionType.ROW_STATED if candidate.answer is not None else QuestionType.ROW_BOX
    if family == WALLS_FAMILY:
        return QuestionType.WALLS
    if family == KINDS_FAMILY:
        return QuestionType.PIECE_KIND
    return None


def not_ready(candidate: Candidate) -> str | None:
    """Why a person cannot confirm this candidate into a key, or `None` when they can."""
    if candidate.status != NEEDS_CONFIRMATION:
        return f"it has status {candidate.status!r}"
    if not candidate.current:
        return f"it is a {candidate.kind} record, not a current decision"
    if not candidate.convertible or candidate.question is None:
        return f"it is not convertible: {candidate.not_convertible or 'no question identity'}"
    if question_type(candidate) is None:
        return f"no key takes its question family ({candidate.question.get('family')!r})"
    if candidate.actor_status != REVIEWER:
        return (
            f"it was decided by {candidate.decided_by!r} (actor status "
            f"{candidate.actor_status!r}), who is not on the local list of real reviewers; only "
            "a real reviewer's decision is ever written into a key"
        )
    return None


def ready_candidates(export: CandidateExport) -> tuple[Candidate, ...]:
    """The candidates a person can confirm, in the export's order: what the review page shows."""
    return tuple(candidate for candidate in export.candidates if not_ready(candidate) is None)


def site_ends(layout: str) -> tuple[str, str]:
    """The left and right ends' site answer the reviewer's layout settles (`key_unsure` if none)."""
    return _SITE_ENDS.get(layout, (KEY_UNSURE, KEY_UNSURE))


def _layout(candidate: Candidate) -> str:
    values = [value for value in candidate.recorded.values() if isinstance(value, str)]
    if len(values) != 1:
        raise ConfirmationRefused(
            f"{candidate.candidate_id}: the reviewer's wall layout cannot be read from "
            f"{dict(candidate.recorded)}"
        )
    return values[0]


# --- the stored row of a piece -------------------------------------------------------------------


def stored_row_widths(
    session: Session, candidates: Iterable[Candidate]
) -> dict[str, list[str | None]]:
    """Each piece-kind candidate's row widths in inches, from the row the run stored. Read-only.

    The row is the one Bala's link names (`part_proposal_rows`: its run and its anchor's page); its
    pieces are the slot reader's spans `slot:0..n-1` there, as the exporter counted them. A width
    the run holds no value for is `None`. A candidate whose row cannot be read again with exactly
    the piece count its question names is left out, so confirming it is refused with the reason."""
    from app.models.drawing import PartConfirmation, PartProposalRow
    from app.models.evidence import ObservationCandidate
    from units.measurement import Unit

    widths: dict[str, list[str | None]] = {}
    for candidate in candidates:
        question = candidate.question
        if question is None or question.get("family") != KINDS_FAMILY:
            continue
        if candidate.table != "part_confirmations":
            continue
        try:
            decision_id = UUID(candidate.decision_id)
        except ValueError:
            continue
        proposal_id = session.scalar(
            select(PartConfirmation.part_proposal_id).where(PartConfirmation.id == decision_id)
        )
        link = (
            None
            if proposal_id is None
            else session.scalar(
                select(PartProposalRow).where(PartProposalRow.part_proposal_id == proposal_id)
            )
        )
        anchor = None if link is None else session.get(ObservationCandidate, link.row_candidate_id)
        if link is None or anchor is None:
            continue
        values: dict[int, str | None] = {}
        for span in session.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.extraction_run_id == link.extraction_run_id,
                ObservationCandidate.page_id == anchor.page_id,
                ObservationCandidate.ambiguity_flags.contains(["slot-reader"]),
            )
        ):
            numbers = [
                flag.removeprefix("slot:")
                for flag in span.ambiguity_flags or ()
                if isinstance(flag, str)
                and flag.startswith("slot:")
                and flag.removeprefix("slot:").isdigit()
            ]
            if len(numbers) != 1:
                continue
            slot = int(numbers[0])
            numerator, denominator = span.value_numerator, span.value_denominator
            exact = (
                Fraction(numerator, denominator)
                if numerator is not None
                and denominator
                and numerator > 0
                and span.unit == Unit.INCH.value
                else None
            )
            if slot in values and values[slot] != (None if exact is None else str(exact)):
                values = {}
                break
            values[slot] = None if exact is None else str(exact)
        pieces = question.get("pieces")
        if isinstance(pieces, int) and set(values) == set(range(pieces)):
            widths[candidate.candidate_id] = [values[slot] for slot in range(pieces)]
    return widths


# --- reading the answers ---------------------------------------------------------------------


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfirmationRefused(f"{field} must be a non-empty string, not {value!r}")
    return value


def _unknown(data: Mapping[str, object], known: frozenset[str], where: str) -> None:
    extra = sorted(set(data) - known)
    if extra:
        raise ConfirmationRefused(f"unknown field(s) in {where}: {', '.join(extra)}")


def _row_pick(entry: Mapping[str, Any], where: str) -> dict[str, Any] | None:
    """The row answer the person stated (`box`, `not_among_boxes`, `no_countertop`), or None."""
    given = [field for field in ("box", "not_among_boxes", "no_countertop") if field in entry]
    if not given:
        return None
    if len(given) > 1:
        raise ConfirmationRefused(f"{where}: give one of {', '.join(given)}, not both")
    field = given[0]
    if field == "box":
        box = entry["box"]
        if isinstance(box, bool) or not isinstance(box, int) or box < 1:
            raise ConfirmationRefused(
                f"{where}: box is the numbered row on the picture, a whole number from 1, not "
                f"{box!r}"
            )
        return {"box": box}
    if entry[field] is not True:
        raise ConfirmationRefused(f"{where}: {field} is true or absent")
    return {"kind": field}


def _label_answer(entry: Mapping[str, Any], where: str) -> dict[str, Any] | None:
    if "texts" in entry and "no_label" in entry:
        raise ConfirmationRefused(f"{where}: give texts or no_label, not both")
    if "texts" in entry:
        texts = entry["texts"]
        if not isinstance(texts, list) or not texts:
            raise ConfirmationRefused(f"{where}: texts is a list of the printed texts")
        return {"texts": [_text(text, f"{where} texts") for text in texts]}
    if "no_label" in entry:
        if entry["no_label"] is not True:
            raise ConfirmationRefused(f"{where}: no_label is true or absent")
        return {"no_label": True}
    return None


@dataclass(frozen=True, slots=True)
class _Case:
    agent: str
    document: dict[str, Any]


def _source(candidate: Candidate, export: CandidateExport, confirmed_by: str, said: str) -> str:
    when = datetime.now(UTC).date().isoformat()
    return (
        f"smriti {candidate.candidate_id} ({candidate.corrects}; {candidate.kind} by a reviewer on "
        f"{candidate.decided_at[:10]} in {export.database}); {said}confirmed by {confirmed_by} on "
        f"{when}"
    )


def _case(
    candidate: Candidate,
    entry: Mapping[str, Any],
    where: str,
    row_widths: Mapping[str, Sequence[str | None]],
) -> tuple[dict[str, Any] | None, str, str | None]:
    """The key case a Yes makes (without its source), a note for the source, and, when the Yes
    does not join the AI's key, why (`vendor` for a vendor error)."""
    assert candidate.question is not None
    kind = question_type(candidate)
    question = dict(candidate.question)
    cid = candidate.candidate_id
    mistake = entry.get("mistake")
    if mistake is not None and candidate.mistake_needed != MISTAKE_UNANSWERED:
        raise ConfirmationRefused(
            f"{cid}: 'AI misread or vendor drew it wrong' is not asked of this candidate"
        )

    if kind == QuestionType.LABEL_PROPOSED:
        assert candidate.answer is not None
        return (
            {**question, "answer": dict(candidate.answer)},
            (
                f"answer from {candidate.answer_source or 'the decision'} checked against the "
                "picture; "
            ),
            None,
        )

    if kind == QuestionType.LABEL_STATED:
        if candidate.mistake_needed == MISTAKE_UNANSWERED:
            if mistake is None:
                raise ConfirmationRefused(
                    f"{cid}: say whether the AI misread it or the vendor drew it wrong (mistake: "
                    f"one of {MISTAKE_ANSWERS}); a vendor error never enters the AI's key"
                )
            if mistake not in MISTAKE_ANSWERS:
                raise ConfirmationRefused(
                    f"{cid}: mistake must be one of {MISTAKE_ANSWERS}, not {mistake!r}"
                )
            if mistake == VENDOR_DREW_IT:
                return None, "", "vendor"
            if mistake != AI_MISREAD:
                return (
                    None,
                    "",
                    (
                        "the person was not sure whether the AI misread it or the vendor drew it "
                        "wrong; kept out of the key"
                    ),
                )
        answer = _label_answer(entry, where)
        if answer is None:
            raise ConfirmationRefused(
                f"{cid}: {candidate.answer_needed or 'the answer'}. Give the printed text as it "
                "appears in the picture (texts) or say the span has no label (no_label); it is "
                "never made up from the value"
            )
        said = "AI misread (said by the person); " if mistake == AI_MISREAD else ""
        return {**question, "answer": answer}, f"{said}printed text stated from the picture; ", None

    if kind == QuestionType.ROW_STATED:
        assert candidate.answer is not None
        return {**question, "answer": dict(candidate.answer)}, "", None

    if kind in (QuestionType.ROW_BOX, QuestionType.ROW_BATCH):
        pick = _row_pick(entry, where)
        recorded = candidate.recorded.get("answer")
        if pick is None:
            if kind == QuestionType.ROW_BATCH:
                raise ConfirmationRefused(
                    f"{cid}: this page was answered with others in one batch click; it needs its "
                    "own look: state this page's answer on its own card (no_countertop, box or "
                    "not_among_boxes)"
                )
            raise ConfirmationRefused(
                f"{cid}: there is a countertop, so pick its box on the stored picture (box: n) "
                "or say it is not among the boxes (not_among_boxes: true)"
            )
        says_none = pick == {"kind": "no_countertop"}
        if says_none != (recorded == "no_countertop"):
            raise ConfirmationRefused(
                f"{cid}: this answer ({pick}) conflicts with the reviewer's decision "
                f"({recorded!r}); a person settles the disagreement before it joins a key"
            )
        note = (
            "own look at this page (it was answered in a batch); "
            if kind == (QuestionType.ROW_BATCH)
            else "box picked on the stored picture; "
        )
        return {**question, "answer": pick}, note, None

    if kind == QuestionType.WALLS:
        if "left" not in entry or "right" not in entry:
            raise ConfirmationRefused(
                f"{cid}: say what the vendor drew at each end (left and right: one of {DRAWN})"
            )
        drawn = {side: entry[side] for side in ("left", "right")}
        for side, said in drawn.items():
            if said not in DRAWN:
                raise ConfirmationRefused(
                    f"{cid}: {side} drawn must be one of {DRAWN}, not {said!r}"
                )
        layout = _layout(candidate)
        left_site, right_site = site_ends(layout)
        return (
            {
                **question,
                "left": {"drawn": drawn["left"], "site": left_site},
                "right": {"drawn": drawn["right"], "site": right_site},
                "site_layout": layout,
            },
            "drawn ends stated from the picture; site from the reviewer's layout; ",
            None,
        )

    assert kind == QuestionType.PIECE_KIND and candidate.answer is not None
    widths = row_widths.get(cid)
    if widths is None:
        raise ConfirmationRefused(
            f"{cid}: a piece-kind case needs its row_widths from the stored row, so Pariksha can "
            "tell a run read the keyed row; give the database (--db) the candidates came from"
        )
    return {**question, "row_widths": list(widths), "answer": dict(candidate.answer)}, "", None


# --- the keys --------------------------------------------------------------------------------------


def _loader(agent: str) -> Callable[[Mapping[str, object]], Key]:
    def labels_or_rows(data: Mapping[str, object]) -> Key:
        key = load_key(data)
        assert isinstance(key, AnswerKey)
        return key

    if agent == WALLS_AGENT:
        return load_walls_key
    if agent == KINDS_AGENT:
        return load_kinds_key
    return labels_or_rows


def _dump(key: Key | ArchitectAnswerKey) -> dict[str, object]:
    if isinstance(key, WallsKey):
        return dump_walls_key(key)
    if isinstance(key, KindsKey):
        return dump_kinds_key(key)
    return dump_key(key)


def _agent_of(key: object) -> str | None:
    if isinstance(key, WallsKey):
        return WALLS_AGENT
    if isinstance(key, KindsKey):
        return KINDS_AGENT
    if isinstance(key, AnswerKey):
        return key.agent
    return None


def _identity(case: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(case["family"]), str(case["page_sha256"]), str(case["question_id"]))


def _folded(agent: str, case: Mapping[str, Any]) -> object:
    """The case as its loader reads it, without its source: two spellings of one answer are one."""
    load = _loader(agent)
    document = _dump(
        load(
            {
                "format": KEY_FORMAT,
                "agent": agent,
                "name": "-",
                "version": "-",
                "cases": [{name: value for name, value in case.items() if name != "source"}],
            }
        )
    )
    cases = document["cases"]
    assert isinstance(cases, list)
    return cases[0]


def _same(agent: str, first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    try:
        return _folded(agent, first) == _folded(agent, second)
    except KeyFormatError:
        return False


def confirm_candidates(
    export: CandidateExport,
    confirmations: Mapping[str, Any],
    *,
    candidates_sha256: str,
    row_widths: Mapping[str, Sequence[str | None]] | None = None,
    bases: Sequence[object] = (),
    name: str = "smriti",
    version: str = "1",
) -> Confirmed:
    """The keys made of exactly the candidates the person answered Yes (plus the base keys' cases).

    `candidates_sha256` is the candidates file's own hash: answers given for another file are
    refused. `row_widths` (from `stored_row_widths`) fills the piece-kind cases."""
    if not isinstance(confirmations, Mapping):
        raise ConfirmationRefused("the confirmations are one JSON object")
    if confirmations.get("format") != CONFIRMATIONS_FORMAT:
        raise ConfirmationRefused(
            f"format must be {CONFIRMATIONS_FORMAT!r}, not {confirmations.get('format')!r}"
        )
    _unknown(confirmations, _HEADER, "the confirmations")
    confirmed_by = _text(confirmations.get("confirmed_by"), "confirmed_by").strip()
    if confirmations.get("candidates_sha256") != candidates_sha256:
        raise ConfirmationRefused(
            "these answers were given for another candidates file (candidates_sha256 "
            f"{str(confirmations.get('candidates_sha256'))[:12]}, this file "
            f"{candidates_sha256[:12]}); build the review page from this file and answer again"
        )
    entries = confirmations.get("answers")
    if not isinstance(entries, list) or not entries:
        raise ConfirmationRefused(
            "nothing is answered: each case a person looked at is listed under answers; nothing "
            "joins a key without that"
        )
    widths = row_widths or {}

    by_id = {candidate.candidate_id: candidate for candidate in export.candidates}
    seen: set[str] = set()
    cases: list[_Case] = []
    joined: list[str] = []
    vendor: list[str] = []
    kept_out: list[tuple[str, str]] = []
    for index, entry in enumerate(entries):
        where = f"answer {index + 1}"
        if not isinstance(entry, Mapping):
            raise ConfirmationRefused(f"{where} is not an object")
        _unknown(entry, _ALL_FIELDS, where)
        candidate_id = _text(entry.get("candidate"), f"{where} candidate")
        where = f"{where} ({candidate_id})"
        if candidate_id in seen:
            raise ConfirmationRefused(f"{candidate_id} is answered twice")
        seen.add(candidate_id)
        verdict = entry.get("verdict")
        if verdict not in VERDICTS:
            raise ConfirmationRefused(
                f"{where}: verdict must be one of {VERDICTS}, not {verdict!r}"
            )
        note = entry.get("note")
        if note is not None and not isinstance(note, str):
            raise ConfirmationRefused(f"{where}: note is text")
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise ConfirmationRefused(
                f"{candidate_id} is not in the candidates of {export.database}"
            )
        why = not_ready(candidate)
        if why is not None:
            raise ConfirmationRefused(f"{candidate_id} cannot be confirmed: {why}")
        kind = question_type(candidate)
        assert kind is not None
        extra = sorted(set(entry) - _COMMON - _FIELDS[kind])
        if extra and verdict == YES:
            raise ConfirmationRefused(
                f"{where}: {', '.join(extra)} is not asked of this question ({kind}); a proposed "
                "answer is never replaced: if it is wrong, answer No"
            )
        if verdict != YES:
            kept_out.append((candidate_id, f"answered {verdict}"))
            continue
        document, said, out = _case(candidate, entry, where, widths)
        if out == "vendor":
            vendor.append(candidate_id)
            continue
        if out is not None:
            kept_out.append((candidate_id, out))
            continue
        assert document is not None
        if isinstance(note, str) and note.strip():
            said = f"{said}note: {note.strip()}; "
        document["source"] = _source(candidate, export, confirmed_by, said)
        cases.append(_Case(agent=KEY_AGENTS[document["family"]], document=document))
        joined.append(candidate_id)

    merged: dict[str, list[dict[str, Any]]] = defaultdict(list)
    known: dict[tuple[str, tuple[str, str, str]], dict[str, Any]] = {}
    base_agents: set[str] = set()
    for base in bases:
        agent = _agent_of(base)
        if agent is None or agent not in KEY_AGENTS.values():
            raise ConfirmationRefused(
                f"a base key must be one of {sorted(KEY_AGENTS.values())}'s keys"
            )
        if agent in base_agents:
            raise ConfirmationRefused(f"two base keys are {agent}'s; give one per agent")
        base_agents.add(agent)
        assert isinstance(base, AnswerKey | WallsKey | KindsKey)
        base_cases = _dump(base)["cases"]
        assert isinstance(base_cases, list)
        for case in base_cases:
            known[(agent, _identity(case))] = case
            merged[agent].append(case)
    for case in cases:
        identity = (case.agent, _identity(case.document))
        existing = known.get(identity)
        if existing is None:
            known[identity] = case.document
            merged[case.agent].append(case.document)
            continue
        if _same(case.agent, existing, case.document):
            continue
        raise ConfirmationRefused(
            f"question {identity[1][2]} on page {identity[1][1][:12]} is already answered "
            f"otherwise ({_without_source(existing)}); a key holds one answer per question, so "
            "a person settles which is right first"
        )

    keys: dict[str, Key] = {}
    documents: dict[str, dict[str, object]] = {}
    for agent in sorted({case.agent for case in cases}):
        try:
            key = _loader(agent)(
                {
                    "format": KEY_FORMAT,
                    "agent": agent,
                    "name": name,
                    "version": version,
                    "cases": merged[agent],
                }
            )
        except KeyFormatError as error:
            raise ConfirmationRefused(
                f"the confirmed {agent} cases do not make a valid key: {error}"
            ) from error
        keys[agent] = key
        documents[agent] = _dump(key)
    return Confirmed(
        keys=keys,
        documents=documents,
        joined=tuple(joined),
        vendor=tuple(vendor),
        kept_out=tuple(kept_out),
    )


def _without_source(case: Mapping[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in case.items() if name not in ("source", "family")}
