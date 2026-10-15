"""Pariksha's run check: every safety rule a finished run broke is reported, and nothing else (#1220).

Each test builds a synthetic revision in the test database and runs the real check stage on it. A
seeded fault either switches one safety step off for that run (the code that would turn a PASS into
REVIEW_REQUIRED, say) or writes the broken rows a faulty run would leave, and the test asserts the run
check reports it with the agent it points to and the suggested action. A clean revision gives an empty
report, and the check never writes a row. Synthetic values only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session

import workflow.stages as stages_module
from alembic import command
from app.db.base import Base, utc_now
from app.db.session import session_factory
from app.models import (
    ArchitectPairingRecord,
    CanonicalObservation,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    NoCountertopAnswer,
    ObservationCandidate,
    PackageRevision,
    SlotRowReviewDecision,
)
from app.models.rules import RuleDefinition
from app.models.rules import RuleSnapshot as RuleSnapshotRow
from app.models.verdicts import CheckRun, Finding, VerdictInput
from eval.run_check import Action, Agent, Check, RunCheckReport, UnknownRevision, run_check
from rules.semantic_types import SemanticType
from tests.api.test_slot_rows import (
    _canonical_with_reader_support,
    _package_rows,
    _reader_support,
    _run_current_checks,
    _unwitnessed_on_first_page,
)
from tests.app.postgres_fixture import alembic_config
from workflow.architect_pairing_contract import EffectivePairing
from workflow.architect_row_plan import pairing_source_note
from workflow.nina_gate import NinaGateOutcome
from workflow.several_countertops import WHOLE_PACKAGE_RULE_IDS
from workflow.slot_row_scope import latest_slot_reader_run

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


# --- building revisions ---------------------------------------------------------------------------


def _slot_candidates(session: Session) -> list[ObservationCandidate]:
    return list(
        session.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
            )
        ).all()
    )


def _rows(
    session: Session, tmp_path: Path, *, run: bool = True, supported: bool = True, **options: Any
) -> tuple[UUID, dict[int, UUID]]:
    """Two vendor pages, each with one sealed two-piece countertop row whose widths add up and
    whose walls the reviewer chose: a clean run passes both rows. `options` override
    `_package_rows`'. `supported=False` leaves out the two readers' answers. Returns the revision
    and each page's row anchor."""
    settings: dict[str, Any] = {
        "piece_count": 2,
        "widths_add_up": True,
        "wall_source": "readers",
        "walls_confirmed": True,
    }
    settings.update(options)
    _project_id, package_id, anchors = _package_rows(session, **settings)
    if supported:
        for candidate in _slot_candidates(session):
            _reader_support(session, candidate)
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    if run:
        _run_current_checks(session, package_id, tmp_path)
    return revision.id, anchors


def _run(session: Session, revision_id: UUID, tmp_path: Path) -> None:
    package_id = session.get(PackageRevision, revision_id)
    assert package_id is not None
    _run_current_checks(session, package_id.package_id, tmp_path)


def _live(session: Session, revision_id: UUID) -> list[tuple[Finding, CheckRun, str]]:
    return [
        (finding, run, rule_id)
        for finding, run, rule_id in session.execute(
            select(Finding, CheckRun, RuleDefinition.rule_id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshotRow, RuleSnapshotRow.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshotRow.rule_definition_id)
            .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
        ).all()
    ]


def _live_for(
    session: Session, revision_id: UUID, rule_id: str, *, row: UUID | None = None
) -> tuple[Finding, CheckRun]:
    matches = [
        (finding, run)
        for finding, run, rule in _live(session, revision_id)
        if rule == rule_id and (row is None or finding.scope_row_candidate_id == row)
    ]
    if row is None and rule_id in WHOLE_PACKAGE_RULE_IDS and len(matches) > 1:
        # Since #1201 a depth or sink check gives each countertop row of a package with several
        # its own result, and no whole-package one: take the first row's, by its name.
        assert all(finding.scope_row_candidate_id is not None for finding, _run in matches)
        matches = sorted(matches, key=lambda match: match[0].scope_label or "")[:1]
    assert len(matches) == 1, (rule_id, row, len(matches))
    return matches[0]


def _supersede(session: Session, run: CheckRun) -> None:
    """What a faulty run leaves: this result is no longer live, and nothing replaced it."""
    session.execute(update(CheckRun).where(CheckRun.id == run.id).values(superseded_at=utc_now()))
    session.flush()


def _forge(
    session: Session,
    revision_id: UUID,
    *,
    replaces: CheckRun,
    outcome: str,
    reason: str | None = "synthetic forged result",
    trace: dict[str, object] | None = None,
    inputs: tuple[dict[str, object], ...] = (),
    scope_row: UUID | None = None,
    scope_label: str | None = None,
    notes: tuple[str, ...] = (),
) -> Finding:
    """Write the result a broken check stage would have written in place of `replaces`, for the
    same countertop row unless told otherwise."""
    if scope_row is None and scope_label is None:
        replaced = session.scalars(select(Finding).where(Finding.check_run_id == replaces.id)).one()
        scope_row, scope_label = replaced.scope_row_candidate_id, replaced.scope_label
    _supersede(session, replaces)
    run = CheckRun(
        package_revision_id=revision_id,
        rule_snapshot_id=replaces.rule_snapshot_id,
        engine_version="0.1.0",
    )
    session.add(run)
    session.flush()
    for item in inputs:
        session.add(
            VerdictInput(
                check_run_id=run.id,
                operand_name=item.get("name", "countertop_depth"),
                value_numerator=40,
                value_denominator=1,
                unit=item.get("unit", "in"),
                evidence_status=item.get("status", "CORROBORATED"),
                canonical_observation_id=item.get("canonical"),
            )
        )
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision_id,
        scope_row_candidate_id=scope_row,
        scope_label=scope_label,
        outcome=outcome,
        severity="CRITICAL",
        trace=(
            trace
            if trace is not None
            else {"cause": "synthetic", "reason": reason or "", "outcome": outcome}
        ),
        parameter_set_versions={},
        reason=reason,
        notes=list(notes),
    )
    session.add(finding)
    session.flush()
    return finding


