"""Pariksha's walls scorecard (Rekha, #1250): the two AI wall answers and code's wall ink, per row end.

**Why.** Every countertop row is asked "is there a wall at each end?" (`slot-walls-v3`), and code
looks for wall ink at the two ends itself (`extraction/slot_reader/walls.py`). The walls decide the
field cut, so a change to the question's wording, its models or the ink detectors must be measured
before it ships, and accepted only when it is **better with 0 new wrong**, like every Pariksha
scorecard (`eval/pariksha/__init__.py`). This scores a run that already happened, at $0.

**The key (`pariksha-key/1`, agent `rekha-walls`).** Local only, like every key: it holds what
client drawings show. One case per row (the wall question is asked once per row), each end with
two truths:

    {"format": "pariksha-key/1", "agent": "rekha-walls", "name": "...", "version": "...",
     "cases": [{"family": "slot-walls", "page_sha256": "<64 hex>", "question_id": "p4:walls",
                "row_box": "x0,y0,x1,y1",
                "left":  {"drawn": "wall", "site": "wall"},
                "right": {"drawn": "wall", "site": "open", "note": "..."},
                "site_layout": "back_and_left", "source": "..."}]}

* **`drawn`** (gating): what the vendor drew at the end the stone meets: `wall`, `open`,
  `not_shown` (the drawing does not show it: a confident yes or no is then wrong), or `key_unsure`
  (not settled yet: that end is listed and not scored; it is never guessed). This is the readers'
  truth: they read the vendor's drawing.
* **`site`** (reported, never gating): GV's answer for the site, from its markup. It is the answer
  key, never a reading source (decided 2026-09-09). Scoring against both separates "the vendor drew
  it wrong" from a reader's error: a reader that reports a wall the vendor drew is right on `drawn`
  and wrong on `site`.
* **`row_box`**: the row picture's box in pixels, as the run records it (the wall candidate's
  polygon). A run whose row is another one (any edge more than `ROW_SLACK_PX` away) is "not asked:
  another row", never scored against this row's truth.
* **`site_layout`** (optional): the layout the check should get (`back_left_right`, ...); each
  row's stored layout is scored against it.

**What is scored, per row end.**

* **The pair** (the case's outcome): agreed-right or **agreed-wrong** when both readers say the same
  yes or no (the agreement `seal_walls` builds on); **disagreed** when one says yes and the other
  no (a contradiction, counted on its own); **one-unsure** when one says yes or no and the other
  unsure; **both-unsure**; **no-answer** when a reader has no accepted answer.
* **Each reader**: right, wrong, unsure or invalid (no accepted answer).
* **Code's wall ink**, separately, as its own judgment (`code-wall-ink` among the case's readers):
  ink is positive only, so ink seen is right at a drawn wall and **wrong** anywhere else; no ink says
  nothing (`none`); a run stored before the ink check has none recorded (`not_recorded`).
* Each against `drawn` (the plain metric names) and against `site` (the same names with `@site`).

**Per row**: the stored layout against `site_layout`: **automatic** right or wrong (code's ink at
both ends and both AIs, `wall-source:code-ink-and-both-ais`, the one layout a check uses without a
click), the readers' **sealed** proposal right or wrong, or held for the person. And the cost of the
question, from `model_invocations.cost_micros`. Stored data this cannot read (a row id that is no id,
a row box or a wall-ink flag that does not parse) is counted in `skipped_malformed`, never dropped
silently.

**Compare.** `compare_walls` is the Pariksha compare (`eval/pariksha/compare.py`), plus two more
reasons to reject: an end whose wall ink became wrong, and a row whose automatic layout became
wrong. Its metric directions are the walls' own (`walls_direction`). It compares two walls cards
only: a card of another agent (or two different agents) is refused, and the other compares refuse a
walls card, because they do not know wall ink or layouts and would accept a change this rejects.

A row whose **sealed** layout became wrong does **not** reject on its own. A sealed layout is the
readers' proposal, never automatic: no check uses it without a person's click, and nothing is
pre-selected (decided 2026-10-13 (c)). It is sealed only when both readers agree at both ends
(`seal_walls`), so a newly wrong one comes from ends that agree with what the vendor drew while the
site differs (a wall GV removes, which no reading of the drawing can know), from an end that is
agreed-wrong on what the vendor drew (which rejects when it is new), from an end the key has not
settled, or from the back wall, which this card does not key. So the bar stays "0 new agreed-wrong
on what the vendor drew"; `sealed_wrong` is shown (fewer is better) for a person to weigh.

**Reading stored answers.** Each accepted reply is read again by the reader's own code
(`replay_stored_answer`, the path a re-run takes when it reuses an answer); every wall prompt
version has one answer shape. A reply accepted when stored that cannot be read again refuses the
scorecard. The row (box, ink, layout, source) is the wall candidate the question's packet names.

Source: issue #1250 · Verification: `tests/eval/test_pariksha_walls.py`
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import ObservationCandidate
from app.models.runs import REUSED_FROM_KEY, ModelInvocation
from eval.metrics import MetricResult
from eval.pariksha.compare import CaseChange, Comparison, compare_scorecards
from eval.pariksha.keys import KEY_FORMAT, KeyFormatError, QuestionIdentity

# The helpers every Pariksha scorecard shares, so a walls scorecard rates, dates and refuses an
# unattributable run exactly as the label and row scorecards do.
from eval.pariksha.score import (
    AGREED_RIGHT,
    AGREED_WRONG,
    NOT_ASKED,
    CaseScore,
    ReaderScore,
    Scorecard,
    UnreadableStoredAnswers,
    _code_note,
    _now,
    _provenance,
    _rate,
)
from eval.pariksha.stored import (
    StoredAttempt,
    StoredRunError,
    _choose_run,
    _read_only,
    _run_row,
    prompt_family,
)
from eval.regression import IncomparableRuns
from eval.release_metrics import Direction
from extraction.slot_reader.bedrock import CropJob, replay_stored_answer
from extraction.slot_reader.walls import Side, WallAnswer
from vocabulary.wall_layouts import AUTOMATIC_WALL_SOURCE, WALL_LAYOUT_WORDS
from workflow.layout_proposals import WALL_INK_FLAG, WALL_READER_FLAG, WALLS_SEALED_FLAG

__all__ = [
    "AUTOMATIC_RIGHT",
    "AUTOMATIC_WRONG",
    "BOTH_UNSURE",
    "DISAGREED",
    "DRAWN",
    "HELD_LAYOUT",
    "INK_READER",
    "KEY_UNSURE",
    "NO_ANSWER",
    "ONE_UNSURE",
    "ROW_SLACK_PX",
    "SEALED_RIGHT",
    "SEALED_WRONG",
    "SITE",
    "WALLS_AGENT",
    "WALLS_FAMILY",
    "EndTruth",
    "StoredWallQuestion",
    "StoredWallRow",
    "StoredWallsRun",
    "WallRowCase",
    "WallsKey",
    "compare_translated",
    "compare_walls",
    "dump_walls_key",
    "load_stored_walls",
    "load_walls_key",
    "read_walls_key",
    "require_agent",
    "score_walls",
    "walls_direction",
]

WALLS_AGENT: Final = "rekha-walls"
WALLS_FAMILY: Final = "slot-walls"
KEY_UNSURE: Final = "key_unsure"
BOTH_UNSURE: Final = "both_unsure"
#: One reader says yes and the other no: a contradiction, never lumped with an unsure.
DISAGREED: Final = "disagreed"
#: One reader says yes or no, the other unsure.
ONE_UNSURE: Final = "one_unsure"
#: A reader has no accepted answer for the row.
NO_ANSWER: Final = "no_answer"
#: What the vendor drew at an end (gating), and what the site has there (reported).
DRAWN: Final = ("wall", "open", "not_shown", KEY_UNSURE)
SITE: Final = ("wall", "open", KEY_UNSURE)
#: Code's wall-ink judgment, listed among each end's readers.
INK_READER: Final = "code-wall-ink"
#: A row's stored layout, scored against the key's site layout.
AUTOMATIC_RIGHT: Final = "automatic_right"
AUTOMATIC_WRONG: Final = "automatic_wrong"
SEALED_RIGHT: Final = "sealed_right"
SEALED_WRONG: Final = "sealed_wrong"
HELD_LAYOUT: Final = "held"
#: How far (pixels, in the 300 dpi picture) a run's row-box edge may lie from the key's and still be
#: the same row: renderer jitter, far less than the distance between two rows.
ROW_SLACK_PX: Final = 12

RIGHT: Final = "right"
WRONG: Final = "wrong"
UNSURE: Final = "unsure"
INVALID: Final = "invalid"
INK_NONE: Final = "none"
INK_NOT_RECORDED: Final = "not_recorded"
_READER_VERDICTS: Final = (RIGHT, WRONG, UNSURE, INVALID)
_INK_VERDICTS: Final = (RIGHT, WRONG, INK_NONE, INK_NOT_RECORDED)
_PAIR_OUTCOMES: Final = (AGREED_RIGHT, AGREED_WRONG, DISAGREED, ONE_UNSURE, BOTH_UNSURE, NO_ANSWER)
_SIDES: Final = ("left", "right")
_SAYS: Final[Mapping[Side, str]] = {Side.YES: "wall", Side.NO: "open"}

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_BOX: Final = re.compile(r"^(\d+),(\d+),(\d+),(\d+)$")
_QUESTION: Final = re.compile(r"^p\d+:walls$")
_HEADER: Final = frozenset({"format", "agent", "name", "version", "cases"})
_CASE: Final = frozenset(
    {"family", "page_sha256", "question_id", "row_box", "left", "right", "site_layout", "source"}
)
_END: Final = frozenset({"drawn", "site", "note"})
_PICTURE: Final = b"\x89PNG\r\n\x1a\n"
_REPLAY_MARK: Final = "pariksha-walls"
_SOURCE_FLAG: Final = "wall-source:"

Box = tuple[int, int, int, int]


# --- the key ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndTruth:
    drawn: str
    site: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class WallRowCase:
    question: QuestionIdentity
    row_box: Box
    left: EndTruth
    right: EndTruth
    site_layout: str | None = None
    source: str = ""

    @property
    def case_id(self) -> str:
        return self.question.case_id

    def end(self, side: str) -> EndTruth:
        return self.left if side == "left" else self.right


@dataclass(frozen=True, slots=True)
class WallsKey:
    name: str
    version: str
    cases: tuple[WallRowCase, ...]
    sha256: str


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KeyFormatError(f"{field} must be a non-empty string, not {value!r}")
    return value


def _unknown(data: Mapping[str, object], known: frozenset[str], where: str) -> None:
    extra = sorted(set(data) - known)
    if extra:
        raise KeyFormatError(f"unknown field(s) in {where}: {', '.join(extra)}")


def _end(data: object, where: str) -> EndTruth:
    if not isinstance(data, Mapping):
        raise KeyFormatError(f"{where} must be an object with drawn and site")
    _unknown(data, _END, where)
    drawn, site = data.get("drawn"), data.get("site")
    if drawn not in DRAWN:
        raise KeyFormatError(f"{where}: drawn must be one of {DRAWN}, not {drawn!r}")
    if site not in SITE:
        raise KeyFormatError(f"{where}: site must be one of {SITE}, not {site!r}")
    note = data.get("note", "")
    if not isinstance(note, str):
        raise KeyFormatError(f"{where}: note must be text")
    return EndTruth(drawn=str(drawn), site=str(site), note=note)


def parse_box(value: object, where: str) -> Box:
    found = _BOX.match(value) if isinstance(value, str) else None
    if found is None:
        raise KeyFormatError(f"{where}: row_box is 'x0,y0,x1,y1' in pixels, not {value!r}")
    x0, y0, x1, y1 = (int(part) for part in found.groups())
    if not (x0 < x1 and y0 < y1):
        raise KeyFormatError(f"{where}: row_box {value!r} has no area")
    return (x0, y0, x1, y1)


def _case(data: object, index: int) -> WallRowCase:
    where = f"case {index + 1}"
    if not isinstance(data, Mapping):
        raise KeyFormatError(f"{where} is not an object")
    _unknown(data, _CASE, where)
    if data.get("family") != WALLS_FAMILY:
        raise KeyFormatError(
            f"{where}: family must be {WALLS_FAMILY!r}, not {data.get('family')!r}"
        )
    page = data.get("page_sha256")
    if not isinstance(page, str) or not _SHA256.match(page):
        raise KeyFormatError(f"{where}: page_sha256 must be 64 lower-case hex characters")
    question_id = _text(data.get("question_id"), f"{where} question_id")
    if not _QUESTION.match(question_id):
        raise KeyFormatError(f"{where}: question_id is a row's walls question (p<page>:walls)")
    layout = data.get("site_layout")
    if layout is not None and layout not in WALL_LAYOUT_WORDS:
        raise KeyFormatError(
            f"{where}: site_layout must be one of {sorted(WALL_LAYOUT_WORDS)}, not {layout!r}"
        )
    source = data.get("source", "")
    if not isinstance(source, str):
        raise KeyFormatError(f"{where}: source must be text")
    return WallRowCase(
        question=QuestionIdentity(family=WALLS_FAMILY, page_sha256=page, question_id=question_id),
        row_box=parse_box(data.get("row_box"), where),
        left=_end(data.get("left"), f"{where} left"),
        right=_end(data.get("right"), f"{where} right"),
        site_layout=None if layout is None else str(layout),
        source=source,
    )


def canonical_hash(document: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def load_walls_key(data: Mapping[str, object]) -> WallsKey:
    """Read a walls key from its parsed JSON, refusing anything it does not know."""
    if not isinstance(data, Mapping):
        raise KeyFormatError("a key is one JSON object")
    if data.get("format") != KEY_FORMAT:
        raise KeyFormatError(f"format must be {KEY_FORMAT!r}, not {data.get('format')!r}")
    if data.get("agent") != WALLS_AGENT:
        raise KeyFormatError(f"agent must be {WALLS_AGENT!r}, not {data.get('agent')!r}")
    _unknown(data, _HEADER, "the key")
    name = _text(data.get("name"), "name")
    version = _text(data.get("version"), "version")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise KeyFormatError("cases must be a non-empty list")
    cases = tuple(_case(entry, index) for index, entry in enumerate(raw_cases))
    seen: set[QuestionIdentity] = set()
    for case in cases:
        if case.question in seen:
            raise KeyFormatError(f"the key names question {case.question} twice")
        seen.add(case.question)
    key = WallsKey(name=name, version=version, cases=cases, sha256="")
    return replace(key, sha256=canonical_hash(dump_walls_key(key)))


def read_walls_key(path: Path) -> WallsKey:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise KeyFormatError(f"{path.name} is not JSON: {error}") from error
    return load_walls_key(data)


def _end_document(end: EndTruth) -> dict[str, str]:
    document = {"drawn": end.drawn, "site": end.site}
    if end.note:
        document["note"] = end.note
    return document


def dump_walls_key(key: WallsKey) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for case in key.cases:
        document: dict[str, object] = {
            "family": WALLS_FAMILY,
            "page_sha256": case.question.page_sha256,
            "question_id": case.question.question_id,
            "row_box": ",".join(str(value) for value in case.row_box),
            "left": _end_document(case.left),
            "right": _end_document(case.right),
        }
        if case.site_layout is not None:
            document["site_layout"] = case.site_layout
        if case.source:
            document["source"] = case.source
        cases.append(document)
    return {
        "format": KEY_FORMAT,
        "agent": WALLS_AGENT,
        "name": key.name,
        "version": key.version,
        "cases": cases,
    }


# --- the stored run -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredWallRow:
    """The row the wall question was about, as the run stored it (its wall candidate)."""

    candidate_id: str
    row_box: Box | None
    layout: str | None
    """The sealed layout (`walls-sealed:`), or `None` when the walls were held for the person."""
    source: str | None
    """Who set the layout (`wall-source:`): automatic, the readers, or an older run's source."""
    ink: tuple[bool, bool] | None
    """Code's wall ink at (left, right); `None` for a run stored before code judged it (#1202)."""
    malformed: tuple[str, ...] = ()
    """What of the stored row could not be read (its box, its wall-ink flag)."""


