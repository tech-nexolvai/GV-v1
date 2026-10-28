"""One countertop's depth and sink operands, from the depth reader's readings tied to it (#1290).

Verification for `workflow/depth_evidence.py`, its dispatch in `workflow/part_operands.py`, and the
five checks scoped per countertop in `DatabaseStages.run_checks` behind `GV_DEPTH_READER`.

**The bar.** Only a qualified reading (both AI readers agree AND code confirmed the number by the
printed text or a chain, both answers on record) fills a check. Everything else is held with a plain
reason on the countertop: a drawn-length-only reading waits for a person ("Confirm this reading"),
two countertops on one drawing, a view not tied to exactly one countertop, two different values for
one countertop, code's own hold, nothing read. A person's confirmation always wins. Switched off,
every finding is exactly as before.

Every value here is made up.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.evidence.confirm import confirm_candidate_type
from app.models import (
    CanonicalObservation,
    CheckRun,
    DrawingView,
    EvidenceCorroborationLane,
    ItemViewLink,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    RuleSnapshot,
    ViewRole,
)
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from app.models.verdicts import Finding
from app.verdicts.rulebook import from_row
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_identifier_pairing import _rules
from tests.workflow.test_one_drawing_per_check import _drawing, _revision
from tests.workflow.test_reading_parts import REGION, _confirmed
from tests.workflow.test_stages import _publish_rulebook
from verdict.operands import EvidenceStatus
from verdict.outcomes import Outcome
from vocabulary.part_kinds import PartKind
from workflow.countertop_runs import withdraw_countertop_run
from workflow.depth_evidence import (
    CLAUDE_PAIR,
    CONFIRM_ONLY,
    DEPTH_EXTRACTOR,
    DEPTH_READER_FLAG,
    DEPTH_READER_VERSION,
    DEPTH_RULE_IDS,
    DEPTH_SPAN_PROMPT_ID,
    HELD,
    QUALIFIED,
    SUPPORT_FLAG,
    depth_operands,
)
from workflow.evidence_operands import evidence_operands
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

ACTOR = "reviewer@example.com"
CT010, CT012, CT008, CT007 = "CT010", "CT012", "CT008", "CT007"


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


def _rule(rule_id: str):  # type: ignore[no-untyped-def]
    (found,) = [rule for rule in _rules() if rule.id == rule_id]
    return found


class _Set:
    """One vendor drawing on one page, one countertop a person confirmed, and a depth-reader run."""

    def __init__(self, session: Session, store: LocalStore) -> None:
        self.session = session
        self.revision: PackageRevision = _revision(session, store, _drawing("synthetic"))
        version = session.scalars(
            select(PackageRevisionDocument).where(
                PackageRevisionDocument.package_revision_id == self.revision.id
            )
        ).one()
        self.page = Page(
            document_version_id=version.document_version_id,
            index=0,
            content_hash="d" * 64,
            width_pt=400,
            height_pt=100,
            rotation=0,
            has_vector_text=True,
            render_failed=False,
            media_box=["0", "0", "400", "100"],
            crop_box=["0", "0", "400", "100"],
        )
        session.add(self.page)
        session.flush()
        self.view = DrawingView(page_id=self.page.id, tag="SYNTHETIC", region=REGION)
        session.add(self.view)
        session.flush()
        confirm_view_role(session, view=self.view, role=ViewRole.SHOP, actor=ACTOR)
        self.proposal, self.item = _confirmed(
            session, self.view, PartKind.COUNTERTOP, "0.20", "0.80"
        )
        workflow = WorkflowRun(package_revision_id=self.revision.id, engine_run_id="synthetic")
        session.add(workflow)
        session.flush()
        task = TaskRun(
            workflow_run_id=workflow.id,
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
            dpi=300,
        )
        session.add(self.run)
        session.flush()
        self.views = 0

    def link(self, proposal: UUID | None = None, kind: str = "plan") -> ItemViewLink:
        self.views += 1
        link = ItemViewLink(
            part_proposal_id=proposal or self.proposal,
            page_id=self.page.id,
            region={**REGION, "points": [[str(self.views), "0"]]},
            view_kind=kind,
            heading_text="PLAN VIEW",
            judged_by="code+ais",
            extraction_run_id=self.run.id,
            linked_by="code:depth-reader/v1",
        )
        self.session.add(link)
        self.session.flush()
        return link

    def reading(
        self,
        field: str,
        inches: Fraction | int | None,
        state: str,
        *,
        link: ItemViewLink | None = None,
        unplaced: tuple[UUID, ...] = (),
        text: str | None = None,
        reason: str = "both readers read it; code agrees",
        confirmed_by: str = "vector-text",
        answers: bool = True,
        supports: bool = True,
        kind: str = "plan",
        view: str | None = None,
        run: ExtractionRun | None = None,
        agreed: Fraction | int | None = None,
    ) -> ObservationCandidate:
        """One value the reader wrote, with both readers' answers on record when `answers`.

        `view` names the printed dimension (the same on a re-read); `run` the reading it is in."""
        run = run or self.run
        value = None if inches is None else Fraction(inches)
        text = text if text is not None else (f'{inches}"' if inches is not None else "")
        view = view or f"p0:v1.{uuid4().int % 100000}"
        question = f"{view}:n1"
        flags = [DEPTH_READER_FLAG, state, f"field:{field}", f"view-kind:{kind}"]
        flags += [f"question:{question}", f"view:{view}", "span:1"]
        if agreed is not None:
            flags.append(f"agreed:{Fraction(agreed)}")
        if state != HELD:
            flags.append(f"confirmed-by:{confirmed_by}")
        if link is not None:
            flags += [f"item:{link.part_proposal_id}", f"link:{link.id}"]
        flags += [f"unplaced-among:{proposal}" for proposal in unplaced]
        row = ObservationCandidate(
            document_version_id=self.page.document_version_id,
            page_id=self.page.id,
            extraction_run_id=run.id,
            raw_text=text,
            value_numerator=None if value is None or state == HELD else value.numerator,
            value_denominator=None if value is None or state == HELD else value.denominator,
            unit=None if value is None or state == HELD else "in",
            semantic_guess=field,
            polygon=[[700, 150], [800, 150], [800, 200], [700, 200]],
            coordinate_space="image",
            ambiguity_flags=flags,
            review_reason=reason,
            corroboration_status="CORROBORATED" if state == QUALIFIED else None,
            corroboration_lane="SECOND_READER" if state == QUALIFIED else None,
        )
        self.session.add(row)
        self.session.flush()
        if state == QUALIFIED and supports:
            for model in CLAUDE_PAIR:
                self.session.add(
                    ObservationCandidate(
                        document_version_id=row.document_version_id,
                        page_id=row.page_id,
                        extraction_run_id=row.extraction_run_id,
                        raw_text=text,
                        value_numerator=row.value_numerator,
                        value_denominator=row.value_denominator,
                        unit=row.unit,
                        polygon=row.polygon,
                        coordinate_space="image",
                        ambiguity_flags=[SUPPORT_FLAG, f"supports:{row.id}", f"reader-id:{model}"],
                    )
                )
            self.session.flush()
        if answers:
            for model in CLAUDE_PAIR:
                self.answer(question, model, text, run=run)
        return row

    def rerun(self) -> ExtractionRun:
        """A newer depth reading of the same set (a re-read)."""
        old = self.run
        task = self.session.get_one(TaskRun, old.task_run_id)
        newer = TaskRun(
            workflow_run_id=task.workflow_run_id,
            idempotency_key=f"synthetic-{uuid4()}",
            task_type="match",
            attempt=1,
            outcome="SUCCEEDED",
        )
        self.session.add(newer)
        self.session.flush()
        run = ExtractionRun(
            task_run_id=newer.id,
            extractor=DEPTH_EXTRACTOR,
            extractor_version=DEPTH_READER_VERSION,
            config_hash="synthetic",
            dpi=300,
        )
        self.session.add(run)
        self.session.flush()
        self.run = run
        return run

    def answer(
        self, question: str, model: str, text: str, run: ExtractionRun | None = None
    ) -> None:
        body: dict[str, object] = {
            "reader": "depth-reader",
            "document_version_id": str(self.page.document_version_id),
            "page_index": 0,
            "question_id": question,
            "prompt_id": DEPTH_SPAN_PROMPT_ID,
        }
        body["packet_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.session.add(
            ModelInvocation(
                extraction_run_id=(run or self.run).id,
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
        self.session.flush()

    def operands(self, rule_id: str = "CT-DEPTH-001"):  # type: ignore[no-untyped-def]
        return depth_operands(self.session, self.revision.id, _rule(rule_id), self.item)

    def check(self, store: LocalStore, *, on: bool) -> dict[str, list[Finding]]:
        """Run the checks with a person's run decision on the countertop, so it is a check scope."""
        _publish_rulebook(self.session)
        withdraw_countertop_run(self.session, countertop_item_id=self.item, actor=ACTOR)
        DatabaseStages(
            store, discriminators={"wall_config": "back_left_right"}, depth_reader=on
        ).run_checks(self.session, self.revision.id)
        return _findings(self.session, self.revision)