def _calculation(
    outcome: str, *, unit: str | None = "in", operands: tuple[dict[str, str | None], ...] = ()
) -> dict[str, object]:
    return {
        "operation": "synthetic_equal",
        "operands": list(
            operands
            or (
                {
                    "name": "countertop_depth",
                    "value": "40 in",
                    "source": "SHOP",
                    "evidence_ref": "synthetic-ref",
                },
            )
        ),
        "intermediates": [],
        "comparison": "40 in = 40 in",
        "tolerance": None,
        "arithmetic_unit": unit,
        "outcome": outcome,
        "engine_version": "0.1.0",
        "operation_version": "1",
        "explanation": [],
    }


def _canonical(
    session: Session,
    anchor: UUID,
    *,
    status: str = "CORROBORATED",
    authority: str = "AUTHORITATIVE",
    lanes: tuple[str, ...] = ("SECOND_READER",),
) -> UUID:
    candidate = session.get(ObservationCandidate, anchor)
    assert candidate is not None
    observation = CanonicalObservation(
        document_version_id=candidate.document_version_id,
        page_id=candidate.page_id,
        document_role="SHOP",
        polygon=[["0", "0"], ["1", "0"], ["1", "1"], ["0", "1"]],
        coordinate_space="stored",
        semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH.value,
        value_numerator=40,
        value_denominator=1,
        unit="in",
        status=status,
        authority=authority,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()
    for lane in lanes:
        session.add(EvidenceCorroborationLane(canonical_observation_id=observation.id, lane=lane))
    session.flush()
    return observation.id


def _codes(report: RunCheckReport) -> set[str]:
    return {item.code for item in report.items}


def _only(report: RunCheckReport, code: str) -> Any:
    """The one item the seeded fault must give, and nothing else."""
    assert _codes(report) == {code}, [(item.code, item.found) for item in report.items]
    found = [item for item in report.items if item.code == code]
    assert len(found) == 1, [item.code for item in report.items]
    return found[0]


# --- the clean revision ---------------------------------------------------------------------------


def test_a_clean_run_gives_an_empty_report_that_says_ready(
    session: Session, tmp_path: Path
) -> None:
    """Input: two sealed rows whose widths add up, walls the reviewer chose, every other rule's
    abstention recorded with its reason. Outcome: no item, and the suggested action is `ready`."""
    revision_id, anchors = _rows(session, tmp_path)
    by_row = {
        finding.scope_row_candidate_id: finding.outcome
        for finding, _run, rule in _live(session, revision_id)
        if rule == "CT-WIDTH-001"
    }
    assert by_row == {anchors[0]: "PASS", anchors[1]: "PASS"}

    report = run_check(session, revision_id)

    assert report.items == (), [(item.code, item.found) for item in report.items]
    assert report.action == "ready"
    assert report.package_revision_id == str(revision_id)
    assert dict(report.examined)["decided findings"] == 2


def test_a_clean_run_with_a_listed_page_that_has_its_reason_is_still_clean(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,), run=False)
    _listed_record(session, revision_id, reason="synthetic: only a wall elevation is drawn")
    _run(session, revision_id, tmp_path)

    assert run_check(session, revision_id).items == ()


def test_the_report_is_json_and_names_what_could_not_be_computed(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    report = run_check(session, revision_id)

    data = json.loads(json.dumps(report.to_dict()))
    assert data["package_revision_id"] == str(revision_id)
    assert data["action"] == "ready"
    assert data["items"] == []
    wording = [entry for entry in data["not_computable"] if entry["check"] == Check.WORDING.value]
    assert len(wording) == 1 and wording[0]["reason"]


def test_an_unknown_revision_is_refused(session: Session) -> None:
    with pytest.raises(UnknownRevision):
        run_check(session, uuid4())


def test_the_run_check_never_writes(session: Session, tmp_path: Path) -> None:
    """The check reads only: every table holds exactly the rows it held before, and the session
    has nothing new, changed or deleted to flush."""
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,), run=False)
    _listed_record(session, revision_id, reason=None)
    _run(session, revision_id, tmp_path)
    session.commit()

    def counts() -> dict[str, int]:
        return {
            table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
            for table in Base.metadata.tables.values()
        }

    before = counts()
    report = run_check(session, revision_id)
    assert report.items, "this revision has a fault, so every lookup path ran"
    assert not session.new and not session.dirty and not session.deleted
    session.flush()
    assert counts() == before


