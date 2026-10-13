"""Nina's gate (#1228): a PASS or FAIL that breaks a safety rule is held for a person, and only that.

Each seeded break either switches one safety step off for a real check run (the gate must then
hold what the step would have held) or writes the rows a broken check step would leave and asks
the gate directly (the same function the check stage calls last). Correct runs must come through
untouched. Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

import workflow.stages as stages_module
from alembic import command
from app.auth import Principal, Role
from app.db.session import session_factory
from app.models import Package, PackageRevision
from app.models.verdicts import CheckRun, Finding, VerdictInput
from tests.api.test_findings_query import _app, _url
from tests.api.test_slot_rows import _unwitnessed_on_first_page
from tests.app.postgres_fixture import alembic_config
from tests.eval.test_run_check import (
    _architect_result,
    _calculation,
    _canonical,
    _forge,
    _live,
    _live_for,
    _pairing,
    _rows,
)
from workflow.nina_gate import NINA_GATE, hold_unsafe_decisions
from workflow.pariksha_gates import NOTE_PREFIX
from workflow.pariksha_run_check import gate_tally, run_check

pytest_plugins = ("tests.app.postgres_fixture",)

HELD = "Held for a person by Pariksha: "


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


def _summaries(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Keep each check stage's summary of Nina's gate."""
    seen: list[dict[str, Any]] = []
    real = hold_unsafe_decisions

    def watched(session: Session, revision_id: UUID) -> Any:
        outcome = real(session, revision_id)
        seen.append(outcome.to_dict())
        return outcome

    monkeypatch.setattr(stages_module, "hold_unsafe_decisions", watched)
    return seen


def _gate_notes(finding: Finding) -> list[str]:
    return [note for note in finding.notes or () if note.startswith(NOTE_PREFIX)]


def _inputs(session: Session, run_id: UUID) -> list[tuple[object, ...]]:
    return [
        (
            item.operand_name,
            item.value_numerator,
            item.value_denominator,
            item.unit,
            item.evidence_status,
            item.canonical_observation_id,
            item.slot_row_review_decision_id,
        )
        for item in session.scalars(
            select(VerdictInput)
            .where(VerdictInput.check_run_id == run_id)
            .order_by(VerdictInput.operand_name)
        )
    ]


def _snapshot(session: Session, revision_id: UUID) -> dict[str, tuple[str, str | None]]:
    return {
        f"{rule}:{finding.scope_row_candidate_id}": (finding.outcome, finding.reason)
        for finding, _run, rule in _live(session, revision_id)
    }


# --- correct runs come through untouched -----------------------------------------------------------


def test_a_correct_run_is_left_exactly_as_it_was(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _summaries(monkeypatch)
    revision_id, anchors = _rows(session, tmp_path)

    assert seen and seen[-1]["held"] == 0 and seen[-1]["stops"] == []
    assert seen[-1]["checked"] >= 2, "the two row PASSes were looked at"
    for anchor in anchors.values():
        finding, _run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchor)
        assert finding.outcome == "PASS"
        assert _gate_notes(finding) == []
    assert run_check(session, revision_id).items == ()


