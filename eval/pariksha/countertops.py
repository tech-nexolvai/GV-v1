"""The countertop-level scorecard: the honest metric (#1321).

**Why.** Per-question scorecards (labels, rows) say how often a reader is right, but not what the
reviewer gets: a countertop is decided only when its line and all of its numbers are settled. The
honest question is: **of the keyed countertops, how many did the app decide by itself, and was
each such decision right?**

**One line per keyed countertop.** A keyed countertop is a page whose row question a rows key
answers with a line (by geometry or by box number). A page a key answers "no countertop" is listed
too: deciding a countertop there is wrong. Two keys answering one question differently are
refused; the same answer in several keys is one countertop, named by each.

* **Decided automatically**: the page's first `CT-WIDTH-001` finding for the row is PASS or FAIL.
  The first finding of a row candidate is the check that ran when the reading finished, before
  any person answered anything (later findings of the same row may rest on a reviewer's wall
  answer or decision, so they never count). REVIEW_REQUIRED, NOT_FOUND and the rest are "held":
  not decided.
* **The line**: the box the readers agreed on (the finding's row candidate, `row-choice:N`) on the
  picture the page's row question showed, against the keyed line through the box map
  (`eval/pariksha/row_geometry.py`): `right`, `wrong`, or `unknown` when the picture's boxes are
  not proven (never counted right).
* **The numbers**: each verdict input of the finding whose span (`slot-box:` of its supporting
  candidate) a number key (Akshara) answers with exactly one case, compared in inches with the
  key's printed texts (`read_label`). A number no key covers is counted, not judged.
* **Right** = decided automatically, on the keyed line, and no keyed number differs. **Wrong** =
  decided automatically on another line, with a keyed number that differs, or on a page keyed "no
  countertop". Unknown otherwise. A held countertop is neither.

Nothing is written to the run's database: findings are read in a read-only transaction.

Source: issue #1321 · Verification: `tests/eval/test_pariksha_countertops.py`,
`tests/scripts/test_pariksha_countertops.py`
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from eval.pariksha.keys import AnswerKey, LabelTruth, QuestionIdentity, RowTruth
from eval.pariksha.row_geometry import RowBoxes, on_picture

__all__ = [
    "AUTOMATIC_OUTCOMES",
    "WIDTH_RULE",
    "CountertopFinding",
    "CountertopLine",
    "CountertopScorecard",
    "FindingNumber",
    "card_document",
    "load_countertop_findings",
    "render_countertops",
    "score_countertops",
    "stored_row_pages",
]

#: The countertop width rule: its finding is the countertop's decision.
WIDTH_RULE: Final = "CT-WIDTH-001"
#: A decision the app makes by itself; every other outcome waits for a person.
AUTOMATIC_OUTCOMES: Final = frozenset({"PASS", "FAIL"})
_ROW_FAMILY: Final = "slot-row-choice"


@dataclass(frozen=True, slots=True)
class FindingNumber:
    """One number a decision used: its operand, its value in inches, and the span it was read
    from (`None` when not known, or when it is not in inches the value is `None`)."""

    operand: str
    inches: Fraction | None
    slot_box: str | None


@dataclass(frozen=True, slots=True)
class CountertopFinding:
    """A row's first width finding (before any person acted), as the scorecard needs it."""

    page_sha256: str
    label: str
    outcome: str
    reason: str
    row_choice: int | None
    """The box both readers agreed on (`row-choice:N`), `None` when the row carries none."""
    pictures: tuple[str, ...]
    """The numbered picture(s) the page's row question showed."""
    numbers: tuple[FindingNumber, ...] = ()


@dataclass(frozen=True, slots=True)
class CountertopLine:
    page_sha256: str
    question_id: str
    keys: tuple[str, ...]
    expected: str
    """The key's answer: the line, or "no_countertop"."""
    is_countertop: bool
    automatic: bool
    outcome: str
    """The first finding's outcome(s), or "none"."""
    line: str
    """`right`, `wrong`, `unknown`, or `-` when nothing was decided."""
    numbers_total: int
    numbers_keyed: int
    numbers_wrong: int
    right: bool | None
    """`True` right, `False` wrong, `None` held or not knowable."""
    why: str


