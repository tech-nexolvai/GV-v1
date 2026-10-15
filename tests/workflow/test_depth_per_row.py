"""Depth and sink checks per countertop row from code's own suggestion, on two judgments (#1390).

Verification for `workflow/depth_rows.py`, `workflow/depth_evidence.suggestion_depth_operands`,
their use in `DatabaseStages.run_checks` behind `depth_per_row` (`GV_DEPTH_PER_ROW`), the screens'
Confirm offer (`app/review/depth_readings.depth_readings_for`) and the confirm path's own re-check
(`app/review/evidence_actions.confirm_candidate_reading`).

**The bar.** A row gets the five checks of its own only where both judgments agree on which
countertop it is: (1) Bala's suggestion would be automatic (both AI row readers named the row, code
finds it wholly inside one vendor drawing, ...), and (2) that drawing holds exactly one countertop,
no reader named another on the page, and every view tied to the suggestion lies inside the drawing.
Anything less keeps today's held result, with why. A value still needs its own two judgments or a
person's Confirm. Switched off, every result is exactly as before.

Every value here is made up: the qualifying row is `tests/workflow/test_bala_automatic.py`'s.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.auth import Principal, Role
from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
    DrawingItem,
    ItemViewLink,
    ObservationCandidate,
    PackageRevision,
)
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun
from app.models.verdicts import Finding
from app.review.depth_readings import depth_readings_for
from app.review.evidence_actions import (
    HeldReadingRefusal,
    HeldReadingRefused,
    confirm_candidate_reading,
)
from app.review.review_items import _DepthItem
from app.review.session import open_session
from storage.local import LocalStore
from tests.api.test_depth_readings import _crop
from tests.workflow.test_bala_automatic import _countertop, _qualifying, _vendors
from tests.workflow.test_bala_items import _Revision
from tests.workflow.test_depth_evidence import CT007, _findings, _Set
from tests.workflow.test_part_proposals_route import _upgrade
from tests.workflow.test_stages import _publish_rulebook
from verdict.outcomes import Outcome
from vocabulary.part_kinds import PartKind
from workflow.bala_items import propose_items
from workflow.countertop_runs import withdraw_countertop_run
from workflow.depth_evidence import (
    CLAUDE_PAIR,
    CONFIRM_ONLY,
    DEPTH_EXTRACTOR,
    DEPTH_READER_VERSION,
    DEPTH_RULE_IDS,
    DEPTH_SPAN_PROMPT_ID,
    QUALIFIED,
)
from workflow.depth_rows import (
    NOT_PER_ROW,
    ROW_CHECK_NOTE,
    VIEW_IN_ANOTHER,
    VIEW_IN_DRAWING,
    depth_rows,
    row_check_proposal,
    view_in_drawing,
)
from workflow.parts import confirm_part, record_part_proposal, withdraw_part
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PERSON = "reviewer@example.com"
#: A plan view well inside the synthetic drawing (`tests/workflow/test_bala_items.REGION`).
INSIDE: dict[str, object] = {
    "space": "stored",
    "points": [["0.20", "0.20"], ["0.40", "0.20"], ["0.40", "0.40"], ["0.20", "0.40"]],
}
#: A view a third of which lies off the drawing.
ASTRIDE: dict[str, object] = {
    "space": "stored",
    "points": [["0.00", "0.00"], ["0.30", "0.00"], ["0.30", "0.30"], ["0.00", "0.30"]],
}


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


class _Rows:
    """Qualifying countertop rows (both judgments agree), Bala's suggestions from them, and one
    depth-reader run; readings are written through `test_depth_evidence._Set`'s own helper."""

    def __init__(self, session: Session, *, pages: tuple[int, ...] = (0,), **row: object) -> None:
        self.session = session
        if pages == (0,):
            self.built, anchor = _qualifying(session, **row)
            self.anchors = [anchor]
        else:
            self.built = _Revision(session, role=None)
            _vendors(session, self.built)
            run = self.built.run()
            self.anchors = [
                self.built.row(run, page=page, pieces=3, **row)  # type: ignore[arg-type]
                for page in pages
            ]
        self.revision: PackageRevision = self.built.revision
        propose_items(session, self.revision.id)
        self.proposals = [_countertop(session, anchor).id for anchor in self.anchors]
        task = TaskRun(
            workflow_run_id=self.built.workflow.id,
            idempotency_key=f"synthetic-{uuid4()}",
            task_type="match",
            attempt=1,
            outcome="SUCCEEDED",
        )
        session.add(task)
        session.flush()
        self.run = ExtractionRun(
            task_run_id=task.id,
            extractor=DEPTH_EXTRACTOR,
            extractor_version=DEPTH_READER_VERSION,
            config_hash="synthetic",
            # The slot reader's resolution, so `_Set`'s readings lie inside the synthetic drawing.
            dpi=150,
        )
        session.add(self.run)
        session.flush()
        self.sheets: list[_Set] = []
        for index, proposal in enumerate(self.proposals):
            sheet = _Set.__new__(_Set)
            sheet.session = session
            sheet.revision = self.revision
            sheet.page = session.get_one(
                type(self.built.pages[0]), self.built.pages[pages[index]].id
            )
            sheet.run = self.run
            sheet.views = 0
            sheet.proposal = proposal
            self.sheets.append(sheet)

    @property
    def anchor(self) -> UUID:
        return self.anchors[0]

    @property
    def proposal(self) -> UUID:
        return self.proposals[0]

    def link(
        self,
        index: int = 0,
        *,
        region: dict[str, object] | None = None,
        proposal: UUID | None = None,
    ) -> ItemViewLink:
        sheet = self.sheets[index]
        link = ItemViewLink(
            part_proposal_id=proposal or self.proposals[index],
            page_id=sheet.page.id,
            region=region or INSIDE,
            view_kind="plan",
            heading_text="PLAN VIEW",
            judged_by="code+ais",
            extraction_run_id=self.run.id,
            linked_by="code:depth-reader/v1",
        )
        self.session.add(link)
        self.session.flush()
        return link

    def reading(
        self, field: str, inches: int, state: str, *, index: int = 0, **kwargs: Any
    ) -> ObservationCandidate:
        if state == CONFIRM_ONLY:
            kwargs.setdefault("confirmed_by", "drawn-length-only")
        sheet = self.sheets[index]
        if sheet.page.index == 0:
            return sheet.reading(field, inches, state, **kwargs)
        # `_Set` records its readers' answers on page 1 only: these are on this page.
        row = sheet.reading(field, inches, state, answers=False, **kwargs)
        question = next(
            f.removeprefix("question:") for f in row.ambiguity_flags if f.startswith("question:")
        )
        for model in CLAUDE_PAIR:
            _answer(sheet, question, model, row.raw_text)
        return row

    def check(self, *, on: bool = True, reader: bool = True) -> dict[str, list[Finding]]:
        _publish_rulebook_once(self.session)
        DatabaseStages(
            discriminators={"wall_config": "back_left_right"},
            depth_reader=reader,
            depth_per_row=on,
        ).run_checks(self.session, self.revision.id)
        return _findings(self.session, self.revision)

    def second_countertop(self, index: int = 0) -> None:
        """A person confirms another countertop on the same drawing."""
        view = self.built.views[index]
        proposal = record_part_proposal(
            self.session,
            drawing_view_id=view.id,
            kind=PartKind.COUNTERTOP,
            extent=[(Decimal("0.60"), Decimal("0.60")), (Decimal("0.90"), Decimal("0.70"))],
            defining_line=((Decimal("0.60"), Decimal("0.65")), (Decimal("0.90"), Decimal("0.65"))),
            code_as_printed=None,
            code_candidate_id=None,
            reason="a person's countertop",
            source="reviewer",
            source_version="test",
            find_existing=False,
        )
        confirm_part(
            self.session, proposal=proposal, kind=PartKind.COUNTERTOP, code=None, actor=PERSON
        )
        self.session.flush()


