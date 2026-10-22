"""How code's reading at a spot (`verify_value`, #1531) moves a chat finding's trust level. $0."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import chat_trust, witness


def _side(result: str, route: str, read: str | None = '46"', value: str = '46"') -> dict[str, Any]:
    return {
        "result": result,
        "route": route,
        "read": read,
        "reads": [],
        "reason": "",
        "value": value,
        "pending": False,
        "words": "",
    }


def _checks(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "printed_architect": "no_text",
        "printed_vendor": "no_text",
        "second_read": "not_run",
        "lead_checked": False,
    }
    return {**base, **fields}


def test_a_text_match_on_both_sides_is_the_printed_witness() -> None:
    checks = _checks(
        printed_architect="match",
        printed_vendor="match",
        verify_architect=_side("match", "text"),
        verify_vendor=_side("match", "text"),
        lead_checked=True,
    )
    assert witness.witnesses(checks) == ["printed"]
    assert witness.trust_level(checks) == "confirmed"


def test_a_drawn_ink_match_confirms_only_with_the_independent_read() -> None:
    drawn = {
        "printed_architect": "match",
        "verify_architect": _side("match", "text"),
        "verify_vendor": _side("match", "drawn"),
    }
    lead_only = _checks(**drawn, lead_checked=True)
    assert witness.witnesses(lead_only) == ["ocr_ink"]
    assert witness.evidence_kind(lead_only) == "observed"
    assert witness.trust_level(lead_only) == "likely"
    critic_too = _checks(**drawn, lead_checked=True, critic="passed")
    assert witness.trust_level(critic_too) == "likely"
    read = _checks(**drawn, second_read="agrees")
    assert witness.trust_level(read) == "confirmed"
    third = _checks(**drawn, second_read="unsure", adjudication="agrees_after_adjudication")
    assert witness.trust_level(third) == "confirmed"
    alone = _checks(**drawn)
    assert witness.trust_level(alone) == "likely"


def test_one_side_read_is_not_a_witness() -> None:
    checks = _checks(verify_vendor=_side("match", "drawn"), second_read="agrees")
    assert "ocr_ink" not in witness.witnesses(checks)
    assert witness.trust_level(checks) == "to_check"


def test_the_drawing_printing_another_value_makes_it_to_check() -> None:
    checks = _checks(
        printed_architect="match",
        verify_architect=_side("match", "text"),
        printed_vendor="not_found",
        verify_vendor=_side("mismatch", "text", read="1168 [46]", value='47"'),
        second_read="agrees",
        lead_checked=True,
    )
    assert witness.disagreement(checks) == 'the drawing prints 1168 [46] here, not 47"'
    assert witness.trust_level(checks) == "to_check"


def test_cannot_read_is_neutral() -> None:
    checks = _checks(
        verify_architect=_side("cannot_read", "none", read=None),
        verify_vendor=_side("cannot_read", "drawn", read=None),
        second_read="agrees",
        lead_checked=True,
    )
    assert witness.disagreement(checks) is None
    assert witness.trust_level(checks) == "likely"


class _Checks:
    """`finding_checks` as code would answer, without reading a PDF."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def drawn_checks(self, finding: dict[str, Any], pdf_paths: dict[str, Path]) -> dict[str, Any]:
        self.asked.append(str(finding["id"]))
        return dict(self.answer)


def _finding(fid: str, status: str = "different", **checks: Any) -> dict[str, Any]:
    pending = {**_side("cannot_read", "none", read=None), "pending": True}
    return {
        "id": fid,
        "status": status,
        "architect": '46"',
        "vendor": '46"',
        "arch_box": {"page": 1, "x0": 1, "y0": 1, "x1": 9, "y1": 9},
        "vendor_box": {"page": 2, "x0": 1, "y0": 1, "x1": 9, "y1": 9},
        "checks": _checks(
            printed_architect="match",
            verify_architect=_side("match", "text"),
            verify_vendor=pending,
            **checks,
        ),
    }