@dataclass(frozen=True, slots=True)
class CountertopScorecard:
    lines: tuple[CountertopLine, ...]
    totals: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _Keyed:
    question: QuestionIdentity
    truth: RowTruth
    picture_sha256: str | None
    tolerance: object
    keys: tuple[str, ...]


def _keyed(rows_keys: Sequence[AnswerKey]) -> list[_Keyed]:
    found: dict[QuestionIdentity, _Keyed] = {}
    for key in rows_keys:
        if key.agent != "rekha":
            raise ValueError(f"{key.name} is a {key.agent} key, not a rows key")
        for case in key.cases:
            truth = case.truth
            assert isinstance(truth, RowTruth)
            known = found.get(case.question)
            if known is None:
                found[case.question] = _Keyed(
                    case.question, truth, case.picture_sha256, key.row_tolerance_pt, (key.name,)
                )
                continue
            if (known.truth, known.picture_sha256) != (truth, case.picture_sha256):
                raise ValueError(
                    f"{case.question}: {known.keys[0]} and {key.name} give it different answers "
                    f"({known.truth} and {truth}); a person settles which is right first"
                )
            found[case.question] = _Keyed(
                known.question,
                known.truth,
                known.picture_sha256,
                known.tolerance,
                tuple(sorted({*known.keys, key.name})),
            )
    return sorted(found.values(), key=lambda k: (k.question.page_sha256, k.question.question_id))


def _label_index(label_keys: Sequence[AnswerKey]) -> dict[tuple[str, str], list[LabelTruth]]:
    index: dict[tuple[str, str], list[LabelTruth]] = defaultdict(list)
    for key in label_keys:
        if key.agent != "akshara":
            raise ValueError(f"{key.name} is a {key.agent} key, not a number key")
        for case in key.cases:
            if case.slot_box is not None and isinstance(case.truth, LabelTruth):
                index[(case.question.page_sha256, case.slot_box)].append(case.truth)
    return index


def _key_inches(truth: LabelTruth) -> set[Fraction]:
    from extraction.architect.labels import read_label

    found: set[Fraction] = set()
    for printed in truth.texts:
        inches = read_label(printed).printed_inches
        if inches is not None:
            found.add(Fraction(inches))
    return found


def _numbers(
    finding: CountertopFinding, index: Mapping[tuple[str, str], list[LabelTruth]]
) -> tuple[int, int, list[str]]:
    keyed = wrong = 0
    notes: list[str] = []
    for number in finding.numbers:
        if number.slot_box is None or number.inches is None:
            continue
        truths = index.get((finding.page_sha256, number.slot_box), [])
        if len(truths) != 1 or truths[0].no_label:
            continue
        accepted = _key_inches(truths[0])
        if not accepted:
            continue
        keyed += 1
        if number.inches not in accepted:
            wrong += 1
            notes.append(f"{number.operand} is {number.inches} in, the key says {truths[0]}")
    return keyed, wrong, notes


def _line_on(
    keyed: _Keyed, finding: CountertopFinding, row_boxes: RowBoxes | None
) -> tuple[str, str]:
    """`right`/`wrong`/`unknown` for the line the finding rests on, and why."""
    truth = keyed.truth
    if finding.row_choice is None:
        return "unknown", "the finding's row names no agreed box"
    if truth.at is None:
        if truth.kind == "not_among_boxes":
            return "wrong", "the key says the countertop's line has no box, but a box was decided"
        if keyed.picture_sha256 not in finding.pictures:
            return "unknown", "the key's box number belongs to another picture"
        same = finding.row_choice == truth.box
        return ("right" if same else "wrong"), f"box {finding.row_choice}, the key's is {truth}"
    page = finding.page_sha256
    shown = [] if row_boxes is None else [p for p in finding.pictures if row_boxes.get(p, page)]
    if len(shown) != 1 or row_boxes is None:
        return "unknown", "no box geometry for the picture the row question showed"
    picture = row_boxes.get(shown[0], page)
    assert picture is not None
    here = on_picture(truth.at, picture, keyed.tolerance)  # type: ignore[arg-type]
    if isinstance(here, str):
        return "unknown", here
    if here.kind != "row":
        return "wrong", f"box {finding.row_choice} was decided; the keyed line has no box here"
    if here.box == finding.row_choice:
        return "right", f"box {finding.row_choice} is {truth.at}"
    return "wrong", f"box {finding.row_choice} was decided; {truth.at} is box {here.box} here"