@dataclass(frozen=True, slots=True)
class StoredWallQuestion:
    identity: QuestionIdentity
    prompt_id: str
    packet: Mapping[str, object]
    attempts: tuple[StoredAttempt, ...]
    row: StoredWallRow | None
    """`None` when the packet names no stored wall row: the row is unknown, so never matched."""


@dataclass(frozen=True, slots=True)
class StoredWallsRun:
    database: str
    extraction_run_id: str
    extractor: str
    extractor_version: str
    config_hash: str
    questions: tuple[StoredWallQuestion, ...]
    unidentified: int = 0
    malformed: int = 0
    """Stored rows or row ids this could not read: counted, never dropped silently."""


def _flag(flags: Iterable[str], prefix: str) -> str | None:
    return next((flag.removeprefix(prefix) for flag in flags if flag.startswith(prefix)), None)


def _ink(flags: Sequence[str]) -> tuple[tuple[bool, bool] | None, bool]:
    """Code's wall ink, and whether a stored wall-ink flag could not be read."""
    value = _flag(flags, WALL_INK_FLAG)
    if value is None:
        return None, False
    sides = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
    if sides.get("left") not in ("True", "False") or sides.get("right") not in ("True", "False"):
        return None, True
    return (sides["left"] == "True", sides["right"] == "True"), False