def _answer(sheet: _Set, question: str, model: str, text: str) -> None:
    """`_Set.answer`, on the sheet's own page."""
    body: dict[str, object] = {
        "reader": "depth-reader",
        "document_version_id": str(sheet.page.document_version_id),
        "page_index": sheet.page.index,
        "question_id": question,
        "prompt_id": DEPTH_SPAN_PROMPT_ID,
    }
    body["packet_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    sheet.session.add(
        ModelInvocation(
            extraction_run_id=sheet.run.id,
            model_id=model,
            prompt_id=DEPTH_SPAN_PROMPT_ID,
            template_id=DEPTH_SPAN_PROMPT_ID,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            outcome="ok",
            private_raw_response=json.dumps(
                {
                    "belongs": "yes",
                    "text": text,
                    "stacked": False,
                    "combined": False,
                    "readable": True,
                    "no_dimension": False,
                }
            ),
            reader_question_packet=body,
        )
    )
    sheet.session.flush()


def _publish_rulebook_once(session: Session) -> None:
    from app.models import RuleDefinition

    if session.scalar(select(RuleDefinition.id).limit(1)) is None:
        _publish_rulebook(session)


def _row_results(findings: dict[str, list[Finding]], anchor: UUID) -> dict[str, Finding]:
    found: dict[str, Finding] = {}
    for rule_id in DEPTH_RULE_IDS:
        mine = [f for f in findings.get(rule_id, []) if f.scope_row_candidate_id == anchor]
        assert len(mine) <= 1, rule_id
        if mine:
            found[rule_id] = mine[0]
    return found


