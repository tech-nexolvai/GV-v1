#!/usr/bin/env python3
"""Measure Khoj, the value finder, on a stored run: read-only, and no model call (#1231).

`workflow/value_hunter.py` (#881) looks for each setting the rules still need and points at the
passage of the package that states it. It is off (`GV_VALUE_HUNTER`) and had never been measured.
This runs it, exactly as the worker would, on stored package revisions and reports, per outstanding
setting: a pointer was proposed (where), or not found (and why). It changes no behaviour and
switches nothing on.

    DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5434/<database> \\
        .venv/bin/python scripts/measure_value_hunter.py [--revision <id> ...] \\
        [--truth truth.json] [--json report.json] [--with-text]

**Read-only, twice over.**

- The transaction is `READ ONLY` from its first statement and is rolled back at the end, so the
  database refuses any write, whatever the code under measurement tries.
- The hunter's one write is filing a pointer (`workflow.parameter_proposals._record`). For the length
  of a measurement that one function is swapped for a stand-in that writes nothing and keeps the
  pointer in memory instead; everything that decides whether a pointer is filed (the plan, the
  searches, the guard, the second-value check) runs unchanged. The original is put back afterwards.

**No model call.** The hunter's planner is a code-owned table; nothing here imports a model client.

**Graded where the truth is known** (`--truth`, a local JSON file, never committed), per revision and
setting: whether the architect states the setting in words in this package, and the value:

    {"<revision id>": {"backsplash_thickness": {"stated": true, "value": "5/8",
                                                "note": "where it is stated"},
                       "countertop_overhang": {"stated": false, "note": "why not"}}}

- a pointer is **right** when its passage's number is the true value, and **wrong** when it differs
  or the architect states no such value;
- a not-found is **right** when the architect states no such value in words, and a **miss** when
  they do;
- anything without a truth entry is **ungraded**, and a pointer whose passage also names a different
  dimension (a height, say) is marked suspect.

**Every passage the searches found is audited** against the guard, read-only: its side, whether it
holds a number, and whether that number reads as one inch dimension, so a not-found says which of
those stopped it.

**What leaves the database.** The default report and JSON hold ids, page numbers, the code's own
reasons and counts: no passage's words and no number. `--with-text` adds both, for a local report
about a client's drawings, which is never committed. A database at an older schema than this code is
refused with a plain message (exit code 3): measure a copy migrated to the current schema.

Source: issue #1231 (step 5.3 of #798). Verification: `tests/scripts/test_measure_value_hunter.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Final
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.evidence.sides import ReadingSides, SideRefusal
from app.models.document import Document, DocumentVersion, Page
from app.models.package import PackageRevision
from app.models.package_text import TextPhrase
from retrieval.package_text import search_package_text
from rules.parameter_sources import citable_sources
from workflow import parameter_proposals
from workflow.parameter_citations import (
    Citation,
    CitationRefusal,
    _check,
    _read_inch_dimension,
    _runs,
)
from workflow.parameter_proposals import _span
from workflow.value_hunter import (
    HuntProgress,
    NotFound,
    PlannedSetting,
    Proposal,
    ToolCall,
    hunt_setting,
    plan_hunt,
    table_planner,
)

#: How a setting's measurement is graded.
RIGHT: Final = "right"
WRONG: Final = "wrong"
MISSED: Final = "missed"
RIGHT_NOT_FOUND: Final = "right_not_found"
UNGRADED: Final = "ungraded"
NOT_SEARCHED: Final = "not_searched"

#: Words that name a different dimension from the setting's own: a pointer whose passage holds one
#: (`4" HIGH BACKSPLASH` for the backsplash's thickness) is marked suspect. A measurement aid only.
OTHER_DIMENSION_WORDS: Final[Mapping[str, tuple[str, ...]]] = {
    "countertop_overhang": ("HIGH", "HT", "HEIGHT", "TALL", "THICK", "THK"),
    "backsplash_thickness": ("HIGH", "HT", "HEIGHT", "TALL", "WIDE", "LONG"),
    "cabinet_side_thickness": ("HIGH", "HT", "HEIGHT", "TALL", "WIDE", "DEEP", "DEPTH"),
    "cabinet_depth": ("HIGH", "HT", "HEIGHT", "TALL", "WIDE", "THICK", "THK"),
}


# ---------------------------------------------------------------------------
# The truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingTruth:
    """What the architect's part of the package states for one setting, as a person found it."""

    stated: bool
    value: Fraction | None = None
    note: str = ""