def _findings(session: Session, revision: PackageRevision) -> dict[str, list[Finding]]:
    found: dict[str, list[Finding]] = {}
    for finding, snapshot in session.execute(
        select(Finding, RuleSnapshot)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(Finding.package_revision_id == revision.id, CheckRun.superseded_at.is_(None))
    ).all():
        found.setdefault(from_row(snapshot).rule.id, []).append(finding)
    return found


# -- the constants this module writes out, since it may not import extraction code ---------------


def test_the_pair_and_the_number_question_are_the_readers_own() -> None:
    from extraction.depth.prompts import DEPTH_SPAN_PROMPT_ID as READERS_ID
    from extraction.depth.read import CLAUDE_PAIR as READERS_PAIR

    assert CLAUDE_PAIR == READERS_PAIR
    assert DEPTH_SPAN_PROMPT_ID == READERS_ID


# -- what fills a check ----------------------------------------------------------------------------


def test_a_qualified_reading_tied_to_the_countertop_fills_its_check(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())

    found = sheet.operands()

    assert found.missing is None and found.ambiguous == {}
    operand = found.operands["countertop_depth"]
    assert operand.value.exact == 24  # type: ignore[union-attr]
    assert operand.status is EvidenceStatus.CORROBORATED
    canonical = session.get(CanonicalObservation, UUID(str(operand.evidence_observation_id)))
    assert canonical is not None and canonical.semantic_type == CT010
    lanes = session.scalars(
        select(EvidenceCorroborationLane.lane).where(
            EvidenceCorroborationLane.canonical_observation_id == canonical.id
        )
    ).all()
    assert lanes == ["SECOND_READER"]
    again = sheet.operands()
    assert again.operands["countertop_depth"].evidence_observation_id == str(canonical.id)