def _shape(findings: dict[str, list[Finding]]) -> list[tuple[object, ...]]:
    """Everything a result says, without its ids or times: equal means the same results."""
    return sorted(
        (
            rule_id,
            f.outcome,
            f.severity,
            f.reason,
            tuple(f.notes or ()),
            None if f.scope_item_id is None else str(f.scope_item_id),
            None if f.scope_row_candidate_id is None else str(f.scope_row_candidate_id),
            f.scope_label,
            f.delta_numerator,
            f.delta_denominator,
        )
        for rule_id, listed in findings.items()
        for f in listed
    )


# -- switched off: exactly as before ---------------------------------------------------------------


def test_switched_off_every_result_is_exactly_as_before(session: Session) -> None:
    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())

    before = _shape(rows.check(on=False))
    default = DatabaseStages(discriminators={"wall_config": "back_left_right"}, depth_reader=True)
    default.run_checks(session, rows.revision.id)
    unset = _shape(_findings(session, rows.revision))
    # On, but the reader off: the switch acts only with the reader.
    reader_off = _shape(rows.check(on=True, reader=False))
    reader_off_before = _shape(rows.check(on=False, reader=False))

    assert unset == before
    assert reader_off == reader_off_before
    assert not any(row_check_proposal(shape[4]) for shape in before)
    assert DatabaseStages()._depth_per_row is False


def test_a_set_the_reader_never_read_is_exactly_as_before(session: Session) -> None:
    """Nothing to check per row without a depth reading: switched on, the same results."""
    built, _anchor = _qualifying(session)
    propose_items(session, built.revision.id)
    _publish_rulebook_once(session)
    shapes = []
    for on in (False, True):
        DatabaseStages(
            discriminators={"wall_config": "back_left_right"}, depth_reader=True, depth_per_row=on
        ).run_checks(session, built.revision.id)
        shapes.append(_shape(_findings(session, built.revision)))

    assert shapes[0] == shapes[1]


def test_the_switch_must_be_a_bool() -> None:
    with pytest.raises(TypeError, match="depth_per_row"):
        DatabaseStages(depth_per_row="yes")  # type: ignore[arg-type]


# -- both judgments agree: the row's own five checks -----------------------------------------------


def test_two_judgments_give_the_row_its_own_five_checks(session: Session) -> None:
    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())

    off = rows.check(on=False)
    on = rows.check(on=True)

    # Off: one result for the whole package, as today.
    assert _row_results(off, rows.anchor) == {}
    # On: each of the five is the row's own, and the package has no other.
    own = _row_results(on, rows.anchor)
    assert set(own) == DEPTH_RULE_IDS
    for rule_id in DEPTH_RULE_IDS:
        assert len(on[rule_id]) == 1, rule_id
        finding = own[rule_id]
        assert finding.scope_item_id is None
        assert row_check_proposal(finding.notes) == rows.proposal
        assert ROW_CHECK_NOTE.format(proposal=rows.proposal, view=rows.built.views[0].id) in (
            finding.notes or []
        )
        assert finding.scope_label == "Countertop row on page 1"
    # A qualified 4" front offset (both readers and code's printed-text check) decides it.
    assert own["CT-SINK-OFFSET-FRONT-001"].outcome == Outcome.PASS.value
    # Nothing read for the depth: not found, with the plain reason, never a guess.
    assert own["CT-DEPTH-001"].outcome == Outcome.NOT_FOUND.value
    assert own["CT-DEPTH-001"].reason
    # The sink cabinet check never runs per countertop: untouched.
    assert all(f.scope_row_candidate_id is None for f in on["CT-SINK-CABINET-WIDTH-001"])