def load_truth(path: Path) -> dict[UUID, dict[str, SettingTruth]]:
    """The truth file: per revision id, per setting, stated or not, and the value when stated."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    truth: dict[UUID, dict[str, SettingTruth]] = {}
    for revision, settings in raw.items():
        entries: dict[str, SettingTruth] = {}
        for setting, entry in settings.items():
            stated = entry["stated"]
            if not isinstance(stated, bool):
                raise TypeError(f"{revision} {setting}: 'stated' must be true or false")
            value = entry.get("value")
            entries[setting] = SettingTruth(
                stated=stated,
                value=None if value is None else Fraction(str(value)),
                note=str(entry.get("note", "")),
            )
        truth[UUID(revision)] = entries
    return truth


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _HeldBack:
    """What the stand-in returns where a stored pointer row would be: no row, so no id."""

    id: None = None


@contextmanager
def pointers_held_back() -> Iterator[list[Citation]]:
    """For the duration, a pointer the hunter would file is kept in the list instead of written."""
    held: list[Citation] = []
    original = parameter_proposals._record

    def _keep(_session: Session, _revision: UUID, citation: Citation) -> _HeldBack:
        held.append(citation)
        return _HeldBack()

    parameter_proposals._record = _keep  # type: ignore[assignment]
    try:
        yield held
    finally:
        parameter_proposals._record = original


@contextmanager
def read_only_session(url: str) -> Iterator[tuple[Session, list[Citation]]]:
    """A session in a `READ ONLY` transaction that is always rolled back, with pointers held back."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            # First in the transaction, so nothing after it can write.
            connection.execute(text("SET TRANSACTION READ ONLY"))
            try:
                with Session(bind=connection) as session, pointers_held_back() as held:
                    yield session, held
            finally:
                connection.rollback()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PassageAudit:
    """One passage a search for the setting returned, checked against the guard, read-only."""

    phrase_id: str
    page: int
    """1-based, as a person counts pages in the document."""

    document_kind: str
    tag: str
    sides: tuple[str, ...]
    """The side of each of the passage's runs, deduplicated: ARCH, SHOP or why it has none."""

    has_number: bool
    number_readable: bool
    """The number reads as exactly one inch dimension, whatever its side."""

    guard: str
    """`citable`, or the guard's refusal reason (`no_number` when no run holds a digit)."""

    text: str | None = None
    number: str | None = None


@dataclass(frozen=True, slots=True)
class SettingMeasurement:
    """One outstanding setting: how it was looked for, how it ended, and the grade."""

    setting: str
    searched: bool
    words: tuple[tuple[str, int], ...]
    """Each search word and how many passages it found."""

    ending: str
    """`proposed` or `not_found`."""

    reason: str
    """The hunter's own reason for a not-found; empty for a pointer."""

    pointer: PassageAudit | None
    passages: tuple[PassageAudit, ...]
    grade: str
    basis: str