def test_the_real_codes_own_hold_is_not_held_again(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _summaries(monkeypatch)
    revision_id, anchors = _rows(session, tmp_path, slot_flags=_unwitnessed_on_first_page)

    finding, _run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    assert finding.outcome == "REVIEW_REQUIRED"
    assert _gate_notes(finding) == []
    assert seen[-1]["held"] == 0


def test_running_the_gate_again_changes_nothing(session: Session, tmp_path: Path) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    before = _snapshot(session, revision_id)

    outcome = hold_unsafe_decisions(session, revision_id)

    assert outcome.held == ()
    assert _snapshot(session, revision_id) == before


# --- seeded breaks in a real check run ------------------------------------------------------------


def test_a_pass_on_an_unwitnessed_reading_is_held_with_the_checks_reason(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeded break: the step that holds such a PASS for one click is switched off."""
    monkeypatch.setattr(stages_module, "confirm_unwitnessed_pass", lambda finding, _u: finding)
    seen = _summaries(monkeypatch)

    revision_id, anchors = _rows(session, tmp_path, slot_flags=_unwitnessed_on_first_page)

    held, held_run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    assert held.outcome == "REVIEW_REQUIRED"
    assert held.reason is not None and held.reason.startswith(HELD + "A PASS rests on")
    assert held.notes is not None
    assert held.notes[0].startswith("The engine's result, which counts only once a person")
    assert ": PASS" in held.notes[0], "the engine's own outcome is kept"
    (note,) = _gate_notes(held)
    assert f"(Nina, {NINA_GATE}): stop [pass-on-unwitnessed-reading]" in note
    assert held.trace["cause"] == "needs_review" and "operation" not in held.trace
    assert held.delta_numerator is None
    # The decided result is kept as history, replaced, never edited or deleted.
    original = session.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .where(
            Finding.package_revision_id == revision_id,
            Finding.scope_row_candidate_id == anchors[0],
            Finding.outcome == "PASS",
            CheckRun.superseded_at.is_not(None),
        )
        .order_by(Finding.created_at.desc())
    ).first()
    assert original is not None
    assert held.variant == original.variant and held.severity == original.severity
    assert _inputs(session, held_run.id) == _inputs(session, original.check_run_id)
    assert seen[-1]["held"] == 1
    assert seen[-1]["stops"][0]["code"] == "pass-on-unwitnessed-reading"
    # The other row's PASS is left alone.
    other, _other_run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[1])
    assert other.outcome == "PASS"
    # Visible to the run check: nothing left to report, and the gate's stop is counted.
    assert run_check(session, revision_id).items == ()
    nina = next(count for count in gate_tally(session, revision_id) if count.agent.value == "Nina")
    assert nina.stopped == 1 and nina.passed >= 1


def test_a_pass_on_automatic_walls_is_held(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        stages_module,
        "confirm_vendor_walls_pass",
        lambda finding, *, walls_automatic, unwitnessed=(): finding,
    )
    revision_id, anchors = _rows(
        session, tmp_path, wall_source="code-ink-and-both-ais", walls_confirmed=False
    )

    for anchor in anchors.values():
        held, _run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchor)
        assert held.outcome == "REVIEW_REQUIRED"
        assert held.reason is not None and "walls set automatically" in held.reason
        assert any("[pass-on-automatic-walls]" in note for note in _gate_notes(held))
    assert run_check(session, revision_id).items == ()


def test_the_held_reason_reaches_the_findings_api(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stages_module, "confirm_unwitnessed_pass", lambda finding, _u: finding)
    revision_id, anchors = _rows(session, tmp_path, slot_flags=_unwitnessed_on_first_page)
    held, _run = _live_for(session, revision_id, "CT-WIDTH-001", row=anchors[0])
    revision = session.get(PackageRevision, revision_id)
    assert revision is not None
    package = session.get(Package, revision.package_id)
    assert package is not None
    principal = Principal(
        id="synthetic reviewer",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({package.project_id}),
    )

    body = TestClient(_app(principal, session)).get(_url(package.project_id, package.id)).json()

    shown = [item for item in body["items"] if item["id"] == str(held.id)]
    assert len(shown) == 1
    assert shown[0]["outcome"] == "REVIEW_REQUIRED"
    assert shown[0]["reason"] == held.reason
    assert shown[0]["reviewer_reason"] == held.reason


# --- seeded breaks a broken step would leave, handed to the gate ---------------------------------


def _held_only(session: Session, revision_id: UUID, forged: Finding, code: str) -> Finding:
    outcome = hold_unsafe_decisions(session, revision_id)
    assert {result.code for result in outcome.held} == {code}, outcome.to_dict()
    run = session.get(CheckRun, forged.check_run_id)
    assert run is not None and run.superseded_at is not None
    replacement = session.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .where(
            Finding.package_revision_id == revision_id,
            CheckRun.superseded_at.is_(None),
            Finding.id == UUID(outcome.held[0].scope_id or ""),
        )
    ).one()
    assert replacement.outcome == "REVIEW_REQUIRED"
    assert replacement.notes is not None and f": {forged.outcome} " in replacement.notes[0]
    assert any(f"[{code}]" in note for note in _gate_notes(replacement))
    return replacement


def test_a_decision_on_a_one_judgment_pairing_is_held(session: Session, tmp_path: Path) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    record = _pairing(session, revision_id, anchors[0], "code")
    forged = _architect_result(session, revision_id, anchors[0], noted=record)

    held = _held_only(session, revision_id, forged, "decision-on-one-judgment-pairing")
    assert held.scope_row_candidate_id == anchors[0]


def test_a_decision_on_a_reviewer_pairing_stands(session: Session, tmp_path: Path) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    record = _pairing(session, revision_id, anchors[0], "reviewer")
    forged = _architect_result(session, revision_id, anchors[0], noted=record)

    assert hold_unsafe_decisions(session, revision_id).held == ()
    run = session.get(CheckRun, forged.check_run_id)
    assert run is not None and run.superseded_at is None


def test_a_decision_with_no_stored_inputs_is_held(session: Session, tmp_path: Path) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    forged = _forge(session, revision_id, replaces=run, outcome="FAIL", trace=_calculation("FAIL"))

    held = _held_only(session, revision_id, forged, "decision-without-inputs")
    assert held.outcome == "REVIEW_REQUIRED", "a FAIL is held, never turned into a PASS"


@pytest.mark.parametrize(
    ("status", "authority", "unit", "code"),
    [
        ("CONFLICTING", "AUTHORITATIVE", "in", "decision-on-unqualified-evidence"),
        ("CORROBORATED", "ADVISORY", "in", "decision-on-advisory-value"),
        ("CORROBORATED", "AUTHORITATIVE", "furlong", "decision-in-unknown-unit"),
    ],
)
def test_a_decision_that_breaks_a_contract_gate_is_held(
    session: Session, tmp_path: Path, status: str, authority: str, unit: str, code: str
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    canonical = _canonical(session, anchors[0], status=status, authority=authority)
    forged = _forge(
        session,
        revision_id,
        replaces=run,
        outcome="PASS",
        trace=_calculation("PASS", unit=unit),
        inputs=({"canonical": canonical},),
    )

    _held_only(session, revision_id, forged, code)


def test_coverage_faults_are_left_to_the_report(session: Session, tmp_path: Path) -> None:
    """A rule with no result is about the run, not one result: nothing to hold."""
    revision_id, _anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    run.superseded_at = run.created_at
    session.flush()

    assert hold_unsafe_decisions(session, revision_id).held == ()
    assert {item.code for item in run_check(session, revision_id).items} == {"rule-without-result"}


def test_an_abstention_is_never_touched(session: Session, tmp_path: Path) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    _finding, run = _live_for(session, revision_id, "CT-DEPTH-001")
    _forge(
        session, revision_id, replaces=run, outcome="NOT_FOUND", reason="synthetic: nothing read"
    )

    assert hold_unsafe_decisions(session, revision_id).held == ()


def test_a_two_judgment_note_whose_record_is_missing_is_left_to_the_report(
    session: Session, tmp_path: Path
) -> None:
    """The result says code and both AIs paired it; only its record cannot be found. The record
    store is in doubt, not the result: the report lists it, the gate does not hold it."""
    revision_id, anchors = _rows(session, tmp_path)
    # A note naming a record that is not stored (as the suites' injected pairings do).
    missing = SimpleNamespace(id=uuid4(), source="code+ais", status="paired")
    forged = _architect_result(session, revision_id, anchors[0], noted=missing)  # type: ignore[arg-type]

    assert hold_unsafe_decisions(session, revision_id).held == ()
    run = session.get(CheckRun, forged.check_run_id)
    assert run is not None and run.superseded_at is None
    codes = {item.code for item in run_check(session, revision_id).items}
    assert codes == {"decision-without-pairing-record"}


def test_a_one_judgment_note_whose_record_disagrees_is_held(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    record = _pairing(session, revision_id, anchors[0], "code+ais")
    forged = _architect_result(session, revision_id, anchors[0], noted=record, source="code")

    _held_only(session, revision_id, forged, "decision-without-pairing-record")


def test_a_decided_architect_result_with_no_pairing_note_is_held(
    session: Session, tmp_path: Path
) -> None:
    revision_id, anchors = _rows(session, tmp_path)
    forged = _architect_result(session, revision_id, anchors[0], noted=None)

    _held_only(session, revision_id, forged, "decision-without-pairing-record")