def test_a_wrong_value_fails_on_the_row(session: Session) -> None:
    rows = _Rows(session)
    rows.reading(CT007, 2, QUALIFIED, link=rows.link())

    own = _row_results(rows.check(), rows.anchor)

    assert own["CT-SINK-OFFSET-FRONT-001"].outcome == Outcome.FAIL.value


def test_a_drawn_length_value_waits_for_a_person_on_the_row(session: Session) -> None:
    """The value's own two judgments still apply: drawn length alone never decides."""
    rows = _Rows(session)
    rows.reading(CT007, 4, CONFIRM_ONLY, link=rows.link())

    own = _row_results(rows.check(), rows.anchor)

    front = own["CT-SINK-OFFSET-FRONT-001"]
    assert front.outcome == Outcome.REVIEW_REQUIRED.value
    assert "Confirm this reading" in (front.reason or "")


def test_another_countertop_s_reading_never_fills_the_row(session: Session) -> None:
    rows = _Rows(session, pages=(0, 1))
    # A 4" read on the second countertop's own view only.
    rows.reading(CT007, 4, QUALIFIED, index=1, link=rows.link(1))

    findings = rows.check()

    first = _row_results(findings, rows.anchors[0])
    second = _row_results(findings, rows.anchors[1])
    assert first["CT-SINK-OFFSET-FRONT-001"].outcome == Outcome.NOT_FOUND.value
    assert second["CT-SINK-OFFSET-FRONT-001"].outcome == Outcome.PASS.value, second[
        "CT-SINK-OFFSET-FRONT-001"
    ].reason


def test_several_countertops_each_checked_on_its_own_row_leave_no_held_result(
    session: Session,
) -> None:
    rows = _Rows(session, pages=(0, 1))
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())
    rows.reading(CT007, 4, QUALIFIED, index=1, link=rows.link(1))

    findings = rows.check()

    for rule_id in DEPTH_RULE_IDS:
        listed = findings[rule_id]
        assert {f.scope_row_candidate_id for f in listed} == set(rows.anchors), rule_id
        assert all(row_check_proposal(f.notes) is not None for f in listed), rule_id


# -- every judgment, and every fail-closed case: held, with why ------------------------------------


def _held_on_one_countertop(rows: _Rows, why: str) -> None:
    """On a package with one countertop: no row result; the whole-package result, saying why."""
    findings = rows.check()
    assert _row_results(findings, rows.anchor) == {}
    for rule_id in DEPTH_RULE_IDS:
        (finding,) = findings[rule_id]
        assert finding.scope_row_candidate_id is None and finding.scope_item_id is None
        assert any(
            note.startswith(NOT_PER_ROW.split("{why}")[0]) and why in note
            for note in finding.notes or ()
        ), (rule_id, finding.notes)


def test_judgment_one_the_readers_did_not_both_name_the_row(session: Session) -> None:
    rows = _Rows(session, chosen=False)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())

    _held_on_one_countertop(rows, "the AI readers did not both name this row")


def test_judgment_one_a_held_row(session: Session) -> None:
    rows = _Rows(session, extra=("row-hold:synthetic",))
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())

    _held_on_one_countertop(rows, "do not both say which countertop this row is")


def test_judgment_one_no_printed_overall(session: Session) -> None:
    rows = _Rows(session, overall=False)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())

    _held_on_one_countertop(rows, "no overall length is printed for it")


def test_judgment_two_a_second_countertop_on_the_drawing(session: Session) -> None:
    """A person's countertop beside the row's on the same drawing: which one a plan or section
    shows is not known, so the row is not checked on its own. (The package counts the two as one
    here, `several_countertops.package_countertops`, so its one result is the whole package's.)"""
    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())
    rows.second_countertop()

    _held_on_one_countertop(rows, "its drawing holds 2 countertops")


def test_judgment_two_a_second_countertop_on_a_drawing_of_several(session: Session) -> None:
    """The same on a package with several countertops: the row keeps its own held result, which
    says why, and the other row is still checked on its own."""
    rows = _Rows(session, pages=(0, 1))
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())
    rows.reading(CT007, 4, QUALIFIED, index=1, link=rows.link(1))
    rows.second_countertop(0)

    findings = rows.check()

    held = _row_results(findings, rows.anchors[0])
    assert set(held) == DEPTH_RULE_IDS
    for finding in held.values():
        assert row_check_proposal(finding.notes) is None
        assert finding.outcome == Outcome.REVIEW_REQUIRED.value
        assert "its drawing holds 2 countertops" in (finding.reason or "")
    other = _row_results(findings, rows.anchors[1])
    assert all(row_check_proposal(f.notes) == rows.proposals[1] for f in other.values())