@dataclass(frozen=True, slots=True)
class RevisionMeasurement:
    """Every outstanding setting of one package revision, in the hunter's order."""

    package_revision_id: str
    settings: tuple[SettingMeasurement, ...]
    held_back: int
    """Pointers the hunter would have filed, kept in memory instead."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _Recorder:
    """The production planner, with the last progress it was shown kept."""

    last: HuntProgress | None = None
    calls: list[ToolCall] = field(default_factory=list)

    def __call__(self, progress: HuntProgress) -> ToolCall:
        self.last = progress
        call = table_planner(progress)
        self.calls.append(call)
        return call


def _side_name(side: object) -> str:
    if isinstance(side, SideRefusal):
        return side.reason.value
    return str(getattr(side, "value", side))


def audit_passage(
    session: Session,
    sides: ReadingSides,
    package_revision_id: UUID,
    setting: str,
    phrase_id: UUID,
    *,
    with_text: bool,
) -> PassageAudit:
    """The passage checked as `propose_passage` checks it, without filing anything."""
    phrase = session.get(TextPhrase, phrase_id)
    if phrase is None:  # pragma: no cover - a search returns only stored phrases
        raise ValueError(f"no phrase {phrase_id}")
    page = session.get(Page, phrase.page_id)
    kind = session.execute(
        select(Document.kind)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(DocumentVersion.id == (None if page is None else page.document_version_id))
    ).scalar_one_or_none()
    runs = _runs(session, phrase_id) or ()
    run_sides = tuple(
        dict.fromkeys(_side_name(sides.of(run, confirmed_views_only=True)) for run in runs)
    )
    span = _span([run.raw_text for run in runs])
    number: Fraction | None = None
    if span is None:
        guard = "no_number"
    else:
        (source,) = citable_sources(setting)
        checked = _check(
            session,
            sides,
            package_revision_id=package_revision_id,
            setting=setting,
            phrase_id=phrase_id,
            first_member=span[0],
            last_member=span[1],
            claimed_source=source,
        )
        guard = checked.reason.value if isinstance(checked, CitationRefusal) else "citable"
        read = _read_inch_dimension(" ".join(run.raw_text for run in runs[span[0] : span[1] + 1]))
        number = None if isinstance(read, CitationRefusal) else read
    return PassageAudit(
        phrase_id=str(phrase_id),
        page=-1 if page is None else page.index + 1,
        document_kind=str(kind),
        tag=phrase.tag,
        sides=run_sides,
        has_number=span is not None,
        number_readable=number is not None,
        guard=guard,
        text=phrase.content if with_text else None,
        number=None if number is None or not with_text else str(number),
    )


def _names_another_dimension(setting: str, words: str) -> bool:
    return any(
        re.search(rf"\b{word}\b", words.upper()) for word in OTHER_DIMENSION_WORDS.get(setting, ())
    )


def _grade(
    session: Session,
    sides: ReadingSides,
    package_revision_id: UUID,
    setting: str,
    pointer: PassageAudit | None,
    truth: SettingTruth | None,
) -> tuple[str, str]:
    """The grade and what it rests on, in words that quote nothing from the drawing."""
    if pointer is not None:
        phrase = session.get(TextPhrase, UUID(pointer.phrase_id))
        suspect = phrase is not None and _names_another_dimension(setting, phrase.content)
        if truth is None:
            return UNGRADED, (
                "no truth known; the passage also names a different dimension (suspect)"
                if suspect
                else "no truth known"
            )
        if not truth.stated:
            return WRONG, "the architect states no such value in words in this package"
        audited = audit_passage(
            session, sides, package_revision_id, setting, UUID(pointer.phrase_id), with_text=True
        )
        value = None if audited.number is None else Fraction(audited.number)
        if truth.value is not None and value == truth.value:
            return RIGHT, "the passage's number is the value the architect states"
        return WRONG, "the passage's number is not the value the architect states"
    if truth is None:
        return UNGRADED, "no truth known"
    if truth.stated:
        return MISSED, "the architect states this setting in words in this package"
    return RIGHT_NOT_FOUND, "the architect states no such value in words in this package"


def measure_setting(
    session: Session,
    sides: ReadingSides,
    package_revision_id: UUID,
    planned: PlannedSetting,
    *,
    truth: SettingTruth | None,
    with_text: bool,
) -> SettingMeasurement:
    """Run the hunter on one planned setting, as `hunt_values` does, and audit what it found."""
    if planned.not_searched is not None:
        return SettingMeasurement(
            setting=planned.setting,
            searched=False,
            words=(),
            ending="not_found",
            reason=planned.not_searched,
            pointer=None,
            passages=(),
            grade=NOT_SEARCHED,
            basis="the hunter does not look for this setting, by design",
        )
    recorder = _Recorder()
    ending = hunt_setting(session, package_revision_id, planned, recorder)
    words = tuple(
        (word, len(search_package_text(session, package_revision_id, word)))
        for word in planned.terms
    )
    found = () if recorder.last is None else recorder.last.found
    passages = tuple(
        audit_passage(
            session, sides, package_revision_id, planned.setting, phrase_id, with_text=with_text
        )
        for phrase_id in found
    )
    pointer = None
    if isinstance(ending, Proposal):
        pointer = next(
            (passage for passage in passages if passage.phrase_id == str(ending.phrase_id)),
            None,
        ) or audit_passage(
            session,
            sides,
            package_revision_id,
            planned.setting,
            ending.phrase_id,
            with_text=with_text,
        )
    grade, basis = _grade(session, sides, package_revision_id, planned.setting, pointer, truth)
    return SettingMeasurement(
        setting=planned.setting,
        searched=True,
        words=words,
        ending="proposed" if isinstance(ending, Proposal) else "not_found",
        reason=ending.reason if isinstance(ending, NotFound) else "",
        pointer=pointer,
        passages=passages,
        grade=grade,
        basis=basis,
    )


class UnknownRevision(ValueError):
    """The database holds no such package revision."""


def measure_revision(
    session: Session,
    package_revision_id: UUID,
    *,
    held: list[Citation],
    truth: Mapping[str, SettingTruth] | None = None,
    with_text: bool = False,
) -> RevisionMeasurement:
    """Every outstanding setting of one revision, in the hunter's order, graded where truth is known."""
    if session.get(PackageRevision, package_revision_id) is None:
        raise UnknownRevision(f"no package revision {package_revision_id}")
    sides = ReadingSides(session)
    before = len(held)
    known = truth or {}
    settings = tuple(
        measure_setting(
            session,
            sides,
            package_revision_id,
            planned,
            truth=known.get(planned.setting),
            with_text=with_text,
        )
        for planned in plan_hunt(session, package_revision_id)
    )
    return RevisionMeasurement(
        package_revision_id=str(package_revision_id),
        settings=settings,
        held_back=len(held) - before,
    )