def _score_one(
    keyed: _Keyed,
    findings: Sequence[CountertopFinding],
    row_boxes: RowBoxes | None,
    index: Mapping[tuple[str, str], list[LabelTruth]],
) -> CountertopLine:
    is_countertop = keyed.truth.kind != "no_countertop"
    automatic = [f for f in findings if f.outcome in AUTOMATIC_OUTCOMES]
    outcome = ", ".join(sorted({f.outcome for f in findings})) or "none"
    common = {
        "page_sha256": keyed.question.page_sha256,
        "question_id": keyed.question.question_id,
        "keys": keyed.keys,
        "expected": str(keyed.truth),
        "is_countertop": is_countertop,
        "automatic": bool(automatic),
        "outcome": outcome,
        "numbers_total": sum(len(f.numbers) for f in automatic),
    }
    if not automatic:
        why = (
            "no countertop finding on this page"
            if not findings
            else f"held: {outcome} ({'; '.join(f.reason for f in findings if f.reason)[:200]})"
        )
        return CountertopLine(
            **common,  # type: ignore[arg-type]
            line="-",
            numbers_keyed=0,
            numbers_wrong=0,
            right=None,
            why=why,
        )
    if not is_countertop:
        return CountertopLine(
            **common,  # type: ignore[arg-type]
            line="wrong",
            numbers_keyed=0,
            numbers_wrong=0,
            right=False,
            why=f"a countertop was decided ({outcome}) on a page keyed 'no countertop'",
        )
    lines = [_line_on(keyed, finding, row_boxes) for finding in automatic]
    counted = [_numbers(finding, index) for finding in automatic]
    keyed_numbers = sum(count[0] for count in counted)
    wrong_numbers = sum(count[1] for count in counted)
    notes = [why for _state, why in lines] + [note for count in counted for note in count[2]]
    states = {state for state, _why in lines}
    if "wrong" in states or wrong_numbers:
        right: bool | None = False
        line = "wrong" if "wrong" in states else ("unknown" if "unknown" in states else "right")
    elif states == {"right"}:
        right, line = True, "right"
    else:
        right, line = None, "unknown"
    return CountertopLine(
        **common,  # type: ignore[arg-type]
        line=line,
        numbers_keyed=keyed_numbers,
        numbers_wrong=wrong_numbers,
        right=right,
        why="; ".join(notes),
    )


def score_countertops(
    rows_keys: Sequence[AnswerKey],
    findings: Sequence[CountertopFinding],
    *,
    row_boxes: RowBoxes | None = None,
    label_keys: Sequence[AnswerKey] = (),
    read_pages: Collection[str] | None = None,
) -> CountertopScorecard:
    """One line per keyed countertop (and per page keyed "no countertop"), with the totals.

    `read_pages`, when given, are the pages the run read (`stored_row_pages`, and the findings'):
    a keyed page the run never read is another drawing set's, so it is counted apart
    (`keyed_not_in_run`), never as a countertop the app failed to decide."""
    by_page: dict[str, list[CountertopFinding]] = defaultdict(list)
    for finding in findings:
        by_page[finding.page_sha256].append(finding)
    index = _label_index(label_keys)
    keyed_all = _keyed(rows_keys)
    if read_pages is not None:
        read = {*read_pages, *by_page}
        in_run = [keyed for keyed in keyed_all if keyed.question.page_sha256 in read]
    else:
        in_run = keyed_all
    lines = tuple(
        _score_one(keyed, by_page.get(keyed.question.page_sha256, []), row_boxes, index)
        for keyed in in_run
    )
    countertops = [line for line in lines if line.is_countertop]
    automatic = [line for line in countertops if line.automatic]
    others = [line for line in lines if not line.is_countertop]
    totals = {
        "keyed_countertops": len(countertops),
        "automatic": len(automatic),
        "automatic_right": sum(1 for line in automatic if line.right is True),
        "automatic_wrong": sum(1 for line in automatic if line.right is False),
        "automatic_unknown": sum(1 for line in automatic if line.right is None),
        "keyed_no_countertop": len(others),
        "no_countertop_decided": sum(1 for line in others if line.automatic),
        "keyed_not_in_run": len(keyed_all) - len(in_run),
    }
    return CountertopScorecard(lines=lines, totals=totals)


