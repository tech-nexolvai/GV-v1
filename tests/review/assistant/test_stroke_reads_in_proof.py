"""The sealed stroke reads in the proof rules: what they may back, and what they never do.

Verification for `verify_value`'s route 3 (`sealed_answer`, `with_sealed`), `reading_routes`'
`ai_seal` kind and `witness`'s `corroborated` witness (Step 2 of the rebuild plan, 2026-11-05).
Made-up findings and reads; no client value. Decision B (2026-10-26): a red needs every number it
rests on read by two different kinds of route; the plan counts a sealed two-maker reading as one
kind, so a sealed reading is two kinds only with its drawn length agreeing (`sealed_witnessed`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import proof, reading_routes, witness
from app.review.assistant.reading_routes import AI_SEAL, DRAWN, TEXT
from app.review.assistant.verify_value import BIG_BOX_PT2, sealed_answer, with_sealed

FILE = "set.pdf"
SPOT = {"x0": 100.0, "y0": 100.0, "x1": 110.0, "y1": 130.0}
CANNOT: dict[str, Any] = {
    "result": "cannot_read",
    "route": "none",
    "read": None,
    "reads": [],
    "reason": "x",
}
ADVOCATE_PASSED = {
    "model": "devil's advocate",
    "result": "passed",
    "note": "",
    "answers": {"pairing": "code"},
}


def reads(*labels: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "gv-stroke-reads/1",
        "pack_version": 26,
        "readers": [],
        "labels": list(labels),
    }


def label(
    state: str,
    inches: str | None = "23",
    *,
    box: list[float] | None = None,
    text: str = '23"',
    page: int = 1,
) -> dict[str, Any]:
    return {
        "id": "p1.S1",
        "file": FILE,
        "page": page,
        "side": "vendor",
        "box": box or [102.0, 104.0, 108.0, 126.0],
        "reasons": ["sideways"],
        "turns_tried": [90, 270],
        "answers": [
            {"model": "anthropic.claude-sonnet-5-5", "turn": 270, "text": text},
            {"model": "qwen.qwen3-vl-235b-a22b", "turn": 270, "text": text},
        ],
        "state": state,
        "why": None,
        "turn": 270 if state.startswith("sealed") else None,
        "text": text if state.startswith("sealed") else None,
        "inches": inches if state.startswith("sealed") else None,
    }


# ---- route 3 -------------------------------------------------------------------------------------


def test_a_sealed_witnessed_number_at_the_spot_matches_an_equal_claim() -> None:
    found = sealed_answer(reads(label("sealed_witnessed")), FILE, 1, SPOT, '23"')
    assert found is not None and (found.result, found.route, found.read) == (
        "match",
        "sealed",
        '23"',
    )
    assert found.words().startswith("Read by two AI readers of different makers")
    assert any("drawn length agrees" in r for r in found.reads)


def test_a_sealed_witnessed_number_mismatches_a_different_claim() -> None:
    found = sealed_answer(reads(label("sealed_witnessed")), FILE, 1, SPOT, '25"')
    assert found is not None and (found.result, found.route) == ("mismatch", "sealed")
    assert found.words() == (
        'Two AI readers of different makers read 23" here (its drawn length agreeing), not 25"'
    )


@pytest.mark.parametrize("state", ["sealed_unwitnessed", "held", "unread"])
def test_any_other_state_says_nothing(state: str) -> None:
    assert sealed_answer(reads(label(state)), FILE, 1, SPOT, '23"') is None


def test_it_says_nothing_off_its_page_file_or_box_or_with_two_labels_in_the_box() -> None:
    one = label("sealed_witnessed")
    assert sealed_answer(reads(one), FILE, 2, SPOT, '23"') is None
    assert sealed_answer(reads(one), "other.pdf", 1, SPOT, '23"') is None
    far = {"x0": 400.0, "y0": 400.0, "x1": 410.0, "y1": 410.0}
    assert sealed_answer(reads(one), FILE, 1, far, '23"') is None
    neighbour = {**label("unread"), "id": "p1.S2", "box": [104.0, 110.0, 107.0, 120.0]}
    assert sealed_answer(reads(one, neighbour), FILE, 1, SPOT, '23"') is None
    assert sealed_answer(None, FILE, 1, SPOT, '23"') is None
    assert sealed_answer(reads(one), FILE, 1, SPOT, "a note") is None


def test_in_a_big_box_it_may_match_but_never_mismatch() -> None:
    big = {"x0": 0.0, "y0": 0.0, "x1": 400.0, "y1": 400.0}
    assert (400.0 * 400.0) > BIG_BOX_PT2
    matched = sealed_answer(reads(label("sealed_witnessed")), FILE, 1, big, '23"')
    assert matched is not None and matched.result == "match"
    assert sealed_answer(reads(label("sealed_witnessed")), FILE, 1, big, '25"') is None


def test_it_never_loosens_the_drawings_own_reading() -> None:
    match = sealed_answer(reads(label("sealed_witnessed")), FILE, 1, SPOT, '23"')
    differ = sealed_answer(reads(label("sealed_witnessed")), FILE, 1, SPOT, '25"')
    text_match = {"result": "match", "route": "text", "read": '25"'}
    text_mismatch = {"result": "mismatch", "route": "text", "read": '26"'}
    assert with_sealed(CANNOT, None) == CANNOT
    assert with_sealed(text_mismatch, match) == text_mismatch  # their mismatch stands
    assert with_sealed(CANNOT, match)["route"] == "sealed"  # they could not read: ours
    assert with_sealed(text_match, differ)["result"] == "mismatch"  # disagreeing: held
    assert with_sealed({**text_match, "read": '23"'}, match)["route"] == "text"  # theirs


# ---- what a sealed reading may back --------------------------------------------------------------


@dataclass
class _Check:
    agrees: bool
    drawn_in: float


@dataclass
class _Fit:
    checks: dict[str, _Check] = field(default_factory=dict)

    def check(self, label_id: object) -> _Check | None:
        return self.checks.get(str(label_id))


ARCHITECT = {
    "id": "D9",
    "page": 2,
    "file": FILE,
    "text": '24"',
    "inches": "24",
    "side": "architect",
    "route": "text",
    "box": [10, 10, 30, 18],
}


def _wrong(verify_vendor: dict[str, Any]) -> dict[str, Any]:
    """A Wrong whose vendor number is an unread pen-stroke number (no code label of its own) and
    whose architect number is printed and drawn to its value; code paired the two itself, and the
    independent second read agrees."""
    checks = {
        "printed_architect": "match",
        "verify_vendor": verify_vendor,
        "second_read": "agrees",
        "lead_checked": True,
        "critic": "passed",
    }
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 1,
        "vs_page": 2,
        "item": "Run: depth",
        "category": "cabinet",
        "vendor": '23"',
        "architect": '24"',
        "status": "different",
        "source": "architect",
        "paired_by": "code",
        "critic": ADVOCATE_PASSED,
        "vendor_box": {**SPOT, "page": 1, "file": FILE},
        "checks": checks,
        "inches": {"vendor": "23", "architect": "24", "difference": "1"},
        "dims": {"architect": ARCHITECT},
        "witnesses": witness.witnesses(checks, "different"),
        "trust_level": witness.trust_level(checks, "different"),
    }
    reading_routes.mark(
        finding,
        lambda f, p: [ARCHITECT] if (f, p) == (FILE, 2) else [],
        lambda f, p: _Fit({"D9": _Check(True, 24.0)}),
    )
    return finding


def _read(state: str, claim: str = '23"') -> dict[str, Any]:
    return with_sealed(CANNOT, sealed_answer(reads(label(state)), FILE, 1, SPOT, claim))


def test_a_sealed_witnessed_reading_is_two_kinds_and_may_back_a_red() -> None:
    finding = _wrong(_read("sealed_witnessed"))
    values = {v.side: v for v in reading_routes.verdict(finding).values}
    assert values["vendor"].routes == (AI_SEAL, DRAWN)
    assert values["architect"].routes == (TEXT, DRAWN)
    assert reading_routes.held(finding) is None
    assert finding["witnesses"] == ["corroborated"]
    assert finding["trust_level"] == "confirmed"  # the independent read agrees
    assert proof.code_proved(finding)
    assert proof.proof_group(finding) == "clouded"


def test_a_sealed_unwitnessed_reading_alone_never_makes_red() -> None:
    finding = _wrong(_read("sealed_unwitnessed"))
    assert finding["checks"]["verify_vendor"]["result"] == "cannot_read"
    assert finding["witnesses"] == []
    assert reading_routes.verdict(finding).kind == "missing"
    assert not proof.code_proved(finding)
    assert proof.proof_group(finding) != "clouded"


def test_a_sealed_reading_of_another_value_is_a_doubt_never_a_red() -> None:
    finding = _wrong(_read("sealed_witnessed", claim='25"'))
    finding["vendor"], finding["inches"]["vendor"] = '25"', "25"
    assert 'two AI readers read 23" at its spot' in (reading_routes.held(finding) or "")
    assert witness.disagreement(finding["checks"], "different") is not None
    assert witness.trust_level(finding["checks"], "different") == "to_check"
    assert not proof.code_proved(finding)


def test_the_corroborated_witness_is_apart_from_code_observed_and_confirms_only_with_the_read() -> (
    None
):
    checks = {"printed_architect": "match", "verify_vendor": _read("sealed_witnessed")}
    assert witness.witnesses(checks) == ["corroborated"]
    assert witness.evidence_kind(checks) == "derived"
    assert witness.trust_level(checks) == "likely"
    assert witness.trust_level({**checks, "lead_checked": True, "critic": "passed"}) == "likely"
    assert witness.trust_level({**checks, "second_read": "agrees"}) == "confirmed"
    # A side the sealed route read never counts towards the drawn-ink witness.
    drawn = {"result": "match", "route": "drawn", "read": '24"'}
    both = {"verify_architect": drawn, "verify_vendor": _read("sealed_witnessed")}
    assert witness.witnesses(both) == ["corroborated"]
    assert "ocr_ink" not in witness.witnesses(both)


# ---- the proving reads use it (`spot_pool.verify_answer_at`) -------------------------------------


def _wire(monkeypatch: pytest.MonkeyPatch, asked: list[str]) -> None:
    from app.review.assistant import read_number
    from app.review.assistant import verify_value as vv

    def cannot(*_args: Any, **_kwargs: Any) -> vv.Verified:
        return vv.Verified("cannot_read", "none", None, (), "no characters here", '23"')

    def reader(*_args: Any) -> Any:
        def read(_request: Any) -> Any:
            asked.append("read_number")
            raise RuntimeError("not read")

        return read

    monkeypatch.setattr(vv, "verify_value", cannot)
    monkeypatch.setattr(read_number, "reader_for_pack", reader)


def test_the_proving_read_answers_from_a_sealed_witnessed_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from app.review.assistant import spot_pool

    asked: list[str] = []
    _wire(monkeypatch, asked)
    (tmp_path / "stroke_reads.json").write_text(json.dumps(reads(label("sealed_witnessed"))))
    found = spot_pool.verify_answer_at(tmp_path, tmp_path, FILE, 1, (100, 100, 110, 130), '23"')
    assert (found["result"], found["route"]) == ("match", "sealed") and asked == []


def test_with_no_stroke_reads_the_proving_read_is_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from app.review.assistant import spot_pool

    asked: list[str] = []
    _wire(monkeypatch, asked)
    found = spot_pool.verify_answer_at(tmp_path, tmp_path, FILE, 1, (100, 100, 110, 130), '23"')
    assert found["result"] == "cannot_read" and asked == ["read_number"]
    (tmp_path / "stroke_reads.json").write_text(json.dumps(reads(label("sealed_unwitnessed"))))
    found = spot_pool.verify_answer_at(tmp_path, tmp_path, FILE, 1, (100, 100, 110, 130), '23"')
    assert found["result"] == "cannot_read" and asked == ["read_number", "read_number"]