def _polygon_box(polygon: object) -> Box | None:
    if not isinstance(polygon, list) or not polygon:
        return None
    try:
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
    except (TypeError, ValueError, IndexError):
        return None
    return (round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys)))


def _wall_rows(session: Session, ids: Iterable[str]) -> tuple[dict[str, StoredWallRow], int]:
    """The stored wall rows the ids name, and how many ids are no id at all."""
    wanted: set[UUID] = set()
    not_ids = 0
    for value in ids:
        try:
            wanted.add(UUID(value))
        except ValueError:
            not_ids += 1
    if not wanted:
        return {}, not_ids
    rows: dict[str, StoredWallRow] = {}
    for candidate_id, polygon, flags in session.execute(
        select(
            ObservationCandidate.id,
            ObservationCandidate.polygon,
            ObservationCandidate.ambiguity_flags,
        ).where(ObservationCandidate.id.in_(wanted))
    ):
        listed = [flag for flag in flags or () if isinstance(flag, str)]
        if WALL_READER_FLAG not in listed:
            continue
        ink, bad_ink = _ink(listed)
        row_box = _polygon_box(polygon)
        malformed = (*(("row box",) if row_box is None else ()), *(("wall-ink flag",) * bad_ink))
        rows[str(candidate_id)] = StoredWallRow(
            candidate_id=str(candidate_id),
            row_box=row_box,
            layout=_flag(listed, WALLS_SEALED_FLAG),
            source=_flag(listed, _SOURCE_FLAG),
            ink=ink,
            malformed=malformed,
        )
    return rows, not_ids


