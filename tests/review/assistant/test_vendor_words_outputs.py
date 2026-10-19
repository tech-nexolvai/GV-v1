"""GV's own working words never reach the vendor's files (the pipeline audit, 2026-10-19). The
simple full check writes its reasons for GV's screen ("the blind second look was not made (the call
failed)", "the answer names no architect label code knows (p23.D9)", "by the AI's reading", the
compare model's own note); the Not-compared PDF and workbook went to the vendor with them. The proof
line said "the critic passed it" (the simple check has no critic; its code checks pass it). The
check report named the chat's model, not the models the simple check used. Made-up values only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.review.assistant import not_compared
from app.review.assistant.proof import proof_line
from tests.review.assistant.test_proof_drawing import _proved
from tests.review.assistant.test_simple_run_stop import _Run


def _unproved(why: str, fid: str = "F1") -> dict[str, Any]:
    return {
        "id": fid,
        "page": 3,
        "item": "Kitchen: overall width",
        "category": "cabinet",
        "architect": "3' - 6\"",
        "vendor": '45"',
        "status": "check",
        "why": why,
        "source": "architect",
        "created_by": "simple_check",
        "verified": False,
    }


def test_the_not_compared_list_says_no_working_words() -> None:
    findings = [
        _unproved("Not proved: the blind second look was not made (the call failed).", "F1"),
        _unproved("Not proved: the answer names no architect label code knows (p23.D9).", "F2"),
        _unproved("Not settled from the drawings: the model could not see the left end.", "F3"),
        _unproved("The layout differs by the AI's reading; code cannot count it.", "F4"),
        _unproved("Not proved: round 1: round 2 is the vendor's current drawing.", "F5"),
    ]
    rows = {row.finding_id: row for row in not_compared.rows(findings)}
    for fid in ("F1", "F2", "F3", "F4"):
        assert rows[fid].why == "the drawings do not settle it", rows[fid].why
    assert "round 2 is the vendor's current drawing" in rows["F5"].why
    gaps = not_compared.gap_rows(
        [
            {"page": 3, "what": "45\" (page 3, label D7): the answer's item limit was reached"},
            {"page": 8, "what": "Kitchen: room context, not millwork, by the AI's reading"},
            {"page": 9, "what": '30" (page 9, label D2): architect gives no size'},
        ]
    )
    items = sorted(row.item for row in gaps)
    assert items == [
        '30" (page 9, label D2): architect gives no size',
        '45" (page 3, label D7): too many items in one check to list it',
        "Kitchen: the drawings do not settle it",
    ]


def test_a_proof_line_never_names_the_critic() -> None:
    finding = _proved(checks={**_proved()["checks"], "critic": "passed"})
    line = proof_line(finding)
    assert "critic" not in line.lower()
    assert "a second, independent check passed it" in line


def test_the_check_report_names_the_models_the_simple_check_used(tmp_path: Path) -> None:
    check = _Run(
        tmp_path,
        {
            "compare": {
                "model": "claude-sonnet-5-5",
                "effort": "high",
                "hard_model": "claude-opus-5-5",
                "hard_effort": "high",
            },
            "blind_model": "claude-opus-5-5",
            "blind_effort": "high",
        },
    )
    words = check.engine._model_words()
    assert words.startswith("compare Sonnet 5.5, high effort")
    assert "hard drawings Opus 5.5, high effort" in words
    assert words.endswith("blind second look Opus 5.5, high effort")