def test_the_run_check_works_inside_a_read_only_transaction(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    session.commit()
    session.execute(text("SET TRANSACTION READ ONLY"))
    assert run_check(session, revision_id).items == ()
    session.rollback()


# --- (a) coverage ---------------------------------------------------------------------------------


def test_a_rule_in_scope_with_no_result_points_to_kautilya(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    # Every result of the rule: since #1201 each countertop row has its own.
    for _finding, run, rule in _live(session, revision_id):
        if rule == "CT-DEPTH-001":
            _supersede(session, run)

    item = _only(run_check(session, revision_id), "rule-without-result")
    assert item.check is Check.COVERAGE
    assert item.agent is Agent.KAUTILYA
    assert item.action is Action.RERUN
    assert item.suggested == "re-run Kautilya"
    assert ("rule", "CT-DEPTH-001") in item.ids


def test_a_rule_for_another_product_needs_no_result(session: Session, tmp_path: Path) -> None:
    """The package is for countertops, so the cabinet rules are not in scope: no item for them."""
    revision_id, _anchors = _rows(session, tmp_path)
    rules = {rule for _finding, _run, rule in _live(session, revision_id)}
    assert not any(rule.startswith("CAB-") for rule in rules)

    assert run_check(session, revision_id).items == ()


def test_a_countertop_row_with_no_width_result_points_to_nina(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    _supersede(session, run)

    item = _only(run_check(session, revision_id), "row-without-width-result")
    assert item.check is Check.COVERAGE
    assert item.agent is Agent.NINA
    assert item.action is Action.RERUN
    assert ("row", str(anchors[0])) in item.ids


#: A countertop of a package with several that lost one of its six depth and sink results (#1305).
OWN_RESULT = "countertop-without-depth-or-sink-result"


def _own_depth_and_sink(
    session: Session, revision_id: UUID, row: UUID
) -> list[tuple[Finding, CheckRun, str]]:
    """The live results of the six depth and sink checks scoped to this countertop row."""
    return [
        (finding, run, rule)
        for finding, run, rule in _live(session, revision_id)
        if rule in WHOLE_PACKAGE_RULE_IDS and finding.scope_row_candidate_id == row
    ]


def test_a_countertop_row_that_lost_its_depth_and_sink_results_points_to_nina(
    session: Session, tmp_path: Path
) -> None:
    """Two countertop rows, each with its own six depth and sink results (#1201). Seeded fault:
    the second row's six are gone, nothing replaced them, and the first row still has its own, so
    no rule is without a result. The run check names the second row, and only it (#1305)."""
    revision_id, anchors = _rows(session, tmp_path)
    assert run_check(session, revision_id).items == ()
    lost = _own_depth_and_sink(session, revision_id, anchors[1])
    assert sorted(rule for _finding, _run, rule in lost) == sorted(WHOLE_PACKAGE_RULE_IDS)
    for _finding, run, _rule in lost:
        _supersede(session, run)

    item = _only(run_check(session, revision_id), OWN_RESULT)
    assert item.check is Check.COVERAGE
    assert item.agent is Agent.NINA
    assert item.action is Action.RERUN
    assert ("row", str(anchors[1])) in item.ids and ("row", str(anchors[0])) not in item.ids
    assert {value for kind, value in item.ids if kind == "rule"} == set(WHOLE_PACKAGE_RULE_IDS)
    assert item.found.startswith(
        "Countertop row on page 2 has no live result of its own for 6 of the depth and sink "
        "checks, and no held whole-package result names it: "
    ), item.found


def test_one_lost_depth_result_is_named_for_its_own_row_and_check_only(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    ((_finding, run, _rule),) = [
        entry
        for entry in _own_depth_and_sink(session, revision_id, anchors[0])
        if entry[2] == "CT-DEPTH-001"
    ]
    _supersede(session, run)

    item = _only(run_check(session, revision_id), OWN_RESULT)
    assert ("row", str(anchors[0])) in item.ids and ("row", str(anchors[1])) not in item.ids
    assert [value for kind, value in item.ids if kind == "rule"] == ["CT-DEPTH-001"]
    assert item.found == (
        "Countertop row on page 1 has no live result of its own for one of the depth and sink "
        "checks, and no held whole-package result names it: CT-DEPTH-001. Re-run the checks."
    )


def test_one_countertop_row_needs_no_result_of_its_own(session: Session, tmp_path: Path) -> None:
    """One countertop: its depth and sink results are whole-package, as before #1201, and a lost
    one is the rule's only result, which the rule line already reports. Nothing new (#1305)."""
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,), run=False)
    _listed_record(session, revision_id, reason="synthetic: only a wall elevation is drawn")
    _run(session, revision_id, tmp_path)
    assert run_check(session, revision_id).items == ()
    finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    assert finding.scope_row_candidate_id is None and finding.scope_item_id is None
    _supersede(session, run)

    _only(run_check(session, revision_id), "rule-without-result")


def test_an_abstention_with_no_reason_is_held(session: Session, tmp_path: Path) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    forged = _forge(session, revision_id, replaces=run, outcome="NOT_FOUND", reason=None)

    item = _only(run_check(session, revision_id), "abstention-without-reason")
    assert item.check is Check.COVERAGE
    assert item.agent is Agent.NINA
    assert item.action is Action.HOLD
    assert ("finding", str(forged.id)) in item.ids


def test_a_revision_whose_checks_never_ran_asks_for_nina(session: Session, tmp_path: Path) -> None:
    revision_id, _anchors = _rows(session, tmp_path, run=False)

    report = run_check(session, revision_id)
    item = _only(report, "no-live-results")
    assert item.agent is Agent.NINA and item.action is Action.RERUN
    assert report.action == "re-run Nina"


# --- (b) two judgments ----------------------------------------------------------------------------


def _nina_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Since #1228 Nina's gate holds what this report finds at the end of the check stage; a test
    of the report on a seeded fault switches the gate off too (`tests/workflow/test_nina_gate.py`
    shows the gate holding the same faults)."""
    monkeypatch.setattr(
        stages_module,
        "hold_unsafe_decisions",
        lambda _session, _revision: NinaGateOutcome(checked=0, held=()),
    )


def test_a_pass_on_a_reading_with_no_drawn_length_witness_points_to_akshara(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeded fault: the step that turns such a PASS into REVIEW_REQUIRED is switched off."""
    monkeypatch.setattr(stages_module, "confirm_unwitnessed_pass", lambda finding, _u: finding)
    _nina_gate_off(monkeypatch)
    revision_id, anchors = _rows(session, tmp_path, slot_flags=_unwitnessed_on_first_page)
    finding, _run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    assert finding.outcome == "PASS"

    report = run_check(session, revision_id)
    item = _only(report, "pass-on-unwitnessed-reading")
    assert item.check is Check.TWO_JUDGMENTS
    assert item.agent is Agent.AKSHARA
    assert item.action is Action.HOLD
    assert ("finding", str(finding.id)) in item.ids
    assert report.action == "hold for a person"


def test_a_pass_on_values_the_reviewer_confirmed_needs_no_drawn_length_witness(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unwitnessed readings' values were confirmed by the reviewer: the PASS rests on a
    person's confirmation, not on the readers' text alone, so it is not reported."""
    monkeypatch.setattr(stages_module, "confirm_unwitnessed_pass", lambda finding, _u: finding)
    revision_id, anchors = _rows(
        session, tmp_path, run=False, supported=False, slot_flags=_unwitnessed_on_first_page
    )
    for candidate in _slot_candidates(session):
        overall = "slot:overall" in (candidate.ambiguity_flags or [])
        _canonical_with_reader_support(
            session,
            candidate,
            semantic=(
                SemanticType.COUNTERTOP_OVERALL_WIDTH
                if overall
                else SemanticType.COUNTERTOP_PIECE_WIDTH
            ),
            parent_supported=True,
        )
    _run(session, revision_id, tmp_path)
    finding, _run_row = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    assert finding.outcome == "PASS"

    assert run_check(session, revision_id).items == ()


def test_the_same_row_held_by_the_real_code_is_clean(session: Session, tmp_path: Path) -> None:
    revision_id, anchors = _rows(session, tmp_path, slot_flags=_unwitnessed_on_first_page)
    finding, _run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    assert finding.outcome == "REVIEW_REQUIRED"

    assert run_check(session, revision_id).items == ()


def test_a_pass_on_walls_set_automatically_points_to_rekha(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeded fault: the step that holds a PASS on automatic walls for one click is switched off."""
    monkeypatch.setattr(
        stages_module,
        "confirm_vendor_walls_pass",
        lambda finding, *, walls_automatic, unwitnessed=(): finding,
    )
    _nina_gate_off(monkeypatch)
    revision_id, anchors = _rows(
        session, tmp_path, wall_source="code-ink-and-both-ais", walls_confirmed=False
    )
    finding, _run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    assert finding.outcome == "PASS"

    report = run_check(session, revision_id)
    assert _codes(report) == {"pass-on-automatic-walls"}
    items = list(report.items)
    assert len(items) == 2
    assert all(item.agent is Agent.REKHA and item.action is Action.HOLD for item in items)
    assert all(item.check is Check.TWO_JUDGMENTS for item in items)
    assert all("should have waited for one click and did not" in item.found for item in items)


def _pairing(
    session: Session,
    revision_id: UUID,
    anchor_id: UUID,
    source: str,
    *,
    view: str | None = None,
    supersedes: ArchitectPairingRecord | None = None,
) -> ArchitectPairingRecord:
    anchor = session.get(ObservationCandidate, anchor_id)
    assert anchor is not None
    reviewer = source == "reviewer"
    record = ArchitectPairingRecord(
        package_revision_id=revision_id,
        page_id=anchor.page_id,
        row_anchor_candidate_id=anchor.id,
        extraction_run_id=None if reviewer else latest_slot_reader_run(session, revision_id),
        source=source,
        status="reviewer" if reviewer else "paired",
        pairs=[],
        details={} if view is None else {"view": view},
        decided_by="synthetic reviewer" if reviewer else None,
        supersedes_id=None if supersedes is None else supersedes.id,
    )
    session.add(record)
    session.flush()
    return record


def _architect_result(
    session: Session,
    revision_id: UUID,
    anchor_id: UUID,
    *,
    noted: ArchitectPairingRecord | None,
    source: str | None = None,
) -> Finding:
    """A decided architect result on one row, noting the pairing it used as the check writes it."""
    _finding, run = _live_for(session, revision_id, "CT-ARCH-WIDTH-001")
    notes: tuple[str, ...] = ()
    if noted is not None:
        notes = (
            pairing_source_note(
                EffectivePairing(
                    record_id=noted.id,
                    source=source or noted.source,  # type: ignore[arg-type]
                    status=noted.status,
                    pairs=(),
                    reasons=(),
                )
            ),
        )
    return _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        trace=_calculation("PASS"),
        inputs=({"canonical": _canonical(session, anchor_id)},),
        scope_row=anchor_id,
        scope_label="Countertop row on page 1 · architect",
        notes=notes,
    )


def test_a_decision_on_a_one_judgment_architect_pairing_points_to_milan(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    record = _pairing(session, revision_id, anchors[0], "code")
    forged = _architect_result(session, revision_id, anchors[0], noted=record)

    item = _only(run_check(session, revision_id), "decision-on-one-judgment-pairing")
    assert item.check is Check.TWO_JUDGMENTS
    assert item.agent is Agent.MILAN
    assert item.action is Action.HOLD
    assert ("finding", str(forged.id)) in item.ids
    assert "code alone" in item.found and "'" not in item.found


def test_an_older_reviewer_record_does_not_hide_a_one_judgment_pairing(
    session: Session, tmp_path: Path
) -> None:
    """Probe 1 (#1222 review): a reviewer paired the row against an earlier view, then the row was
    matched with another view and the effective pairing is both AIs alone. The result's own note
    says so: flagged, although a reviewer record exists."""
    revision_id, anchors = _rows(session, tmp_path)
    earlier = _pairing(session, revision_id, anchors[0], "reviewer", view="earlier view")
    effective = _pairing(
        session, revision_id, anchors[0], "both-ais", view="matched view", supersedes=earlier
    )
    _architect_result(session, revision_id, anchors[0], noted=effective)

    item = _only(run_check(session, revision_id), "decision-on-one-judgment-pairing")
    assert ("pairing source", "both-ais") in item.ids


def test_a_newer_record_on_a_view_no_longer_matched_does_not_raise_a_false_alarm(
    session: Session, tmp_path: Path
) -> None:
    """Probe 2 (#1222 review): the effective pairing is code and both AIs; a newer record, made on
    a view the row is no longer matched with, says code alone. The result rests on the first."""
    revision_id, anchors = _rows(session, tmp_path)
    effective = _pairing(session, revision_id, anchors[0], "code+ais", view="matched view")
    _pairing(
        session,
        revision_id,
        anchors[0],
        "code",
        view="view no longer matched",
        supersedes=effective,
    )
    _architect_result(session, revision_id, anchors[0], noted=effective)

    assert run_check(session, revision_id).items == ()


def test_a_reviewer_pairing_lets_the_architect_decision_stand(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    record = _pairing(session, revision_id, anchors[0], "reviewer")
    _architect_result(session, revision_id, anchors[0], noted=record)

    assert run_check(session, revision_id).items == ()


def test_a_decided_architect_result_with_no_pairing_note_is_held(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    forged = _architect_result(session, revision_id, anchors[0], noted=None)

    item = _only(run_check(session, revision_id), "decision-without-pairing-record")
    assert item.agent is Agent.MILAN and item.action is Action.HOLD
    assert ("finding", str(forged.id)) in item.ids


def test_a_pairing_note_that_disagrees_with_its_record_is_held(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    record = _pairing(session, revision_id, anchors[0], "code")
    _architect_result(session, revision_id, anchors[0], noted=record, source="code+ais")

    item = _only(run_check(session, revision_id), "decision-without-pairing-record")
    assert "says 'code'" in item.found


def test_a_decision_on_a_value_with_one_reading_on_record_points_to_akshara(
    session: Session, tmp_path: Path
) -> None:
    """Seeded fault: the value behind a PASS records no second reader, no reader answers and no
    drawn-length witness: the two judgments the seal requires are nowhere on record."""
    revision_id, anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    value_id = _canonical(session, anchors[0], lanes=())
    forged = _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        trace=_calculation("PASS"),
        inputs=({"canonical": value_id},),
    )

    item = _only(run_check(session, revision_id), "decision-on-one-route-value")
    assert item.check is Check.TWO_JUDGMENTS
    assert item.agent is Agent.AKSHARA
    assert item.action is Action.HOLD
    assert ("finding", str(forged.id)) in item.ids
    assert ("value", str(value_id)) in item.ids


def test_two_reader_answers_on_record_count_as_two_judgments(
    session: Session, tmp_path: Path
) -> None:
    """A stored value reused by the width check (#1090's shape) keeps its two readers' answers as
    its supporting readings rather than a lane row: that is two judgments on record."""
    revision_id, anchors = _rows(session, tmp_path, run=False, supported=False)
    for candidate in _slot_candidates(session):
        overall = "slot:overall" in (candidate.ambiguity_flags or [])
        _canonical_with_reader_support(
            session,
            candidate,
            semantic=(
                SemanticType.COUNTERTOP_OVERALL_WIDTH
                if overall
                else SemanticType.COUNTERTOP_PIECE_WIDTH
            ),
        )
    _run(session, revision_id, tmp_path)
    finding, _run_row = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    assert finding.outcome == "PASS"

    assert run_check(session, revision_id).items == ()


# --- (c) inputs -----------------------------------------------------------------------------------


def test_a_decision_with_no_recorded_inputs_points_to_nina(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    forged = _forge(session, revision_id, replaces=run, outcome="FAIL", trace=_calculation("FAIL"))

    item = _only(run_check(session, revision_id), "decision-without-inputs")
    assert item.check is Check.INPUTS
    assert item.agent is Agent.NINA
    assert item.action is Action.HOLD
    assert ("finding", str(forged.id)) in item.ids


def test_a_decision_with_no_calculation_on_record_is_held(session: Session, tmp_path: Path) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        inputs=({"canonical": _canonical(session, anchors[0])},),
    )

    item = _only(run_check(session, revision_id), "decision-without-calculation")
    assert item.check is Check.INPUTS and item.agent is Agent.NINA


# --- (d) unread pages -----------------------------------------------------------------------------


def _listed_record(session: Session, revision_id: UUID, *, reason: str | None) -> UUID:
    """Code's "no row to read" record for the second vendor page, as a broken reader could leave
    it: with or without its reason."""
    run_id = latest_slot_reader_run(session, revision_id)
    assert run_id is not None
    page = session.execute(
        text(
            "SELECT p.id, p.document_version_id FROM pages p "
            "JOIN package_revision_documents d ON d.document_version_id = p.document_version_id "
            "WHERE d.package_revision_id = :revision AND p.index = 1"
        ),
        {"revision": revision_id},
    ).one()
    record = ObservationCandidate(
        document_version_id=page.document_version_id,
        page_id=page.id,
        extraction_run_id=run_id,
        raw_text="",
        polygon=[[0, 0], [612, 0], [612, 792], [0, 792]],
        coordinate_space="image",
        ambiguity_flags=["slot-reader-row-choice", "no-row:no-row-found"],
        review_reason=reason,
    )
    session.add(record)
    session.flush()
    return record.id


def test_a_vendor_page_with_nothing_recorded_points_to_rekha(
    session: Session, tmp_path: Path
) -> None:
    """Seeded fault: the reader recorded nothing at all for the second page."""
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,))

    item = _only(run_check(session, revision_id), "silent-page")
    assert item.check is Check.UNREAD_PAGES
    assert item.agent is Agent.REKHA
    assert item.action is Action.RERUN
    assert ("page", "2") in item.ids


def test_a_listed_page_with_no_reason_and_no_answer_is_held(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,), run=False)
    record_id = _listed_record(session, revision_id, reason=None)
    _run(session, revision_id, tmp_path)

    item = _only(run_check(session, revision_id), "unread-page-without-reason")
    assert item.check is Check.UNREAD_PAGES
    assert item.agent is Agent.REKHA
    assert item.action is Action.HOLD
    assert ("record", str(record_id)) in item.ids


def test_a_reviewer_answer_accounts_for_a_listed_page_with_no_reason(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,), run=False)
    record_id = _listed_record(session, revision_id, reason=None)
    record = session.get(ObservationCandidate, record_id)
    assert record is not None
    session.add(
        NoCountertopAnswer(
            package_revision_id=revision_id,
            row_choice_candidate_id=record_id,
            page_id=record.page_id,
            page_number=2,
            page_content_hash=f"{1:064x}",
            reading_answer="no-row:no-row-found",
            answer="no_countertop",
            answered_by="synthetic reviewer",
        )
    )
    session.flush()
    _run(session, revision_id, tmp_path)

    assert run_check(session, revision_id).items == ()


def test_a_page_the_reviewer_says_has_a_countertop_needs_its_result(
    session: Session, tmp_path: Path
) -> None:
    """Answered "There is a countertop" after the last run: the page needs a result of its own."""
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,), run=False)
    record_id = _listed_record(session, revision_id, reason="synthetic: no row was found")
    _run(session, revision_id, tmp_path)
    record = session.get(ObservationCandidate, record_id)
    assert record is not None
    session.add(
        NoCountertopAnswer(
            package_revision_id=revision_id,
            row_choice_candidate_id=record_id,
            page_id=record.page_id,
            page_number=2,
            page_content_hash=f"{1:064x}",
            reading_answer="no-row:no-row-found",
            answer="countertop",
            answered_by="synthetic reviewer",
        )
    )
    session.flush()

    report = run_check(session, revision_id)
    # The answer also makes the page a second countertop (#1201), and the six depth and sink checks
    # ran once for the one countertop there was: each countertop now lacks its own result (#1305).
    assert _codes(report) == {"blocking-page-without-result", OWN_RESULT}
    (item,) = [item for item in report.items if item.code == "blocking-page-without-result"]
    assert item.check is Check.UNREAD_PAGES
    assert item.agent is Agent.NINA
    assert item.action is Action.RERUN
    own = [item for item in report.items if item.code == OWN_RESULT]
    assert [item.found.split(" has no live")[0] for item in own] == [
        "Countertop row on page 1",
        "Countertop row on page 2",
    ]
    assert all(item.action is Action.RERUN for item in own)

    _run(session, revision_id, tmp_path)
    assert run_check(session, revision_id).items == ()