def load_stored_walls(
    session: Session, *, database: str, extraction_run_id: UUID | None = None
) -> StoredWallsRun:
    """Every stored wall question of one run, with every answer and the row it was about."""
    _read_only(session)
    found = session.execute(
        select(
            ModelInvocation.extraction_run_id,
            ModelInvocation.model_id,
            ModelInvocation.prompt_id,
            ModelInvocation.outcome,
            ModelInvocation.private_raw_response,
            ModelInvocation.cost_micros,
            ModelInvocation.reader_question_packet,
        )
        .where(
            ModelInvocation.extraction_run_id.is_not(None),
            ModelInvocation.reader_question_packet.is_not(None),
            ModelInvocation.prompt_id.startswith(f"{WALLS_FAMILY}-v"),
        )
        .order_by(ModelInvocation.created_at, ModelInvocation.id)
    ).all()
    found = [row for row in found if prompt_family(row.prompt_id) == WALLS_FAMILY]
    counts: dict[UUID, int] = defaultdict(int)
    for row in found:
        assert row.extraction_run_id is not None
        counts[row.extraction_run_id] += 1
    run_id = _choose_run(counts, extraction_run_id, f"{WALLS_FAMILY} questions")
    run = _run_row(session, run_id)
    attempts: dict[QuestionIdentity, list[StoredAttempt]] = defaultdict(list)
    packets: dict[QuestionIdentity, tuple[str, Mapping[str, object]]] = {}
    unidentified = 0
    for row in found:
        if row.extraction_run_id != run_id:
            continue
        packet = row.reader_question_packet
        page = packet.get("source_page_sha256") if isinstance(packet, Mapping) else None
        question_id = packet.get("question_id") if isinstance(packet, Mapping) else None
        if not isinstance(page, str) or not isinstance(question_id, str):
            unidentified += 1
            continue
        assert isinstance(packet, Mapping)
        identity = QuestionIdentity(family=WALLS_FAMILY, page_sha256=page, question_id=question_id)
        attempts[identity].append(
            StoredAttempt(
                model_id=row.model_id,
                outcome=row.outcome,
                raw_response=row.private_raw_response,
                cost_micros=row.cost_micros,
                reused=REUSED_FROM_KEY in packet,
            )
        )
        known = packets.setdefault(identity, (row.prompt_id, packet))[1]
        if known.get("packet_sha256") != packet.get("packet_sha256"):
            raise StoredRunError(
                f"run {run_id} asked {identity} with two different question packets: not one "
                "question, so it cannot be scored as one"
            )
    named: dict[QuestionIdentity, str | None] = {}
    for identity, (_prompt, packet) in packets.items():
        candidates = packet.get("candidate_ids")
        named[identity] = (
            str(candidates[0]) if isinstance(candidates, list) and len(candidates) == 1 else None
        )
    rows, not_ids = _wall_rows(session, (value for value in named.values() if value is not None))
    return StoredWallsRun(
        database=database,
        extraction_run_id=str(run_id),
        extractor=run.extractor,
        extractor_version=run.extractor_version,
        config_hash=run.config_hash,
        questions=tuple(
            StoredWallQuestion(
                identity=identity,
                prompt_id=prompt_id,
                packet=packet,
                attempts=tuple(attempts[identity]),
                row=None if named[identity] is None else rows.get(str(named[identity])),
            )
            for identity, (prompt_id, packet) in packets.items()
        ),
        unidentified=unidentified,
        malformed=not_ids + sum(1 for row in rows.values() if row.malformed),
    )