def test_the_drawn_ink_pass_reads_pending_sides_and_judges_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Checks({"verify_vendor": _side("match", "drawn")})
    monkeypatch.setattr(chat_trust, "load_module", lambda name: fake)
    findings = [
        _finding("F1", status="same", second_read="agrees"),
        _finding("F2", status="different", second_read="agrees"),
        {**_finding("F3"), "checks": _checks()},  # nothing pending: not read
    ]
    found = chat_trust.drawn_ink_checks(findings, {})
    assert list(found) == ["F2", "F1"]  # differences first (read at once on code's pool)
    assert sorted(fake.asked) == ["F1", "F2"]
    updated = {f["id"]: f for f in chat_trust.with_drawn_ink(findings, found)}
    assert set(updated) == {"F1", "F2"}
    assert updated["F2"]["checks"]["verify_vendor"]["route"] == "drawn"
    assert updated["F2"]["witnesses"] == ["ocr_ink"]
    assert updated["F2"]["trust_level"] == "confirmed"
    # A finding whose value changed since it was read keeps its own checks.
    changed = [{**findings[1], "vendor": '47"'}]
    assert chat_trust.with_drawn_ink(changed, found) == []


def test_the_drawn_ink_pass_stops_when_told(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Checks({"verify_vendor": _side("match", "drawn")})
    monkeypatch.setattr(chat_trust, "load_module", lambda name: fake)
    assert chat_trust.drawn_ink_checks([_finding("F1")], {}, stop=lambda: True) == {}
    assert chat_trust.drawn_ink_checks([_finding("F1")], {}, budget_s=-1) == {}
    assert fake.asked == []


def test_the_drawn_ink_pass_on_the_pool_keeps_its_order_and_its_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1850: the findings are read on a few threads, the first ones slowest; the answers come
    back in the old order, and a finding whose read raised is reported with its reason."""
    import time

    class Slow:
        def drawn_checks(self, finding: dict[str, Any], _paths: Any) -> dict[str, Any]:
            number = int(str(finding["id"])[1:])
            time.sleep(0.02 * (8 - number))
            if number == 3:
                raise RuntimeError("this page could not be rendered")
            return {"verify_vendor": _side("match", "drawn", read=str(number))}

    monkeypatch.setattr(chat_trust, "load_module", lambda name: Slow())
    monkeypatch.setenv("GV_ASSISTANT_CODE_WORKERS", "3")
    findings = [_finding(f"F{n}") for n in range(1, 8)]
    found = chat_trust.drawn_ink_checks(findings, {})
    assert list(found) == ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]
    read = [found[f]["verify_vendor"]["read"] for f in found if f != "F3"]
    assert read == ["1", "2", "4", "5", "6", "7"]
    # The finding whose read raised: reported, never dropped; its side stays unread, with why.
    assert chat_trust.FAILED_KEY in found["F3"] and "verify_vendor" not in found["F3"]
    updated = {f["id"]: f for f in chat_trust.with_drawn_ink(findings, found)}
    side = updated["F3"]["checks"]["verify_vendor"]
    assert side["pending"] and side["reason"].startswith(chat_trust.DRAWN_INK_FAILED)
    # The item is Not compared with the reason; the others are read as before.
    from app.review.assistant import classification, proof

    assert updated["F3"]["reader_error"] == classification.READER_ERROR_WORDS
    assert "reader_error" not in updated["F2"]
    group = proof.proof_group(updated["F3"])
    assert group == "not_compared"


def test_a_checks_module_without_the_drawn_route_reads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_trust, "load_module", lambda name: object())
    assert chat_trust.drawn_ink_checks([_finding("F1")], {}) == {}


def test_apply_checks_records_both_readings(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Found:
        def as_dict(self) -> dict[str, Any]:
            return {
                "printed_architect": "match",
                "printed_vendor": "not_found",
                "arithmetic": None,
                "units": None,
                "verify_architect": _side("match", "text"),
                "verify_vendor": _side("mismatch", "text", read='45"'),
            }

    class _Module:
        def check_finding(self, finding: Any, pdf_paths: Any) -> _Found:
            return _Found()

        def to_inches(self, text: str) -> None:
            return None

    monkeypatch.setattr(chat_trust, "load_module", lambda name: _Module())
    out = chat_trust.apply_checks(_finding("F1"), {}, None, second_read="agrees")
    assert out["checks"]["verify_vendor"]["result"] == "mismatch"
    assert out["trust_level"] == "to_check"