def render_countertops(card: CountertopScorecard, names: Mapping[str, str] | None = None) -> str:
    """The scorecard in plain words: the totals first, then one line per countertop."""
    totals = card.totals
    text_lines = [
        f"keyed countertops: {totals['keyed_countertops']}",
        (
            f"  decided automatically: {totals['automatic']} of {totals['keyed_countertops']}"
            f"  (right: {totals['automatic_right']}, wrong: {totals['automatic_wrong']}, "
            f"cannot tell: {totals['automatic_unknown']})"
        ),
        (
            f"pages keyed 'no countertop': {totals['keyed_no_countertop']}"
            f"  (a countertop decided there anyway: {totals['no_countertop_decided']})"
        ),
        f"keyed pages this run did not read (another set's): {totals['keyed_not_in_run']}",
        "",
    ]

    def where_of(line: CountertopLine) -> str:
        return (names or {}).get(line.page_sha256, line.page_sha256[:12])

    def natural(line: CountertopLine) -> tuple[object, ...]:
        return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", where_of(line)))

    for line in sorted(card.lines, key=natural):
        where = where_of(line)
        verdict = {True: "RIGHT", False: "WRONG", None: "-"}[line.right]
        numbers = (
            f", numbers keyed {line.numbers_keyed} of {line.numbers_total}"
            if line.automatic and line.is_countertop
            else ""
        )
        text_lines.append(
            f"  {where}: {'automatic' if line.automatic else 'not automatic'} ({line.outcome}), "
            f"line {line.line}{numbers}: {verdict}  [{', '.join(line.keys)}]"
        )
        if line.why:
            text_lines.append(f"      {line.why}")
    return "\n".join(text_lines)


def card_document(card: CountertopScorecard) -> dict[str, object]:
    return {
        "format": "pariksha-countertops/1",
        "totals": dict(card.totals),
        "lines": [
            {
                "page_sha256": line.page_sha256,
                "question_id": line.question_id,
                "keys": list(line.keys),
                "expected": line.expected,
                "is_countertop": line.is_countertop,
                "automatic": line.automatic,
                "outcome": line.outcome,
                "line": line.line,
                "numbers_total": line.numbers_total,
                "numbers_keyed": line.numbers_keyed,
                "numbers_wrong": line.numbers_wrong,
                "right": line.right,
                "why": line.why,
            }
            for line in card.lines
        ],
    }


# --- reading a stored run's findings ---------------------------------------------------------


def _flag(flags: object, prefix: str) -> str | None:
    if not isinstance(flags, list):
        return None
    for flag in flags:
        if isinstance(flag, str) and flag.startswith(prefix):
            return flag.removeprefix(prefix)
    return None


def _row_pictures(session: Session) -> dict[str, tuple[str, ...]]:
    """Each page's numbered row pictures, from the stored row questions."""
    from app.models.runs import ModelInvocation
    from eval.pariksha.stored import prompt_family

    pictures: dict[str, set[str]] = defaultdict(set)
    for prompt_id, packet in session.execute(
        select(ModelInvocation.prompt_id, ModelInvocation.reader_question_packet).where(
            ModelInvocation.reader_question_packet.is_not(None),
            ModelInvocation.prompt_id.startswith(f"{_ROW_FAMILY}-v"),
        )
    ):
        if prompt_family(prompt_id) != _ROW_FAMILY or not isinstance(packet, Mapping):
            continue
        page = packet.get("source_page_sha256")
        images = packet.get("images")
        if not isinstance(page, str) or not isinstance(images, Mapping):
            continue
        for image in images.values():
            if isinstance(image, Mapping) and isinstance(image.get("sha256"), str):
                pictures[page].add(str(image["sha256"]))
    return {page: tuple(sorted(found)) for page, found in pictures.items()}