# --- scoring ------------------------------------------------------------------------------------


def _replay(question: StoredWallQuestion, attempt: StoredAttempt) -> WallAnswer | None:
    if attempt.outcome != "ok" or attempt.raw_response is None:
        return None
    page_index = question.packet.get("page_index")
    job = CropJob(
        key=question.identity.question_id,
        model_id=attempt.model_id,
        page_index=page_index if isinstance(page_index, int) else 0,
        png=_PICTURE,
        view_png=_PICTURE,
        wall_question=True,
        question_packet=question.packet,
    )
    replayed = replay_stored_answer(
        job, attempt.raw_response, reused_from=_REPLAY_MARK, max_tokens=1
    )
    answer = None if replayed is None else replayed[0]
    return answer if isinstance(answer, WallAnswer) else None


def _check_run(run: StoredWallsRun) -> tuple[str, str]:
    seen: set[QuestionIdentity] = set()
    for question in run.questions:
        if question.identity in seen:
            raise StoredRunError(
                f"{question.identity} is listed twice in run {run.extraction_run_id}"
            )
        seen.add(question.identity)
        accepted = Counter(a.model_id for a in question.attempts if a.outcome == "ok")
        doubled = sorted(model for model, count in accepted.items() if count > 1)
        if doubled:
            raise StoredRunError(
                f"{question.identity} has more than one accepted answer from "
                f"{', '.join(doubled)} in run {run.extraction_run_id}: which one counts is not "
                "knowable"
            )
    models = sorted({a.model_id for question in run.questions for a in question.attempts})
    if len(models) != 2:
        raise StoredRunError(
            f"the pair needs exactly two readers; run {run.extraction_run_id} has "
            f"{len(models)}: {', '.join(models) or 'none'}"
        )
    return models[0], models[1]


def _same_row(key_box: Box, run_box: Box | None) -> bool:
    return run_box is not None and all(
        abs(mine - theirs) <= ROW_SLACK_PX for mine, theirs in zip(key_box, run_box, strict=True)
    )