# --- (f) the contract gates, on the run's own operands --------------------------------------------


def test_a_decision_on_conflicting_evidence_breaks_the_ocr_gate(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    forged = _forge(
        session,
        revision_id,
        replaces=run,
        outcome="FAIL",
        trace=_calculation("FAIL"),
        inputs=({"canonical": _canonical(session, anchors[0], status="CONFLICTING")},),
    )

    item = _only(run_check(session, revision_id), "decision-on-unqualified-evidence")
    assert item.check is Check.CONTRACT_GATES
    assert item.agent is Agent.AKSHARA
    assert item.action is Action.HOLD
    assert ("finding", str(forged.id)) in item.ids
    assert ("gate", "ocr_disagreement_never_auto_resolved") in item.ids


def test_a_decision_in_an_unknown_unit_breaks_the_unit_gate(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        trace=_calculation("PASS", unit="furlong"),
        inputs=({"canonical": _canonical(session, anchors[0])},),
    )

    item = _only(run_check(session, revision_id), "decision-in-unknown-unit")
    assert item.check is Check.CONTRACT_GATES
    assert item.agent is Agent.NINA and item.action is Action.HOLD
    assert ("gate", "unknown_unit_cannot_enter_verdict") in item.ids


def test_a_decision_with_a_value_from_no_source_breaks_the_missing_source_gate(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        trace=_calculation(
            "PASS",
            operands=(
                {"name": "countertop_depth", "value": "", "source": "SHOP", "evidence_ref": None},
            ),
        ),
        inputs=({"canonical": None},),
    )

    item = _only(run_check(session, revision_id), "decision-on-value-without-source")
    assert item.check is Check.CONTRACT_GATES
    assert item.agent is Agent.NINA and item.action is Action.HOLD
    assert ("gate", "missing_approved_source_is_not_found") in item.ids


def test_a_decision_on_advisory_evidence_breaks_the_advisory_gate(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        trace=_calculation(
            "PASS",
            operands=(
                {
                    "name": "countertop_depth",
                    "value": "40 in",
                    "source": "RETRIEVAL",
                    "evidence_ref": "synthetic-ref",
                },
            ),
        ),
        inputs=({"canonical": _canonical(session, anchors[0], authority="ADVISORY")},),
    )

    items = [item for item in run_check(session, revision_id).items]
    advisory = [item for item in items if item.code == "decision-on-advisory-value"]
    assert len(advisory) == 1
    assert advisory[0].check is Check.CONTRACT_GATES
    assert advisory[0].agent is Agent.NINA and advisory[0].action is Action.HOLD
    assert ("gate", "advisory_retrieval_never_an_operand") in advisory[0].ids


# --- other kinds of revision (#1222 review): each must be ready when nothing is wrong -------------


def _filled_by_the_reviewer(session: Session) -> UUID:
    """`tests/workflow/test_full_coverage.py`'s revision: no product stated, every value the
    reviewer types typed, list inputs (cabinets, fillers, categories) included, and every drawn
    number as its confirmed reading (#1303); every check that can decide passes."""
    from rules.parameters import ParameterLayer
    from tests.workflow.test_full_coverage import (
        CABINET_TYPES,
        DISCRIMINATORS,
        PROJECT_PARAMETERS,
        RUN_PARAMETERS,
        TYPED_MEASUREMENTS,
        _publish_rulebook,
        _revision,
        _store,
        reviewer_operands,
    )
    from workflow.classifications import record_classifications
    from workflow.stages import DatabaseStages

    revision = _revision(session)
    _publish_rulebook(session)
    _store(session, revision, ParameterLayer.PROJECT, PROJECT_PARAMETERS)
    _store(session, revision, ParameterLayer.RUN, {**RUN_PARAMETERS, **TYPED_MEASUREMENTS})
    record_classifications(
        session,
        package_revision_id=revision.id,
        rule_id="CAB-FILLER-001",
        input_name="cabinet_type",
        categories=CABINET_TYPES,
        confirmed_by="reviewer",
    )
    session.flush()
    DatabaseStages(
        operands=reviewer_operands(session, revision.id), discriminators=DISCRIMINATORS
    ).run_checks(session, revision.id)
    return revision.id


def test_a_revision_the_reviewer_filled_in_with_list_inputs_must_be_ready(
    session: Session,
) -> None:
    """List inputs are never stored one value per row (`record_finding`), so they are read from
    the calculation; the checks that cannot follow each list member say so."""
    revision_id = _filled_by_the_reviewer(session)
    outcomes = {rule: finding.outcome for finding, _run, rule in _live(session, revision_id)}
    assert outcomes["CAB-ARCH-VS-SHOP-001"] == "PASS"

    report = run_check(session, revision_id)

    assert report.items == (), [(item.code, item.found) for item in report.items]
    assert report.action == "ready"
    assert report.product_type is None
    reasons = {(entry.check, entry.reason) for entry in report.not_computable}
    assert any(
        check is Check.TWO_JUDGMENTS and "list of values" in reason for check, reason in reasons
    )
    assert any(
        check is Check.CONTRACT_GATES and "list of values" in reason for check, reason in reasons
    )


def test_a_dropped_single_value_of_a_list_rule_is_still_reported(session: Session) -> None:
    revision_id = _filled_by_the_reviewer(session)
    finding, run = _live_for(session, revision_id, "CAB-FILLER-001")
    trace = dict(finding.trace)
    forged = _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        trace=trace,
        inputs=({"name": "design_width", "status": "HUMAN_CONFIRMED"},),
    )

    item = _only(run_check(session, revision_id), "decision-without-inputs")
    assert ("finding", str(forged.id)) in item.ids
    assert ("input", "field_width") in item.ids


