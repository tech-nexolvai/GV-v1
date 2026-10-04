"""A redline note presents persisted finding facts without inventing a value or role."""

from __future__ import annotations

from types import SimpleNamespace

from workflow.redline_outputs import _stored_finding


def test_stored_scope_comparison_and_layout_reach_the_redline_note() -> None:
    row = SimpleNamespace(
        outcome="FAIL",
        severity="FLAG",
        reason="Recorded reason.",
        trace={"comparison": "Recorded comparison.", "reason": "Fallback."},
        scope_label="Countertop A",
        notes=["Wall layout: chosen by reviewer on recorded date."],
    )
    run = SimpleNamespace(engine_version="test")

    presented = _stored_finding(row, run, "CT-WIDTH-001", "sha256:recorded", ())

    assert presented.scope_label == "Countertop A"
    assert presented.comparison == "Recorded comparison."
    assert presented.notes == ("Wall layout: chosen by reviewer on recorded date.",)
    assert presented.reason == "Recorded reason."


def test_missing_stored_fields_do_not_become_display_values() -> None:
    row = SimpleNamespace(
        outcome="REVIEW_REQUIRED",
        severity="FLAG",
        reason=None,
        trace={"reason": "Could not decide."},
        scope_label=None,
        notes=None,
    )
    run = SimpleNamespace(engine_version="test")

    presented = _stored_finding(row, run, "CT-WIDTH-001", "sha256:recorded", ())

    assert presented.scope_label is None
    assert presented.comparison is None
    assert presented.notes == ()
    assert presented.reason == "Could not decide."