def test_a_chain_confirmed_reading_qualifies_too(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 30, QUALIFIED, link=sheet.link(), confirmed_by="chain")

    assert sheet.operands().operands["countertop_depth"].value.exact == 30  # type: ignore[union-attr]


# -- every hold, with its reason -------------------------------------------------------------------


def test_drawn_length_alone_waits_for_a_person_to_confirm(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only")

    found = sheet.operands()

    assert found.operands == {}
    assert found.ambiguous["countertop_depth"].startswith(
        'Confirm this reading: countertop depth 24"'
    )


def test_code_s_hold_is_the_countertop_s_reason(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, None, HELD, link=sheet.link(), reason="the label is a stacked fraction")

    found = sheet.operands()

    assert found.operands == {} and found.ambiguous == {}
    assert found.missing is not None and "the label is a stacked fraction" in found.missing


def test_two_countertops_on_the_drawing_withhold_its_readings(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    _confirmed(session, sheet.view, PartKind.COUNTERTOP, "0.10", "0.15")

    found = sheet.operands()

    assert found.operands == {}
    assert "holds 2 countertops" in found.ambiguous["countertop_depth"]


def test_a_view_not_tied_to_one_countertop_is_withheld(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(
        CT010,
        24,
        CONFIRM_ONLY,
        unplaced=(sheet.proposal, uuid4()),
        reason="the drawing holding the view holds 2 countertops.",
    )

    found = sheet.operands()

    assert found.operands == {}
    assert "could not be tied to one countertop" in found.ambiguous["countertop_depth"]


def test_two_different_depths_for_one_countertop_hold(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    sheet.reading(CT010, 28, QUALIFIED, link=sheet.link(kind="section"), kind="section")

    found = sheet.operands()

    assert found.operands == {}
    assert '24" and 28"' in found.ambiguous["countertop_depth"]


def test_a_drawn_length_value_that_differs_also_holds_the_qualified_one(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    sheet.reading(CT010, 22, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only")

    assert sheet.operands().operands == {}


def test_another_countertop_s_reading_never_fills_this_one(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    other, _item = _confirmed(session, sheet.view, PartKind.COUNTERTOP, "0.10", "0.15")
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link(other))
    # A reading naming this countertop on a link that names the other one does not count either.
    forged = sheet.reading(CT010, 26, QUALIFIED, link=sheet.link(other))
    forged_flags = [flag for flag in forged.ambiguity_flags if not flag.startswith("item:")] + [
        f"item:{sheet.proposal}"
    ]
    session.add(
        ObservationCandidate(
            document_version_id=forged.document_version_id,
            page_id=forged.page_id,
            extraction_run_id=forged.extraction_run_id,
            raw_text=forged.raw_text,
            value_numerator=26,
            value_denominator=1,
            unit="in",
            semantic_guess=CT010,
            polygon=forged.polygon,
            coordinate_space="image",
            ambiguity_flags=forged_flags,
            corroboration_status="CORROBORATED",
            corroboration_lane="SECOND_READER",
        )
    )
    session.flush()

    found = depth_operands(session, sheet.revision.id, _rule("CT-DEPTH-001"), sheet.item)

    assert "countertop_depth" not in found.operands


def test_a_reading_whose_answers_are_not_on_record_is_not_used(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link(), answers=False)

    found = sheet.operands()

    assert found.operands == {}
    assert found.missing is not None and "not on record" in found.missing


def test_a_reading_without_both_readers_own_rows_is_not_used(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link(), supports=False)

    assert sheet.operands().operands == {}


def test_one_reader_s_answer_on_record_is_not_enough(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    row = sheet.reading(CT010, 24, QUALIFIED, link=sheet.link(), answers=False)
    question = next(f for f in row.ambiguity_flags if f.startswith("question:"))
    sheet.answer(question.removeprefix("question:"), CLAUDE_PAIR[0], '24"')
    sheet.answer(question.removeprefix("question:"), CLAUDE_PAIR[0], '24"')

    assert sheet.operands().operands == {}


def test_nothing_read_says_so(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)

    found = sheet.operands()

    assert found.operands == {}
    assert found.missing is not None and "No countertop depth was read" in found.missing


def test_a_set_the_reader_never_read_says_so(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    session.delete(sheet.run)
    session.flush()

    found = sheet.operands()

    assert found.missing is not None and "has not read this drawing set" in found.missing


# -- a person always wins --------------------------------------------------------------------------


def test_a_person_s_confirmation_wins(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    link = sheet.link()
    confirmed = sheet.reading(CT010, 25, CONFIRM_ONLY, link=link, confirmed_by="drawn-length-only")
    result = confirm_candidate_type(
        session, candidate_id=confirmed.id, semantic_type=CT010, confirmed_by=ACTOR
    )
    assert isinstance(result, CanonicalObservation)

    found = sheet.operands()

    operand = found.operands["countertop_depth"]
    assert operand.status is EvidenceStatus.HUMAN_CONFIRMED and operand.value.exact == 25  # type: ignore[union-attr]


def test_a_reading_a_person_typed_as_something_else_no_longer_counts(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    row = sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    confirm_candidate_type(session, candidate_id=row.id, semantic_type=CT012, confirmed_by=ACTOR)

    assert sheet.operands().operands == {}


# -- the back offset reads its three values from this countertop's views only ---------------------


def test_the_back_offset_takes_its_three_values_from_one_countertop(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    other, _item = _confirmed(session, sheet.view, PartKind.COUNTERTOP, "0.10", "0.15")
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    sheet.reading(CT007, 4, QUALIFIED, link=sheet.link())
    sheet.reading(CT008, 12, QUALIFIED, link=sheet.link(other))

    # Two countertops on the drawing now: everything is held, never mixed.
    found = sheet.operands("CT-BACK-OFFSET-MIN-001")
    assert found.operands == {}


# -- through the check stage -----------------------------------------------------------------------


def test_switched_off_the_five_checks_stay_revision_wide(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT007, 4, QUALIFIED, link=sheet.link())

    findings = sheet.check(store, on=False)

    for rule_id in DEPTH_RULE_IDS:
        (finding,) = findings[rule_id]
        assert finding.scope_item_id is None
        assert finding.outcome == Outcome.NOT_FOUND.value


def test_switched_on_each_countertop_gets_its_own_result_with_a_reason(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT007, 4, QUALIFIED, link=sheet.link())
    sheet.reading(CT010, 24, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only")

    findings = sheet.check(store, on=True)

    for rule_id in DEPTH_RULE_IDS:
        (finding,) = findings[rule_id]
        assert finding.scope_item_id == sheet.item, rule_id
        assert finding.reason, rule_id
    # The front offset has its company default (4"): a qualified 4" decides it.
    assert findings["CT-SINK-OFFSET-FRONT-001"][0].outcome == Outcome.PASS.value
    # A depth read by drawn length alone never decides: a person confirms it first.
    depth = findings["CT-DEPTH-001"][0]
    assert depth.outcome == Outcome.REVIEW_REQUIRED.value
    assert "Confirm this reading" in (depth.reason or "")
    assert findings["CT-SINK-CUTOUT-WIDTH-001"][0].outcome == Outcome.NOT_FOUND.value


def test_a_drawn_length_front_offset_never_passes(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT007, 4, CONFIRM_ONLY, link=sheet.link(), confirmed_by="drawn-length-only")

    findings = sheet.check(store, on=True)

    assert findings["CT-SINK-OFFSET-FRONT-001"][0].outcome == Outcome.REVIEW_REQUIRED.value


def test_the_scoped_evidence_call_owns_every_depth_input(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())

    scoped = evidence_operands(
        session, sheet.revision.id, [_rule("CT-DEPTH-001")], scope_item_id=sheet.item
    )
    unscoped = evidence_operands(session, sheet.revision.id, [_rule("CT-DEPTH-001")])

    assert scoped.owned["CT-DEPTH-001"] == frozenset({"countertop_depth"})
    assert scoped.operands["CT-DEPTH-001"]["countertop_depth"].value.exact == 24  # type: ignore[union-attr]
    assert "countertop_depth" not in unscoped.operands.get("CT-DEPTH-001", {})


# -- review of #1294: a person's decision survives a re-read, conflicts hold ----------------------


def test_a_person_s_retype_survives_a_re_read_of_the_same_value(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    link = sheet.link()
    first = sheet.reading(CT010, 24, QUALIFIED, link=link, view="p0:v1.1")
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT012, confirmed_by=ACTOR)
    sheet.rerun()
    sheet.reading(CT010, 24, QUALIFIED, link=link, view="p0:v1.1")

    found = sheet.operands()

    assert (
        found.operands == {}
    ), "the person said this 24 is not the depth; a re-read cannot undo it"


def test_a_re_read_that_contradicts_a_person_s_retype_holds(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    link = sheet.link()
    first = sheet.reading(CT010, 24, QUALIFIED, link=link, view="p0:v1.1")
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT012, confirmed_by=ACTOR)
    sheet.rerun()
    sheet.reading(CT010, 25, QUALIFIED, link=link, view="p0:v1.1")

    found = sheet.operands()

    assert found.operands == {}
    assert "A person said earlier" in found.ambiguous["countertop_depth"]


def test_a_person_s_confirmation_carries_over_a_re_read(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    link = sheet.link()
    first = sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=link, view="p0:v1.1", confirmed_by="drawn-length-only"
    )
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT010, confirmed_by=ACTOR)
    sheet.rerun()
    sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=link, view="p0:v1.1", confirmed_by="drawn-length-only"
    )

    operand = sheet.operands().operands["countertop_depth"]

    assert operand.status is EvidenceStatus.HUMAN_CONFIRMED and operand.value.exact == 24  # type: ignore[union-attr]


def test_a_re_read_that_contradicts_a_person_s_confirmation_holds(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    link = sheet.link()
    first = sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=link, view="p0:v1.1", confirmed_by="drawn-length-only"
    )
    confirm_candidate_type(session, candidate_id=first.id, semantic_type=CT010, confirmed_by=ACTOR)
    sheet.rerun()
    sheet.reading(CT010, 26, QUALIFIED, link=link, view="p0:v1.1")

    found = sheet.operands()

    assert found.operands == {}
    assert 'A person confirmed 24"' in found.ambiguous["countertop_depth"]


def test_confirming_a_reader_s_own_row_confirms_the_reading(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    row = sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    child = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains([f"supports:{row.id}"])
        )
    ).first()
    assert child is not None
    confirm_candidate_type(session, candidate_id=child.id, semantic_type=CT010, confirmed_by=ACTOR)

    operand = sheet.operands().operands["countertop_depth"]

    assert operand.status is EvidenceStatus.HUMAN_CONFIRMED


def test_an_unplaced_reading_with_another_value_holds_the_tied_one(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    sheet.reading(CT010, 26, CONFIRM_ONLY, unplaced=(sheet.proposal, uuid4()))

    found = sheet.operands()

    assert found.operands == {}
    assert '24" and 26"' in found.ambiguous["countertop_depth"]


def test_a_held_reading_the_readers_agreed_differently_holds_the_tied_one(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    sheet.reading(CT010, None, HELD, link=sheet.link(), agreed=30, reason="two stones")

    assert sheet.operands().operands == {}


def test_a_failed_re_read_falls_back_to_the_earlier_good_one(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    failed = sheet.rerun()
    session.add(
        ObservationCandidate(
            document_version_id=sheet.page.document_version_id,
            page_id=sheet.page.id,
            extraction_run_id=failed.id,
            raw_text="",
            polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
            coordinate_space="image",
            ambiguity_flags=[
                DEPTH_READER_FLAG,
                "state:run-failed",
                f"unplaced-among:{sheet.proposal}",
            ],
            review_reason="the depth reading failed, so nothing was read: Boom: no reply",
        )
    )
    session.flush()

    found = sheet.operands()

    assert found.operands["countertop_depth"].value.exact == 24  # type: ignore[union-attr]
    assert any("failed" in note and "Boom" in note for note in found.notes), "and why"


def test_a_reader_that_only_failed_says_so_on_the_countertop(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    session.add(
        ObservationCandidate(
            document_version_id=sheet.page.document_version_id,
            page_id=sheet.page.id,
            extraction_run_id=sheet.run.id,
            raw_text="",
            polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
            coordinate_space="image",
            ambiguity_flags=[
                DEPTH_READER_FLAG,
                "state:run-failed",
                f"unplaced-among:{sheet.proposal}",
            ],
            review_reason="the depth reading failed, so nothing was read: Boom: no reply",
        )
    )
    session.flush()

    found = sheet.operands()

    assert found.missing is not None and "failed" in found.missing and "Boom" in found.missing


def test_a_person_s_correction_of_a_link_moves_the_reading_to_the_other_countertop(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    link = sheet.link()
    sheet.reading(CT010, 24, QUALIFIED, link=link)
    second = DrawingView(page_id=sheet.page.id, tag="SECOND", region=REGION)
    session.add(second)
    session.flush()
    confirm_view_role(session, view=second, role=ViewRole.SHOP, actor=ACTOR)
    other_proposal, other_item = _confirmed(session, second, PartKind.COUNTERTOP, "0.20", "0.80")
    assert sheet.operands().operands, "before the correction the reading is this countertop's"
    session.add(
        ItemViewLink(
            part_proposal_id=other_proposal,
            page_id=sheet.page.id,
            region=link.region,
            view_kind="plan",
            judged_by="person",
            supersedes_id=link.id,
            linked_by=ACTOR,
        )
    )
    session.flush()

    assert sheet.operands().operands == {}
    moved = depth_operands(session, sheet.revision.id, _rule("CT-DEPTH-001"), other_item)
    assert moved.operands["countertop_depth"].value.exact == 24  # type: ignore[union-attr]


# -- re-review of #1294: a person's earlier number never hides a new one ---------------------------


def _confirmed_plan(sheet: _Set, link: ItemViewLink) -> None:
    first = sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=link, view="p0:v1.1", confirmed_by="drawn-length-only"
    )
    confirm_candidate_type(
        sheet.session, candidate_id=first.id, semantic_type=CT010, confirmed_by=ACTOR
    )


def test_a_new_section_with_another_number_holds_a_person_s_earlier_depth(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    plan, section = sheet.link(), sheet.link(kind="section")
    _confirmed_plan(sheet, plan)
    sheet.rerun()
    sheet.reading(
        CT010, 24, CONFIRM_ONLY, link=plan, view="p0:v1.1", confirmed_by="drawn-length-only"
    )
    sheet.reading(CT010, 26, QUALIFIED, link=section, view="p0:v1.2", kind="section")

    found = sheet.operands()

    assert found.operands == {}
    assert '24" and 26"' in found.ambiguous["countertop_depth"]


def test_a_person_s_depth_is_not_carried_when_the_re_read_does_not_read_it_again(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    _confirmed_plan(sheet, sheet.link())
    sheet.rerun()

    found = sheet.operands()

    assert found.operands == {}
    assert "does not read that dimension" in found.ambiguous["countertop_depth"]


def test_a_person_s_depth_is_not_carried_when_the_re_read_ties_the_view_elsewhere(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    _confirmed_plan(sheet, sheet.link())
    sheet.rerun()
    sheet.reading(CT010, 26, CONFIRM_ONLY, unplaced=(uuid4(),), view="p0:v1.1")

    found = sheet.operands()

    assert found.operands == {} and "countertop_depth" in found.ambiguous


def test_a_person_s_depth_and_a_qualified_other_number_in_one_reading_hold(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    _confirmed_plan(sheet, sheet.link())
    sheet.reading(CT010, 26, QUALIFIED, link=sheet.link(kind="section"), kind="section")

    found = sheet.operands()

    assert found.operands == {}
    assert '24" and 26"' in found.ambiguous["countertop_depth"]


@pytest.mark.parametrize("tied", [True, False])
def test_a_reading_held_without_an_agreed_number_holds_the_other(
    session: Session, store: LocalStore, tied: bool
) -> None:
    sheet = _Set(session, store)
    sheet.reading(CT010, 24, QUALIFIED, link=sheet.link())
    disagreed = "the two readers' reading is held: readers differ"
    if tied:
        sheet.reading(CT010, None, HELD, link=sheet.link(kind="section"), reason=disagreed)
    else:
        sheet.reading(CT010, None, HELD, unplaced=(sheet.proposal, uuid4()), reason=disagreed)

    found = sheet.operands()

    assert found.operands == {}
    assert "readers differ" in found.ambiguous["countertop_depth"]