@pytest.mark.parametrize("product", ["cabinet", "countertop", None])
def test_a_revision_for_one_product_or_none_with_nothing_read_is_ready(
    session: Session, product: str | None
) -> None:
    from app.models import Package
    from tests.workflow.test_stages import _publish_rulebook, _revision
    from workflow.stages import DatabaseStages

    revision = _revision(session)
    package = session.get(Package, revision.package_id)
    assert package is not None
    package.product_type = product
    _publish_rulebook(session)
    DatabaseStages().run_checks(session, revision.id)

    report = run_check(session, revision.id)
    assert report.items == (), [(item.code, item.found) for item in report.items]
    assert report.product_type == product
    rules = {rule for _finding, _run, rule in _live(session, revision.id)}
    assert all(rule.startswith("CAB-") for rule in rules) == (product == "cabinet")


def test_a_confirmed_countertop_without_its_width_result_points_to_nina(
    session: Session, tmp_path: Path
) -> None:
    from storage.local import LocalStore
    from tests.workflow.test_part_operands import Assembly

    store = LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test-only")
    assembly = Assembly(session, store)
    finding = assembly.check(session, store)
    assert finding.outcome == "PASS"
    assert run_check(session, assembly.revision.id).items == ()

    _supersede(session, session.get_one(CheckRun, finding.check_run_id))
    report = run_check(session, assembly.revision.id)
    # It was the width rule's only result, so the rule has none either.
    assert _codes(report) == {"countertop-without-width-result", "rule-without-result"}
    item = next(item for item in report.items if item.code == "countertop-without-width-result")
    assert item.agent is Agent.NINA and item.action is Action.RERUN
    assert ("item", str(assembly.parts[0])) in item.ids


