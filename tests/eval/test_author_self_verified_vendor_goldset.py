"""Safety boundary for the self-verified vendor-reading answer-key harness.

All inputs here are invented JSON and bytes. Client proposals and drawings remain under ignored
``data/`` and never enter the test suite.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from eval.gold_set.schema import GoldCase
from scripts.author_self_verified_vendor_goldset import (
    TYPE_STATUS,
    VALUE_PROVENANCE,
    Proposal,
    author_cases,
    case_payload,
    self_verified,
    suggest_types,
    write_pending_report,
)
from vocabulary.semantic_types import SemanticType


def _proposal(
    proposal_id: str,
    raw: str,
    *,
    agreement: str = "AGREE",
    bbox: list[int] | None = None,
    proposed_type: str = "needs human",
) -> Proposal:
    """Make one entirely synthetic proposal without client values or drawing bytes."""
    return Proposal(
        project="synthetic_project",
        row={
            "id": proposal_id,
            "page": 2,
            "raw": raw,
            "agreement": agreement,
            "bbox_300dpi": bbox or [100, 200, 140, 240],
            "proposed_type": proposed_type,
        },
    )


def test_only_independently_agreeing_dual_tokens_are_eligible() -> None:
    """An AGREE label alone cannot promote malformed or inconsistent authored units."""
    agreeing = _proposal("p02-d001", "762 [30]")
    labelled_pending = _proposal("p02-d002", "762 [0]", agreement="DISAGREE - needs human")

    assert self_verified([agreeing, labelled_pending]) == [agreeing]

    falsely_labelled = _proposal("p02-d003", "100 [100]")
    with pytest.raises(ValueError, match="do not independently corroborate"):
        self_verified([falsely_labelled])

    malformed = _proposal("p02-d004", "about thirty inches")
    with pytest.raises(ValueError, match="not dual notation"):
        self_verified([malformed])

    missing_raw = _proposal("p02-d005", "762 [30]")
    del missing_raw.row["raw"]
    with pytest.raises(TypeError, match="has no raw dual token"):
        self_verified([missing_raw])


def test_position_types_are_suggestions_with_a_neutral_unclear_fallback() -> None:
    """Three-token rows suggest endpoint/interior types while isolated text stays neutral."""
    left = _proposal("p02-d001", "76 [3]", bbox=[100, 200, 140, 240])
    middle = _proposal("p02-d002", "381 [15]", bbox=[200, 202, 240, 242])
    right = _proposal("p02-d003", "76 [3]", bbox=[300, 201, 340, 241])
    isolated = _proposal("p02-d004", "762 [30]", bbox=[500, 500, 540, 540])

    suggestions = suggest_types([left, middle, right, isolated])

    assert suggestions[(left.project, left.proposal_id)].semantic_type is SemanticType.FILLER_WIDTH
    assert TYPE_STATUS in suggestions[(left.project, left.proposal_id)].basis
    assert (
        suggestions[(middle.project, middle.proposal_id)].semantic_type
        is SemanticType.CABINET_WIDTH
    )
    assert (
        suggestions[(right.project, right.proposal_id)].semantic_type is SemanticType.FILLER_WIDTH
    )
    assert TYPE_STATUS in suggestions[(right.project, right.proposal_id)].basis
    fallback = suggestions[(isolated.project, isolated.proposal_id)]
    assert fallback.semantic_type is SemanticType.FIELD_DIMENSION
    assert TYPE_STATUS in fallback.basis


def test_two_token_row_does_not_invent_endpoint_or_cabinet_types() -> None:
    """A two-token row is too ambiguous to classify and must keep the neutral reading type."""
    left = _proposal("p02-d001", "76 [3]", bbox=[100, 200, 140, 240])
    right = _proposal("p02-d002", "381 [15]", bbox=[200, 202, 240, 242])

    suggestions = suggest_types([left, right])

    for proposal in (left, right):
        suggestion = suggestions[(proposal.project, proposal.proposal_id)]
        assert suggestion.semantic_type is SemanticType.FIELD_DIMENSION
        assert TYPE_STATUS in suggestion.basis


def test_existing_positional_labels_remain_explicitly_unconfirmed() -> None:
    """Upstream positional suggestions may be retained but can never look human-confirmed."""
    filler = _proposal(
        "p02-d001",
        "76 [3]",
        proposed_type="filler_width (suggested from end-of-run position)",
    )
    cabinet = _proposal(
        "p02-d002",
        "381 [15]",
        proposed_type="cabinet_width (suggested from mid-run position)",
    )

    suggestions = suggest_types([filler, cabinet])

    assert (
        suggestions[(filler.project, filler.proposal_id)].semantic_type is SemanticType.FILLER_WIDTH
    )
    assert TYPE_STATUS in suggestions[(filler.project, filler.proposal_id)].basis
    assert (
        suggestions[(cabinet.project, cabinet.proposal_id)].semantic_type
        is SemanticType.CABINET_WIDTH
    )
    assert TYPE_STATUS in suggestions[(cabinet.project, cabinet.proposal_id)].basis


def test_authored_cases_are_reading_only_and_mark_the_type_unconfirmed(tmp_path: Path) -> None:
    """Authored truth contains a reading only and makes heuristic type provenance conspicuous."""
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"synthetic PDF bytes, not a drawing")
    proposal = _proposal(
        "p02-d001",
        "2735 [107 3/4]",
        proposed_type="cabinet_width (suggested from mid-run position)",
    )

    written = author_cases(
        self_verified([proposal]),
        source_pdf=source,
        output_root=tmp_path / "cases",
        annotated_on=date(2026, 9, 10),
    )

    assert len(written) == 1
    payload = json.loads((written[0] / "answer_key.json").read_text(encoding="utf-8"))
    case = GoldCase.model_validate(payload)
    answer = case.ground_truth.observations[0]
    assert answer.value.exact.numerator == 431
    assert answer.value.exact.denominator == 4
    assert answer.value.raw_text == "2735 [107 3/4]"
    assert answer.semantic_type is SemanticType.CABINET_WIDTH
    assert case.ground_truth.expected_findings == ()
    assert VALUE_PROVENANCE in case.provenance.annotator
    assert "semantic_type positional heuristic-unconfirmed" in case.provenance.annotator

    metadata = json.loads((written[0] / "case_metadata.json").read_text(encoding="utf-8"))
    assert metadata["semantic_type_status"] == TYPE_STATUS
    assert metadata["semantic_type_basis"].startswith(
        "upstream proposal label records a mid-run segment position"
    )
    assert TYPE_STATUS in metadata["semantic_type_basis"]
    assert metadata["expected_finding"] == "ABSENT - reading accuracy only"


def test_payload_rechecks_dual_corroboration_at_write_boundary() -> None:
    """Direct callers cannot bypass eligibility checks and author inconsistent dual notation."""
    proposal = _proposal("p02-d001", "100 [100]")
    suggestion = suggest_types([proposal])[(proposal.project, proposal.proposal_id)]

    with pytest.raises(ValueError, match="do not independently corroborate"):
        case_payload(
            proposal,
            suggestion=suggestion,
            source_name="synthetic.pdf",
            source_hash="sha256:" + "0" * 64,
            annotated_on=date(2026, 9, 10),
        )


def test_unsafe_and_duplicate_case_ids_are_refused_before_writes(tmp_path: Path) -> None:
    """Proposal IDs cannot escape the case root or silently collide with another package."""
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"synthetic PDF bytes, not a drawing")
    output = tmp_path / "cases"

    unsafe = _proposal("../escape", "762 [30]")
    with pytest.raises(ValueError, match="unsafe case id"):
        author_cases(
            [unsafe], source_pdf=source, output_root=output, annotated_on=date(2026, 9, 10)
        )
    assert not output.exists()

    duplicate = _proposal("p02-d001", "762 [30]")
    with pytest.raises(ValueError, match="duplicate case id"):
        author_cases(
            [duplicate, duplicate],
            source_pdf=source,
            output_root=output,
            annotated_on=date(2026, 9, 10),
        )
    assert not output.exists()


def test_invalid_later_proposal_cannot_leave_a_partial_batch(tmp_path: Path) -> None:
    """Every payload is validated before the authoring transaction makes its first directory."""
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"synthetic PDF bytes, not a drawing")
    output = tmp_path / "cases"
    valid = _proposal("p02-d001", "762 [30]")
    inconsistent = _proposal("p02-d002", "100 [100]")

    with pytest.raises(ValueError, match="do not independently corroborate"):
        author_cases(
            [valid, inconsistent],
            source_pdf=source,
            output_root=output,
            annotated_on=date(2026, 9, 10),
        )

    assert not output.exists()


def test_pending_report_lists_only_non_agree_rows(tmp_path: Path) -> None:
    """The pending artifact is complete, dynamically counted, and excludes authored rows."""
    agreeing = _proposal("p02-d001", "762 [30]")
    disagreement = _proposal("p02-d002", "762 [0]", agreement="DISAGREE - needs human")
    no_pair = _proposal("p02-d003", '30"', agreement="NO DUAL PAIR - needs human")
    report = tmp_path / "pending.md"

    write_pending_report([agreeing, disagreement, no_pair], report)

    text = report.read_text(encoding="utf-8")
    assert "These 2 proposals" in text
    assert "p02-d001" not in text
    assert "p02-d002" in text
    assert "p02-d003" in text