def test_judgment_two_another_row_named_on_the_page(session: Session) -> None:
    rows = _Rows(session, extra=("row-also:opus:2",))
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())

    findings = rows.check()

    assert all(row_check_proposal(f.notes) is None for r in DEPTH_RULE_IDS for f in findings[r])
    held = _row_results(findings, rows.anchor)
    assert held and all(
        "a reader named another countertop row on this page" in (f.reason or "")
        for f in held.values()
    )


def test_judgment_two_a_view_astride_the_drawing(session: Session) -> None:
    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link(region=ASTRIDE))

    _held_on_one_countertop(rows, "does not lie clearly inside its drawing")


def test_a_person_taking_back_the_suggestion_holds_the_row(session: Session) -> None:
    from app.models import PartProposal

    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())
    withdraw_part(session, proposal=session.get_one(PartProposal, rows.proposal), actor=PERSON)

    _held_on_one_countertop(rows, "a person took back the countertop suggested from this row")


def test_a_person_s_run_keeps_the_revision_on_the_advanced_path(session: Session) -> None:
    """A person decided a run (here: withdrew one) beneath a countertop they confirmed: the whole
    revision stays on that path, exactly as with the switch off."""
    from app.models import PartProposal

    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())
    decision = confirm_part(
        session,
        proposal=session.get_one(PartProposal, rows.proposal),
        kind=PartKind.COUNTERTOP,
        code=None,
        actor=PERSON,
    )
    assert decision.drawing_item_id is not None
    withdraw_countertop_run(session, countertop_item_id=decision.drawing_item_id, actor=PERSON)

    assert _shape(rows.check(on=True)) == _shape(rows.check(on=False))


def test_a_person_s_value_for_the_whole_package_stands_on_one_countertop(
    session: Session,
) -> None:
    """A person confirmed a front offset that is not a depth reader's reading: on a package with
    one countertop, each check reading it keeps today's whole-package result, saying why the row is
    not checked on its own; the reader's value never replaces a person's."""
    from app.evidence.confirm import confirm_candidate_type

    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())
    labelled = ObservationCandidate(
        document_version_id=rows.sheets[0].page.document_version_id,
        page_id=rows.sheets[0].page.id,
        extraction_run_id=rows.run.id,
        raw_text='3"',
        value_numerator=3,
        value_denominator=1,
        unit="in",
        polygon=[[700, 300], [760, 300], [760, 330], [700, 330]],
        coordinate_space="image",
        ambiguity_flags=["synthetic-label"],
    )
    session.add(labelled)
    session.flush()
    confirmed = confirm_candidate_type(
        session, candidate_id=labelled.id, semantic_type=CT007, confirmed_by=PERSON
    )
    assert isinstance(confirmed, CanonicalObservation), confirmed

    off = rows.check(on=False)
    on = rows.check(on=True)

    (before,) = off["CT-SINK-OFFSET-FRONT-001"]
    (after,) = on["CT-SINK-OFFSET-FRONT-001"]
    # Per check: the two that read the front offset keep the package's result; the three that
    # read nothing a person gave are the row's own, so the countertop still has one of each.
    assert set(_row_results(on, rows.anchor)) == DEPTH_RULE_IDS - {
        "CT-SINK-OFFSET-FRONT-001",
        "CT-BACK-OFFSET-MIN-001",
    }
    assert all(len(on[rule_id]) == 1 for rule_id in DEPTH_RULE_IDS)
    assert after.scope_row_candidate_id is None and after.scope_item_id is None
    assert (after.outcome, after.reason) == (before.outcome, before.reason)
    assert NOT_PER_ROW.format(why="a value was given for the whole package, which stands") in (
        after.notes or []
    )


def test_a_confirmed_countertop_with_no_run_is_still_checked_on_its_row(session: Session) -> None:
    from app.models import PartProposal

    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())
    confirm_part(
        session,
        proposal=session.get_one(PartProposal, rows.proposal),
        kind=PartKind.COUNTERTOP,
        code=None,
        actor=PERSON,
    )

    own = _row_results(rows.check(), rows.anchor)

    assert own["CT-SINK-OFFSET-FRONT-001"].outcome == Outcome.PASS.value