def test_a_revision_with_the_architect_drawings_as_their_own_file_is_ready(
    session: Session, tmp_path: Path
) -> None:
    from app.models import (
        Document,
        DocumentKind,
        DocumentVersion,
        Package,
        PackageRevisionDocument,
        SourceArtifact,
    )

    revision_id, _anchors = _rows(session, tmp_path, run=False)
    revision = session.get_one(PackageRevision, revision_id)
    package = session.get_one(Package, revision.package_id)
    source = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256="b" * 64, size=1)
    document = Document(package_id=package.id, kind=DocumentKind.ARCHITECTURAL.value)
    session.add_all((source, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=source.id, sha256="b" * 64, page_count=1
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision_id,
            package_id=package.id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    session.flush()
    _run(session, revision_id, tmp_path)
    architect, _run_row = _live_for(session, revision_id, "CT-ARCH-WIDTH-001")
    assert architect.outcome == "REVIEW_REQUIRED"

    assert run_check(session, revision_id).items == ()


# --- more seeded faults (#1222 review) ------------------------------------------------------------


def test_a_supporting_reading_that_names_no_valid_reading_is_reported_not_a_crash(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    anchor = session.get_one(ObservationCandidate, anchors[0])
    value_id = _canonical(session, anchors[0])
    supporter = ObservationCandidate(
        document_version_id=anchor.document_version_id,
        page_id=anchor.page_id,
        extraction_run_id=anchor.extraction_run_id,
        raw_text='40"',
        value_numerator=40,
        value_denominator=1,
        unit="in",
        polygon=anchor.polygon,
        coordinate_space="image",
        ambiguity_flags=["slot-reader-support", "supports:not-a-reading-id"],
    )
    session.add(supporter)
    session.flush()
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=value_id, candidate_id=supporter.id, role="primary"
        )
    )
    session.flush()
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        trace=_calculation("PASS"),
        inputs=({"canonical": value_id},),
    )

    item = _only(run_check(session, revision_id), "malformed-supporting-reading")
    assert item.agent is Agent.AKSHARA and item.action is Action.HOLD
    assert ("reading", str(supporter.id)) in item.ids