def _pair(first: Side | None, second: Side | None, truth: str) -> str:
    if truth == KEY_UNSURE:
        return KEY_UNSURE
    if first is None or second is None:
        return NO_ANSWER
    if first is Side.UNSURE and second is Side.UNSURE:
        return BOTH_UNSURE
    if first is Side.UNSURE or second is Side.UNSURE:
        return ONE_UNSURE
    if first is not second:
        return DISAGREED
    return AGREED_RIGHT if _SAYS[first] == truth else AGREED_WRONG


def _reader(side: Side | None, truth: str) -> str:
    if truth == KEY_UNSURE:
        return KEY_UNSURE
    if side is None:
        return INVALID
    if side is Side.UNSURE:
        return UNSURE
    return RIGHT if _SAYS[side] == truth else WRONG


def _ink_verdict(seen: bool | None, truth: str) -> str:
    if truth == KEY_UNSURE:
        return KEY_UNSURE
    if seen is None:
        return INK_NOT_RECORDED
    if not seen:
        return INK_NONE
    return RIGHT if truth == "wall" else WRONG


def _ink_shown(seen: bool | None) -> str:
    return "not recorded" if seen is None else "wall ink" if seen else "no wall ink"


def _layout(row: StoredWallRow, site_layout: str) -> str:
    if row.layout is None:
        return HELD_LAYOUT
    right = row.layout == site_layout
    if row.source == AUTOMATIC_WALL_SOURCE:
        return AUTOMATIC_RIGHT if right else AUTOMATIC_WRONG
    return SEALED_RIGHT if right else SEALED_WRONG


def _not_asked(case: WallRowCase, note: str) -> list[CaseScore]:
    scores = [
        CaseScore(
            case_id=f"{case.case_id}:{side}",
            question=f"{case.question} {side} end",
            expected=f"drawn {case.end(side).drawn}; site {case.end(side).site}",
            outcome=NOT_ASKED,
            note=f"not asked: {note}",
        )
        for side in _SIDES
    ]
    scores.append(
        CaseScore(
            case_id=f"{case.case_id}:layout",
            question=str(case.question),
            expected=case.site_layout or "(not keyed)",
            outcome=NOT_ASKED,
            note=f"not asked: {note}",
        )
    )
    return scores