def test_the_reader_s_placement_rule_is_the_same() -> None:
    from evidence.coordinates import StoredPoint
    from workflow.depth_reader import VIEW_IN_ANOTHER as READERS_ANOTHER
    from workflow.depth_reader import VIEW_IN_DRAWING as READERS_DRAWING
    from workflow.depth_reader import drawings_holding

    assert (VIEW_IN_DRAWING, VIEW_IN_ANOTHER) == (READERS_DRAWING, READERS_ANOTHER)
    one, two = uuid4(), uuid4()
    d = Decimal
    drawings = [
        (one, (d("0"), d("0"), d("0.5"), d("1"))),
        (two, (d("0.5"), d("0"), d("1"), d("1"))),
    ]
    boxes = [
        (d("0.1"), d("0.1"), d("0.4"), d("0.4")),
        (d("0.1"), d("0.1"), d("0.54"), d("0.4")),
        (d("0.1"), d("0.1"), d("0.6"), d("0.4")),
        (d("0.45"), d("0.1"), d("0.9"), d("0.4")),
        (d("0.2"), d("0.2"), d("0.2"), d("0.4")),
    ]
    for box in boxes:
        left, top, right, bottom = box
        points = [StoredPoint(x=x, y=y) for x, y in ((left, top), (right, bottom))]
        assert view_in_drawing(drawings, box) == drawings_holding(drawings, points), box
    assert view_in_drawing([], boxes[0]) is None


# -- Confirm on the row's own result, and the confirm path's re-check ------------------------------


def _waiting_row(
    session: Session, store: LocalStore
) -> tuple[_Rows, ObservationCandidate, Finding]:
    rows = _Rows(session)
    row = rows.reading(CT007, 4, CONFIRM_ONLY, link=rows.link())
    _crop(session, store, row)
    own = _row_results(rows.check(), rows.anchor)
    return rows, row, own["CT-SINK-OFFSET-FRONT-001"]


def _lines(session: Session, finding: Finding) -> list[Any]:
    from app.models import CheckRun, RuleSnapshot

    snapshot_id = session.scalar(
        select(RuleSnapshot.id)
        .join(CheckRun, CheckRun.rule_snapshot_id == RuleSnapshot.id)
        .where(CheckRun.id == finding.check_run_id)
    )
    assert snapshot_id is not None
    item = _DepthItem(
        finding.id,
        finding.package_revision_id,
        "CT-SINK-OFFSET-FRONT-001",
        snapshot_id,
        Outcome(finding.outcome),
        finding.scope_item_id,
        finding.scope_row_candidate_id,
    )
    return depth_readings_for(session, [item]).get(finding.id, [])


def _confirm(session: Session, rows: _Rows, finding: Finding, row: ObservationCandidate) -> None:
    sitting = open_session(session, package_revision_id=rows.revision.id, reviewer=PERSON)
    confirm_candidate_reading(
        session,
        principal=Principal(id=PERSON, roles=frozenset({Role.REVIEWER})),
        review_session_id=sitting.id,
        finding_id=finding.id,
        candidate_id=row.id,
        expected_value=Fraction(4),
    )


def test_confirm_is_offered_on_the_row_s_own_result_and_the_value_then_decides(
    session: Session, store: LocalStore
) -> None:
    rows, row, front = _waiting_row(session, store)

    (line,) = _lines(session, front)
    assert line.state == "to_confirm" and line.can_confirm is True

    _confirm(session, rows, front, row)
    again = _row_results(rows.check(), rows.anchor)["CT-SINK-OFFSET-FRONT-001"]

    assert again.outcome == Outcome.PASS.value
    assert any("confirmed by a person" in note for note in again.notes or ())
    person = session.scalars(
        select(CanonicalObservation).where(CanonicalObservation.status == "HUMAN_CONFIRMED")
    ).all()
    assert len(person) == 1


def test_the_confirm_path_refuses_once_the_drawing_holds_a_second_countertop(
    session: Session, store: LocalStore
) -> None:
    rows, row, front = _waiting_row(session, store)
    rows.second_countertop()

    (line,) = _lines(session, front)
    assert line.can_confirm is False
    with pytest.raises(HeldReadingRefused) as refused:
        _confirm(session, rows, front, row)
    assert refused.value.reason is HeldReadingRefusal.NOT_THIS_ITEM
    assert "no longer checked on its own" in refused.value.detail