def test_a_pass_whose_wall_choice_was_withdrawn_after_the_run_asks_for_a_re_run(
    session: Session, tmp_path: Path
) -> None:
    """Walls are recomputed from the row, not only read from the result's note: the reviewer's
    wall choice was taken back after the run, so the PASS now rests on walls nobody chose. The run
    was right when it ran, so the item asks for a re-run rather than blaming it."""
    revision_id, anchors = _rows(session, tmp_path)
    decision = session.scalars(
        select(SlotRowReviewDecision).where(SlotRowReviewDecision.row_candidate_id == anchors[0])
    ).one()
    session.add(
        SlotRowReviewDecision(
            id=uuid4(),
            row_candidate_id=anchors[0],
            supersedes_id=decision.id,
            wall_config=None,
            measurements={},
            confirmed_by="synthetic reviewer",
        )
    )
    session.flush()

    report = run_check(session, revision_id)
    item = _only(report, "pass-on-automatic-walls")
    assert ("row", str(anchors[0])) in item.ids
    # The run was right when it ran: the item says the walls changed, never that it skipped the
    # click.
    assert "walls changed after this run" in item.found
    assert "Re-run the checks" in item.found
    assert "did not" not in item.found
    assert item.agent is Agent.NINA and item.action is Action.RERUN
    assert report.action == "re-run Nina"


def test_holds_and_re_runs_together_are_both_in_the_action(
    session: Session, tmp_path: Path
) -> None:
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,))
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    _forge(session, revision_id, replaces=run, outcome="NOT_FOUND", reason=None)

    report = run_check(session, revision_id)
    assert _codes(report) == {"silent-page", "abstention-without-reason"}
    assert report.action == "hold for a person; re-run Rekha"


def test_every_report_names_what_it_cannot_compute(session: Session, tmp_path: Path) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    report = run_check(session, revision_id)

    named = {entry.check: entry.reason for entry in report.not_computable}
    assert set(named) == {Check.TWO_JUDGMENTS, Check.WORDING}
    assert "mark" in named[Check.TWO_JUDGMENTS]
    assert "_" not in named[Check.WORDING], "plain English, no table names"