def score_walls(
    key: WallsKey,
    run: StoredWallsRun,
    *,
    code_version: str,
    scorer_version: str = "unknown",
    created_at: str | None = None,
) -> Scorecard:
    """The walls scorecard: per row end, the pair, each reader and code's ink; per row, the layout."""
    _provenance(code_version)
    readers = _check_run(run)
    by_identity = {question.identity: question for question in run.questions}
    cases: list[CaseScore] = []
    # (truth: "drawn" or "site") -> counter of outcomes / verdicts
    pair: dict[str, Counter[str]] = defaultdict(Counter)
    per_reader: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    ink: dict[str, Counter[str]] = defaultdict(Counter)
    keyed_ends: Counter[str] = Counter()
    layouts: Counter[str] = Counter()
    rows_asked = ends_asked = key_unsure = cost = attempts = reused = 0
    unreadable: list[str] = []
    for case in key.cases:
        question = by_identity.get(case.question)
        if question is None:
            cases += _not_asked(case, "no wall question about this page in the run")
            continue
        if question.row is None:
            cases += _not_asked(case, "the run stored no row for this wall question")
            continue
        if not _same_row(case.row_box, question.row.row_box):
            cases += _not_asked(
                case,
                f"the run asked about another row (box {question.row.row_box}; the key's is "
                f"{case.row_box})",
            )
            continue
        answers: dict[str, WallAnswer | None] = dict.fromkeys(readers)
        for attempt in question.attempts:
            if attempt.outcome != "ok":
                continue
            answer = _replay(question, attempt)
            if answer is None:
                unreadable.append(str(question.identity))
            answers[attempt.model_id] = answer
        rows_asked += 1
        question_cost = sum(attempt.cost_micros or 0 for attempt in question.attempts)
        cost += question_cost
        attempts += len(question.attempts)
        reused += sum(1 for attempt in question.attempts if attempt.reused)
        stored_ink = question.row.ink
        for index, side in enumerate(_SIDES):
            truth = case.end(side)
            said = {
                model: None if answer is None else getattr(answer, side)
                for model, answer in answers.items()
            }
            first, second = (said[model] for model in readers)
            seen = None if stored_ink is None else stored_ink[index]
            ends_asked += 1
            key_unsure += truth.drawn == KEY_UNSURE
            outcomes: dict[str, str] = {}
            for name, value in (("drawn", truth.drawn), ("site", truth.site)):
                outcomes[name] = _pair(first, second, value)
                if value == KEY_UNSURE:
                    continue
                keyed_ends[name] += 1
                pair[name][outcomes[name]] += 1
                for model in readers:
                    per_reader[(model, name)][_reader(said[model], value)] += 1
                ink[name][_ink_verdict(seen, value)] += 1
            note = f"site: {outcomes['site']}"
            if truth.note:
                note += f"; {truth.note}"
            cases.append(
                CaseScore(
                    case_id=f"{case.case_id}:{side}",
                    question=f"{question.identity} {side} end",
                    expected=f"drawn {truth.drawn}; site {truth.site}",
                    outcome=outcomes["drawn"],
                    readers=(
                        *(
                            ReaderScore(
                                model_id=model,
                                answer="(invalid)" if said[model] is None else str(said[model]),
                                verdict=_reader(said[model], truth.drawn),
                            )
                            for model in readers
                        ),
                        ReaderScore(
                            model_id=INK_READER,
                            answer=_ink_shown(seen),
                            verdict=_ink_verdict(seen, truth.drawn),
                        ),
                    ),
                    note=note,
                )
            )
        if case.site_layout is None:
            outcome, note = "layout_not_keyed", "the key gives no site layout for this row"
        else:
            outcome, note = _layout(question.row, case.site_layout), ""
            layouts[outcome] += 1
        cases.append(
            CaseScore(
                case_id=f"{case.case_id}:layout",
                question=str(question.identity),
                expected=case.site_layout or "(not keyed)",
                outcome=outcome,
                readers=(
                    ReaderScore(
                        model_id="stored row",
                        answer=question.row.layout or "(held for the person)",
                        verdict=outcome,
                    ),
                ),
                cost_micros=question_cost,
                note=(note or f"set by: {question.row.source or 'nobody (held)'}")
                + (
                    f"; could not read the stored {', '.join(question.row.malformed)}"
                    if question.row.malformed
                    else ""
                ),
            )
        )
    if unreadable:
        listed = ", ".join(unreadable[:5]) + (", ..." if len(unreadable) > 5 else "")
        raise UnreadableStoredAnswers(
            f"{len(unreadable)} accepted wall answer(s) of run {run.extraction_run_id} cannot be "
            f"read by today's reader code ({listed}); no scorecard is written"
        )
    metrics: dict[str, MetricResult] = {
        "asked": _rate("asked", ends_asked, 2 * len(key.cases), "row ends"),
        "key_unsure_ends": _rate(
            "key_unsure_ends", key_unsure, ends_asked, "what the vendor drew is not settled"
        ),
    }
    for name, suffix in (("drawn", ""), ("site", "@site")):
        keyed = keyed_ends[name]
        for outcome in _PAIR_OUTCOMES:
            metric = f"pair_{outcome}{suffix}"
            metrics[metric] = _rate(metric, pair[name][outcome], keyed)
        for model in readers:
            for verdict in _READER_VERDICTS:
                metric = f"reader_{verdict}:{model}{suffix}"
                metrics[metric] = _rate(metric, per_reader[(model, name)][verdict], keyed)
        for verdict in _INK_VERDICTS:
            metric = f"ink_{verdict}{suffix}"
            metrics[metric] = _rate(metric, ink[name][verdict], keyed, "code's wall ink")
    keyed_rows = sum(layouts.values())
    for outcome, metric in (
        (AUTOMATIC_RIGHT, "automatic_right"),
        (AUTOMATIC_WRONG, "automatic_wrong"),
        (SEALED_RIGHT, "sealed_right"),
        (SEALED_WRONG, "sealed_wrong"),
        (HELD_LAYOUT, "held_rows"),
    ):
        metrics[metric] = _rate(metric, layouts[outcome], keyed_rows, "rows, against site_layout")
    metrics["cost_micros_per_question"] = _rate(
        "cost_micros_per_question",
        cost,
        rows_asked,
        "every recorded attempt of both readers; a reused answer costs 0",
    )
    metrics["reused_answers"] = _rate("reused_answers", reused, attempts)
    keyed_questions = {case.question for case in key.cases}
    metrics["unkeyed_questions"] = _rate(
        "unkeyed_questions",
        sum(1 for question in run.questions if question.identity not in keyed_questions),
        len(run.questions),
        f"{run.unidentified} answer(s) had no page hash or question id" if run.unidentified else "",
    )
    metrics["unreadable_stored_answers"] = _rate("unreadable_stored_answers", 0, attempts)
    metrics["skipped_malformed"] = _rate(
        "skipped_malformed",
        run.malformed,
        len(run.questions),
        "stored row ids, row boxes or wall-ink flags that could not be read",
    )
    return Scorecard(
        agent=WALLS_AGENT,
        key_name=key.name,
        key_version=key.version,
        key_sha256=key.sha256,
        code_version=code_version,
        scorer_version=scorer_version,
        extractor_versions={
            "agent": WALLS_AGENT,
            "prompt_ids": ",".join(sorted({question.prompt_id for question in run.questions})),
            "model_ids": ",".join(readers),
            "extractor": f"{run.extractor}@{run.extractor_version}",
            "config_hash": run.config_hash,
            "wall_ink": "the stored wall-ink flags of each row (code's judgment when the run ran)",
            **_code_note(code_version),
        },
        source={"database": run.database, "extraction_run_id": run.extraction_run_id},
        metrics=metrics,
        cases=tuple(cases),
        created_at=created_at or _now(),
    )