def test_the_confirm_path_refuses_a_reading_of_another_countertop(
    session: Session, store: LocalStore
) -> None:
    rows = _Rows(session, pages=(0, 1))
    mine = rows.reading(CT007, 4, CONFIRM_ONLY, link=rows.link())
    theirs = rows.reading(CT007, 4, CONFIRM_ONLY, index=1, link=rows.link(1))
    for reading in (mine, theirs):
        _crop(session, store, reading)
    front = _row_results(rows.check(), rows.anchors[0])["CT-SINK-OFFSET-FRONT-001"]

    with pytest.raises(HeldReadingRefused) as refused:
        _confirm(session, rows, front, theirs)
    assert refused.value.reason is HeldReadingRefusal.NOT_THIS_ITEM


def test_switched_off_a_row_s_held_result_offers_no_confirm(
    session: Session, store: LocalStore
) -> None:
    """Today's placeholder (#1201) on a package with two countertops: no Confirm, and the confirm
    path refuses it as before."""
    rows = _Rows(session, pages=(0, 1))
    row = rows.reading(CT007, 4, CONFIRM_ONLY, link=rows.link())
    _crop(session, store, row)
    held = _row_results(rows.check(on=False), rows.anchor)["CT-SINK-OFFSET-FRONT-001"]

    assert row_check_proposal(held.notes) is None
    assert all(line.can_confirm is False for line in _lines(session, held))
    with pytest.raises(HeldReadingRefused) as refused:
        _confirm(session, rows, held, row)
    assert refused.value.reason is HeldReadingRefusal.NOT_THIS_ITEM


def test_depth_rows_reads_only(session: Session) -> None:
    rows = _Rows(session)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())
    session.flush()
    before = {name: len(session.scalars(select(model)).all()) for name, model in _MODELS.items()}

    found = depth_rows(session, rows.revision.id)

    assert [row.row_id for row in found.rows] == [rows.anchor]
    assert not session.new and not session.dirty
    after = {name: len(session.scalars(select(model)).all()) for name, model in _MODELS.items()}
    assert after == before


_MODELS = {"items": DrawingItem, "links": ItemViewLink, "readings": ObservationCandidate}


# -- the record layer: a depth value supports a row's result only through the row's own suggestion -


def test_the_record_layer_refuses_a_depth_value_of_another_suggestion(session: Session) -> None:
    from app.models import RuleSnapshot
    from app.verdicts.record import EvidenceMissing, record_finding
    from app.verdicts.rulebook import from_row
    from verdict.finding import Finding as Decided
    from workflow.depth_evidence import suggestion_depth_operands
    from workflow.review import ENGINE_VERSION

    rows = _Rows(session)
    _publish_rulebook_once(session)
    rows.second_countertop()
    from app.models import PartProposal

    theirs = session.scalars(select(PartProposal).where(PartProposal.source == "reviewer")).one()
    rows.reading(CT007, 4, QUALIFIED, link=rows.link(proposal=theirs.id))
    snapshot = next(
        from_row(row)
        for row in session.scalars(select(RuleSnapshot))
        if from_row(row).rule.id == "CT-SINK-OFFSET-FRONT-001"
    )
    found = suggestion_depth_operands(
        session, rows.revision.id, snapshot.rule, theirs.id, rows.built.views[0].id
    )
    assert found.operands, found
    decided = Decided(
        rule_id=snapshot.rule.id,
        outcome=Outcome.REVIEW_REQUIRED,
        severity=snapshot.rule.severity,
        reason="synthetic",
        snapshot_id=snapshot.snapshot_id,
        engine_version=ENGINE_VERSION,
    )

    for suggestion in (rows.proposal, theirs.id):
        with pytest.raises(EvidenceMissing), session.begin_nested():
            record_finding(
                session,
                package_revision_id=rows.revision.id,
                finding=decided,
                operands=found.operands,
                parameter_set_ids={},
                scope_row_candidate_id=rows.anchor,
                scope_label="Countertop row on page 1",
                row_depth_suggestion=suggestion,
            )
    # And a row's own depth check names a row.
    with pytest.raises(EvidenceMissing, match="one countertop row"):
        record_finding(
            session,
            package_revision_id=rows.revision.id,
            finding=decided,
            operands={},
            parameter_set_ids={},
            row_depth_suggestion=rows.proposal,
        )