def stored_row_pages(session: Session) -> frozenset[str]:
    """The pages the database's runs asked a row question about: the pages they read for a row."""
    return frozenset(_row_pictures(session))


def _finding_numbers(session: Session, check_run_id: UUID) -> tuple[FindingNumber, ...]:
    from app.models.evidence import EvidenceSupportingCandidate, ObservationCandidate
    from app.models.verdicts import VerdictInput

    numbers: list[FindingNumber] = []
    for operand, numerator, denominator, unit, observation in session.execute(
        select(
            VerdictInput.operand_name,
            VerdictInput.value_numerator,
            VerdictInput.value_denominator,
            VerdictInput.unit,
            VerdictInput.canonical_observation_id,
        )
        .where(VerdictInput.check_run_id == check_run_id)
        .order_by(VerdictInput.operand_name)
    ):
        boxes: set[str] = set()
        if observation is not None:
            for flags in session.scalars(
                select(ObservationCandidate.ambiguity_flags)
                .join(
                    EvidenceSupportingCandidate,
                    EvidenceSupportingCandidate.candidate_id == ObservationCandidate.id,
                )
                .where(EvidenceSupportingCandidate.canonical_observation_id == observation)
            ):
                box = _flag(flags, "slot-box:")
                if box is not None:
                    boxes.add(box)
        numbers.append(
            FindingNumber(
                operand=operand,
                inches=Fraction(numerator, denominator) if unit == "in" else None,
                slot_box=next(iter(boxes)) if len(boxes) == 1 else None,
            )
        )
    return tuple(numbers)


def load_countertop_findings(
    session: Session, *, package_revision_id: UUID | None = None
) -> tuple[CountertopFinding, ...]:
    """Each countertop row's first width finding in one package revision, read-only."""
    from app.models.document import Page
    from app.models.evidence import ObservationCandidate
    from app.models.rules import RuleDefinition, RuleSnapshot
    from app.models.verdicts import CheckRun, Finding

    session.execute(text("SET TRANSACTION READ ONLY"))
    rows = session.execute(
        select(
            Finding.package_revision_id,
            Finding.scope_row_candidate_id,
            Finding.outcome,
            Finding.reason,
            Finding.scope_label,
            Finding.check_run_id,
            ObservationCandidate.ambiguity_flags,
            Page.content_hash,
        )
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .join(ObservationCandidate, ObservationCandidate.id == Finding.scope_row_candidate_id)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .where(RuleDefinition.rule_id == WIDTH_RULE)
        .order_by(Finding.created_at, Finding.id)
    ).all()
    revisions = sorted({row.package_revision_id for row in rows}, key=str)
    if package_revision_id is not None:
        rows = [row for row in rows if row.package_revision_id == package_revision_id]
    elif len(revisions) > 1:
        raise ValueError(
            f"{len(revisions)} package revisions hold width findings "
            f"({', '.join(str(r) for r in revisions)}); name the one to score"
        )
    first: dict[UUID | None, Any] = {}
    for row in rows:
        first.setdefault(row.scope_row_candidate_id, row)
    pictures = _row_pictures(session)
    findings: list[CountertopFinding] = []
    for row in first.values():
        choice = _flag(row.ambiguity_flags, "row-choice:")
        findings.append(
            CountertopFinding(
                page_sha256=row.content_hash,
                label=row.scope_label or "",
                outcome=row.outcome,
                reason=row.reason or "",
                row_choice=int(choice) if choice and choice.isdigit() and int(choice) else None,
                pictures=pictures.get(row.content_hash, ()),
                numbers=_finding_numbers(session, row.check_run_id),
            )
        )
    return tuple(findings)