def built_revisions(session: Session) -> tuple[UUID, ...]:
    """Every revision with a phrase index, which is all the hunter can search, sorted."""
    return tuple(
        session.execute(
            select(TextPhrase.package_revision_id)
            .distinct()
            .order_by(TextPhrase.package_revision_id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def tally(measurements: Sequence[RevisionMeasurement]) -> dict[str, Any]:
    """The counts across every revision measured: no passage's words and no number."""
    settings = [setting for revision in measurements for setting in revision.settings]
    searched = [setting for setting in settings if setting.searched]
    stopped = Counter(
        passage.guard
        for setting in searched
        if setting.ending == "not_found"
        for passage in setting.passages
    )
    return {
        "revisions": len(measurements),
        "outstanding": len(settings),
        "not_searched": len(settings) - len(searched),
        "searched": len(searched),
        "proposed": sum(setting.ending == "proposed" for setting in searched),
        "not_found": sum(setting.ending == "not_found" for setting in searched),
        "not_found_no_passage": sum(
            setting.ending == "not_found" and not setting.passages for setting in searched
        ),
        "not_found_passages_refused": sum(
            setting.ending == "not_found" and bool(setting.passages) for setting in searched
        ),
        "passages_refused_by_reason": dict(sorted(stopped.items())),
        "grades": dict(sorted(Counter(setting.grade for setting in settings).items())),
        "held_back_pointers": sum(revision.held_back for revision in measurements),
    }


def render(measurements: Sequence[RevisionMeasurement]) -> str:
    """The measurement as plain text: one block per revision, then the totals."""
    lines: list[str] = []
    for revision in measurements:
        lines.append(f"Package revision {revision.package_revision_id}")
        for setting in revision.settings:
            if not setting.searched:
                lines.append(
                    f"  - {setting.setting}: not searched ({setting.reason}) [{setting.grade}]"
                )
                continue
            words = ", ".join(f"{word} -> {count}" for word, count in setting.words)
            if setting.pointer is not None:
                where = (
                    f"page {setting.pointer.page} ({setting.pointer.document_kind}, "
                    f"{setting.pointer.tag})"
                )
                lines.append(f"  - {setting.setting}: PROPOSED, {where} [{setting.grade}]")
            else:
                lines.append(f"  - {setting.setting}: not found [{setting.grade}]")
                lines.append(f"      why: {setting.reason}")
            lines.append(f"      searched: {words}")
            lines.append(f"      grade: {setting.basis}")
            for passage in setting.passages:
                said = "" if passage.text is None else f" {passage.text!r}"
                number = "" if passage.number is None else f" number={passage.number}"
                lines.append(
                    f"      passage page {passage.page} {passage.document_kind}/{passage.tag} "
                    f"sides={','.join(passage.sides) or '-'} guard={passage.guard} "
                    f"readable={passage.number_readable}{number}{said}"
                )
        lines.append("")
    lines.append("Totals: " + json.dumps(tally(measurements), sort_keys=True))
    return "\n".join(lines)


def _schema(engine: Engine) -> str:
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 - only for the message
        return "unknown"
    return str(version) if version else "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument(
        "--revision",
        type=UUID,
        action="append",
        help="a package revision id; repeat for more (default: every revision with a phrase index)",
    )
    parser.add_argument(
        "--truth", type=Path, help="the local truth file (see the module docstring)"
    )
    parser.add_argument("--json", type=Path, help="also save the measurement as JSON to this file")
    parser.add_argument(
        "--with-text",
        action="store_true",
        help="include each passage's words and number (for a local report only; never commit it)",
    )
    parser.add_argument("--database-url", help="overrides DATABASE_URL")
    args = parser.parse_args(argv)
    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        parser.error("set DATABASE_URL or pass --database-url")
    truth = {} if args.truth is None else load_truth(args.truth)

    try:
        with read_only_session(url) as (session, held):
            revisions = tuple(args.revision or built_revisions(session))
            measurements = [
                measure_revision(
                    session,
                    revision,
                    held=held,
                    truth=truth.get(revision),
                    with_text=args.with_text,
                )
                for revision in revisions
            ]
    except UnknownRevision as error:
        print(f"measure value hunter: {error}", file=sys.stderr)
        return 2
    except ProgrammingError:
        engine = create_engine(url)
        try:
            schema = _schema(engine)
        finally:
            engine.dispose()
        print(
            "measure value hunter: this database's tables do not match this code (it is at schema "
            f"{schema}, probably older). Nothing was changed. Measure a copy migrated to the "
            "current schema.",
            file=sys.stderr,
        )
        return 3

    print(render(measurements))
    if args.json is not None:
        report = {
            "revisions": [measurement.to_dict() for measurement in measurements],
            "totals": tally(measurements),
        }
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main(sys.argv[1:]))