# --- compare ------------------------------------------------------------------------------------

_WALLS_HIGHER: Final = (
    "asked",
    "pair_agreed_right",
    "reader_right:",
    "ink_right",
    "automatic_right",
    "sealed_right",
)
_WALLS_NEITHER: Final = (
    "skipped_malformed",
    "unkeyed_questions",
    "reused_answers",
    "key_unsure_ends",
    "ink_none",
    "ink_not_recorded",
    "held_rows",
)
_LISTED: Final = 10


def walls_direction(name: str) -> Direction | None:
    """More is better for rights, less for wrongs; counts that describe the run have no direction."""
    if name.startswith(_WALLS_NEITHER):
        return None
    return Direction.MINIMUM if name.startswith(_WALLS_HIGHER) else Direction.MAXIMUM


def _listed(case_ids: Sequence[str]) -> str:
    shown = ", ".join(case_ids[:_LISTED])
    return shown + (f", and {len(case_ids) - _LISTED} more" if len(case_ids) > _LISTED else "")


def compare_translated(
    before: Scorecard,
    after: Scorecard,
    *,
    outcomes: Mapping[str, str],
    direction: Callable[[str], Direction | None],
    extra_reasons: Callable[[Scorecard, Scorecard], list[str]] | None = None,
) -> Comparison:
    """The Pariksha compare for a scorecard with its own outcome words and metric directions.

    `outcomes` names which of the card's outcomes are the compare's right and wrong ones
    (`AGREED_RIGHT`, `AGREED_WRONG`); the comparison handed back speaks the card's own words.
    `extra_reasons` adds reasons to reject (each one rejects)."""

    def translated(card: Scorecard) -> Scorecard:
        return replace(
            card,
            cases=tuple(
                replace(case, outcome=outcomes.get(case.outcome, case.outcome))
                for case in card.cases
            ),
        )

    base = compare_scorecards(translated(before), translated(after))
    was = {case.case_id: case.outcome for case in before.cases}
    now = {case.case_id: case.outcome for case in after.cases}

    def back(changes: tuple[CaseChange, ...]) -> tuple[CaseChange, ...]:
        return tuple(
            replace(
                change,
                before=was.get(change.case_id, NOT_ASKED),
                after=now.get(change.case_id, NOT_ASKED),
            )
            for change in changes
        )

    reasons = [] if extra_reasons is None else extra_reasons(before, after)
    verdict, accepted = base.verdict, base.accepted
    if reasons:
        joined = "; ".join(reasons)
        verdict = f"REJECT ({joined})" if accepted else f"{verdict[:-1]}; {joined})"
        accepted = False
    return replace(
        base,
        before=before,
        after=after,
        metrics=tuple(replace(delta, direction=direction(delta.metric)) for delta in base.metrics),
        fixed=back(base.fixed),
        broke=back(base.broke),
        new_wrong=back(base.new_wrong),
        moved=back(base.moved),
        dropped=back(base.dropped),
        accepted=accepted,
        verdict=verdict,
    )


def _ink_wrong(card: Scorecard) -> set[str]:
    return {
        case.case_id
        for case in card.cases
        if any(reader.model_id == INK_READER and reader.verdict == WRONG for reader in case.readers)
    }


def _walls_reasons(before: Scorecard, after: Scorecard) -> list[str]:
    reasons: list[str] = []
    ink = sorted(_ink_wrong(after) - _ink_wrong(before))
    if ink:
        reasons.append(f"{len(ink)} new wrong wall ink: {_listed(ink)}")
    was_automatic_wrong = {c.case_id for c in before.cases if c.outcome == AUTOMATIC_WRONG}
    automatic = sorted(
        case.case_id
        for case in after.cases
        if case.outcome == AUTOMATIC_WRONG and case.case_id not in was_automatic_wrong
    )
    if automatic:
        reasons.append(f"{len(automatic)} new wrong automatic layout: {_listed(automatic)}")
    return reasons


def require_agent(before: Scorecard, after: Scorecard, agent: str) -> None:
    """Refuse a compare of any card that is not this agent's: another compare would judge it by
    rules that are not its own (a walls card judged without its wall ink, say)."""
    for card in (before, after):
        if card.agent != agent:
            raise IncomparableRuns(
                f"this compare judges {agent} scorecards only; this is a {card.agent} scorecard "
                f"(before: {before.agent}, after: {after.agent})"
            )


def compare_walls(before: Scorecard, after: Scorecard) -> Comparison:
    """Better, with 0 new agreed-wrong ends, 0 new wrong wall ink and 0 new wrong automatic rows.

    A new wrong *sealed* layout does not reject on its own: it is the readers' proposal, never
    automatic, and nothing is pre-selected (see the module's **Compare**)."""
    require_agent(before, after, WALLS_AGENT)
    return compare_translated(
        before, after, outcomes={}, direction=walls_direction, extra_reasons=_walls_reasons
    )