# -- the simple review screen, end to end ----------------------------------------------------------


def test_the_simple_screen_offers_confirm_on_the_row_s_own_result_and_records_it(
    session: Session, store: LocalStore
) -> None:
    from tests.api.test_depth_readings import _base, _client

    rows, row, front = _waiting_row(session, store)
    session.commit()
    client = _client(session, store, rows)  # type: ignore[arg-type]

    response = client.get(f"{_base(rows)}/review-items")  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    (item,) = [
        entry
        for entry in response.json()["items"]
        if entry["kind"] == "depth_reading" and entry["finding_id"] == str(front.id)
    ]
    assert item["actions"] == ["confirm"]
    assert item["candidate_id"] == str(row.id)

    answered = client.post(
        f"{_base(rows)}/review-items/{item['key']}/confirm",  # type: ignore[arg-type]
        json={"expected_finding_id": item["finding_id"], "shown_value": item["best_guess"]["text"]},
    )

    assert answered.status_code < 300, answered.text
    person = session.scalars(
        select(CanonicalObservation).where(CanonicalObservation.status == "HUMAN_CONFIRMED")
    ).all()
    assert len(person) == 1 and person[0].semantic_type == CT007


# -- several countertops: a row's confirmed value is the row's, never a package hold ---------------


def test_a_row_s_confirmed_value_leaves_no_package_hold_behind(
    session: Session, store: LocalStore
) -> None:
    rows = _Rows(session, pages=(0, 1))
    row = rows.reading(CT007, 4, CONFIRM_ONLY, link=rows.link())
    _crop(session, store, row)
    front = _row_results(rows.check(), rows.anchors[0])["CT-SINK-OFFSET-FRONT-001"]
    _confirm(session, rows, front, row)

    findings = rows.check()

    assert _row_results(findings, rows.anchors[0])["CT-SINK-OFFSET-FRONT-001"].outcome == (
        Outcome.PASS.value
    )
    for rule_id in ("CT-SINK-OFFSET-FRONT-001", "CT-BACK-OFFSET-MIN-001"):
        assert all(f.scope_row_candidate_id is not None for f in findings[rule_id]), rule_id


def test_a_confirmed_value_of_a_row_not_checked_on_its_own_keeps_the_package_hold(
    session: Session, store: LocalStore
) -> None:
    from app.evidence.confirm import confirm_candidate_type

    rows = _Rows(session, pages=(0, 1))
    row = rows.reading(CT007, 4, CONFIRM_ONLY, index=1, link=rows.link(1))
    rows.second_countertop(1)
    confirmed = confirm_candidate_type(
        session, candidate_id=row.id, semantic_type=CT007, confirmed_by=PERSON
    )
    assert isinstance(confirmed, CanonicalObservation), confirmed

    on = rows.check(on=True)
    off = rows.check(on=False)

    package = [f for f in on["CT-SINK-OFFSET-FRONT-001"] if f.scope_row_candidate_id is None]
    before = [f for f in off["CT-SINK-OFFSET-FRONT-001"] if f.scope_row_candidate_id is None]
    assert len(package) == len(before) == 1
    assert package[0].outcome == Outcome.REVIEW_REQUIRED.value
    assert package[0].reason == before[0].reason


def test_a_split_page_keeps_its_held_result_with_why(session: Session) -> None:
    """A page whose readers did not agree on a row has no row and no suggestion: its held result
    stays, saying why it is not checked on its own; the qualifying row beside it is checked."""
    from workflow.slot_row_scope import latest_slot_reader_run

    rows = _Rows(session)
    run = session.get_one(ExtractionRun, latest_slot_reader_run(session, rows.revision.id))
    rows.built.split_page(run, page=1)
    rows.reading(CT007, 4, QUALIFIED, link=rows.link())

    findings = rows.check()

    split = [
        f
        for f in findings["CT-DEPTH-001"]
        if f.scope_row_candidate_id is not None and f.scope_row_candidate_id != rows.anchor
    ]
    assert len(split) == 1
    assert split[0].outcome == Outcome.REVIEW_REQUIRED.value
    assert "no countertop row was read on this page" in (split[0].reason or "")
    assert row_check_proposal(_row_results(findings, rows.anchor)["CT-DEPTH-001"].notes) == (
        rows.proposal
    )
